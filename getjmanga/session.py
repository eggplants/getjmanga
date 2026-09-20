"""The HTTP client every extractor shares."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from httpx._types import QueryParamTypes

#: How many times a connection that fails to open is tried again.
RETRIES = 10


#: Headers a browser would send, so sites serve what they serve a browser.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
}


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
