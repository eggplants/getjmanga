from __future__ import annotations

import pytest
from httpx import HTTPStatusError

from getjmanga.extractors import Comici, GigaViewer, Piccoma
from getjmanga.search import downloadable_links, search

PAGE = """
<html><body>
  <a href="https://shonenjumpplus.com/episode/1">ep</a>
  <a href="/episode/2#top">relative</a>
  <a href=" https://piccoma.com/web/viewer/8195/1185884 ">spaced</a>
  <a href="https://shonenjumpplus.com/episode/1">again</a>
  <a href="https://example.com/">nothing takes this</a>
  <a href="mailto:x@example.com">mail</a>
  <a name="anchor">no href</a>
  <a href="https://shonenjumpplus.com/">the page itself</a>
</body></html>
"""


def test_links_are_resolved_deduplicated_and_kept_in_page_order():
    assert downloadable_links(PAGE, "https://shonenjumpplus.com/") == [
        "https://shonenjumpplus.com/episode/1",
        "https://shonenjumpplus.com/episode/2",
        "https://piccoma.com/web/viewer/8195/1185884",
    ]


def test_the_page_itself_is_left_out_whatever_its_fragment():
    assert "https://shonenjumpplus.com/" not in downloadable_links(PAGE, "https://shonenjumpplus.com/#main")


def test_an_extractor_narrows_the_links_to_what_it_takes():
    assert downloadable_links(PAGE, "https://shonenjumpplus.com/", Piccoma) == [
        "https://piccoma.com/web/viewer/8195/1185884",
    ]
    assert downloadable_links(PAGE, "https://shonenjumpplus.com/", Comici) == []


def test_a_page_without_links_gives_nothing():
    assert downloadable_links("<p>nothing</p>", "https://shonenjumpplus.com/") == []


def test_search_fetches_the_page_and_resolves_against_where_it_landed(fake_session, fake_response):
    session = fake_session({"example.com/list": fake_response(text=PAGE, url="https://shonenjumpplus.com/")})
    links = search(session, "https://example.com/list")
    assert session.calls == ["https://example.com/list"]
    assert links[:2] == ["https://shonenjumpplus.com/episode/1", "https://shonenjumpplus.com/episode/2"]
    assert search(session, "https://example.com/list", GigaViewer) == links[:2]


def test_search_raises_on_a_failing_page(fake_session, fake_response):
    session = fake_session({"example.com": fake_response(status_code=404)})
    with pytest.raises(HTTPStatusError):
        search(session, "https://example.com/list")
