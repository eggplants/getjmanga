"""The shared HTTP session: what it sends and how it builds a URL."""

from __future__ import annotations

import httpx

from getjmanga.session import HEADERS, browser_headers, make_session


def test_browser_headers_name_a_current_desktop_browser():
    for _ in range(20):
        headers = browser_headers()
        agent = headers["User-Agent"]
        assert agent.startswith("Mozilla/5.0 (")
        assert "Macintosh" in agent or "Windows NT" in agent
        assert "Chrome/" in agent or "Version/" in agent
        assert "Mobile" not in agent
        assert headers["Accept-Language"].startswith("ja")
        # Chromium browsers say who they are in the client hints as well; Safari sends none.
        assert ("sec-ch-ua" in headers) == ("Chrome/" in agent)


def test_headers_are_drawn_once():
    assert HEADERS["User-Agent"].startswith("Mozilla/5.0 (")


def test_session_params_add_to_the_query_the_url_carries():
    seen = []

    def echo(request):
        seen.append(str(request.url))
        return httpx.Response(200)

    session = make_session()
    session._transport = httpx.MockTransport(echo)
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
    session._transport = httpx.MockTransport(hop)
    res = session.get("https://example.com/old")
    assert res.text == "landed"
    assert str(res.url) == "https://example.com/new"
