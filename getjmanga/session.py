"""The HTTP client every extractor shares, and the browser it claims to be."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx
import ua_generator
from ua_generator.options import Options

if TYPE_CHECKING:
    from httpx._types import QueryParamTypes

#: How many times a connection that fails to open is tried again.
RETRIES = 10


def browser_headers() -> dict[str, str]:
    """Headers a current desktop browser would send, so sites serve what they serve one.

    The User-Agent is drawn once per process from Chrome, Edge and Safari
    (each among its last three versions) on the latest macOS or Windows, with
    the client-hint headers a Chromium browser sends alongside it.

    Returns:
        The headers, with `User-Agent` and `Accept-Language` always present.
    """
    agent = ua_generator.generate(
        device="desktop",
        platform=("macos", "windows"),
        browser=("chrome", "edge", "safari"),
        options=Options(latest_versions={"chrome": 3, "edge": 3, "safari": 3, "macos": 1, "windows": 1}),
    )
    hints = {key: value for key, value in agent.headers.get().items() if key != "user-agent"}
    return {
        "User-Agent": agent.text,
        "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
        **hints,
    }


#: Headers a browser would send, drawn once and sent with every request.
HEADERS = browser_headers()


class Session(httpx.Client):
    """An `httpx.Client` whose `params` add to a URL's own query instead of replacing it.

    The extractors build URLs like `/api/csr?rq=title/detail` and pass the
    endpoint's own parameters separately; `httpx` would drop the `rq`, and
    re-encode it as `title%2Fdetail` if asked to merge. The query the URL
    carries is sent as written, the way `requests` sent it.
    """

    def build_request(
        self, method: str, url: httpx.URL | str, *, params: QueryParamTypes | None = None, **kwargs: Any
    ) -> httpx.Request:
        """Build a request, appending `params` to the query `url` already carries."""
        if params is not None:
            url = httpx.URL(url)
            query = str(httpx.QueryParams(params)).encode()
            if query:
                url = url.copy_with(query=url.query + b"&" + query if url.query else query)
            params = None
        return super().build_request(method, url, params=params, **kwargs)


def make_session() -> Session:
    """Build a session that follows redirects and retries connections that fail to open.

    Returns:
        A session whose connections are retried up to `RETRIES` times.
    """
    return Session(follow_redirects=True, transport=httpx.HTTPTransport(retries=RETRIES))
