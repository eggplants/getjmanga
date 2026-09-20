"""The shared HTTP session: what it sends and how it builds a URL."""

from __future__ import annotations

import httpx

from getjmanga.session import make_session


def test_session_params_add_to_the_query_the_url_carries():
    seen = []

    def echo(request):
        seen.append(str(request.url))
        return httpx.Response(200)

    session = make_session()
    session._transport = httpx.MockTransport(echo)  # noqa: SLF001 (no public way to swap the transport)
    session.get("https://example.com/api?rq=title/detail", params={"title_id": 1})
    session.get("https://example.com/api", params={"page": 2})
    session.get("https://example.com/api?rq=viewer")
    assert seen == [
        "https://example.com/api?rq=title/detail&title_id=1",
        "https://example.com/api?page=2",
        "https://example.com/api?rq=viewer",
    ]


def test_session_follows_redirects():
    def hop(request):
        if request.url.path == "/old":
            return httpx.Response(302, headers={"Location": "https://example.com/new"})
        return httpx.Response(200, text="landed")

    session = make_session()
    session._transport = httpx.MockTransport(hop)  # noqa: SLF001
    res = session.get("https://example.com/old")
    assert res.text == "landed"
    assert str(res.url) == "https://example.com/new"
