from __future__ import annotations

from dataclasses import replace
from datetime import date
from io import BytesIO

import pytest
from httpx import HTTPStatusError
from PIL import Image

from getjmanga.errors import LoginError, NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractor import Episode, Extractor, Page, neighbours, published_on


class Plain(Extractor):
    """An extractor that only implements what it must."""

    NAME = "plain"
    HOSTS = ("example.com",)

    def episode(self, url):
        return Episode(url=url, series_title="s", episode_title="e", pages=(Page(url="https://cdn.example/1.jpg"),))


def png_bytes(size=(8, 8), color=(1, 2, 3)):
    raw = BytesIO()
    Image.new("RGB", size, color).save(raw, "PNG")
    return raw.getvalue()


# --- the defaults a subclass inherits ------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://example.com/anything", True),
        ("http://example.com/anything", False),
        ("https://other.example.com/", False),
        ("not a url", False),
    ],
)
def test_suitable_checks_the_scheme_and_the_host(url, expected):
    assert Plain.suitable(url) is expected


def test_is_series_is_false_and_series_urls_refuses_by_default(fake_session):
    assert Plain(fake_session({})).is_series("https://example.com/series/1") is False
    with pytest.raises(UnsupportedUrlError, match="cannot list a series"):
        Plain(fake_session({})).series_urls("https://example.com/series/1")


def test_login_is_refused_by_default(fake_session):
    with pytest.raises(LoginError, match="does not support logging in"):
        Plain(fake_session({})).login("https://example.com/", "who", "pw")


def test_image_sends_the_episode_as_referer_and_decodes(fake_session, fake_response):
    session = fake_session({"cdn.example": fake_response(png_bytes())})
    plain = Plain(session)
    episode = plain.episode("https://example.com/ep/1")

    image = plain.image(episode.pages[0], episode)

    assert image.size == (8, 8)
    assert session.headers_seen[-1]["Referer"] == "https://example.com/ep/1"
    assert "User-Agent" in session.headers_seen[-1]


def test_get_raises_on_a_failing_status(fake_session, fake_response):
    session = fake_session({"example.com": fake_response(status_code=403)})
    with pytest.raises(HTTPStatusError):
        Plain(session)._get("https://example.com/")


def test_a_default_extractor_brings_its_own_session():
    assert Plain().session is not None


def test_publisher_is_per_host_else_the_extractors_own():
    class Imprints(Plain):
        PUBLISHER = "house"
        PUBLISHERS = {"a.example.com": "a"}  # noqa: RUF012

    assert Imprints.publisher("https://a.example.com/ep/1") == "a"
    assert Imprints.publisher("https://example.com/ep/1") == "house"
    assert Plain.publisher("https://example.com/ep/1") == ""


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-09-20T15:00:00Z", date(2026, 9, 21)),  # midnight in Japan, the day before in UTC
        ("2026-09-21T00:00:00+09:00", date(2026, 9, 21)),
        ("2026-09-21T23:30:00", date(2026, 9, 21)),  # no offset: the site's own clock
        ("2026-09-21", date(2026, 9, 21)),
        ("2026/09/21", date(2026, 9, 21)),
        ("2026.9.21", date(2026, 9, 21)),
        ("2026年9月21日 00:00", date(2026, 9, 21)),
        ("Sat, 20 Sep 2026 15:00:00 GMT", date(2026, 9, 21)),  # an HTTP date, UTC again
        ("公開日: 2026/9/21", date(2026, 9, 21)),
        (1789995600, date(2026, 9, 21)),  # epoch seconds
        (1789995600000, date(2026, 9, 21)),  # epoch milliseconds
        ("", None),
        (None, None),
        (0, None),
        ("2026-13-45", None),
        ("soon", None),
    ],
)
def test_published_on_reads_what_the_sites_write(value, expected):
    assert published_on(value) == expected


def test_dated_by_upload_reads_the_last_modified_header(fake_session, fake_response):
    session = fake_session(
        {"cdn.example": fake_response(b"", headers={"last-modified": "Thu, 21 Aug 2025 08:16:41 GMT"})}
    )
    plain = Plain(session)
    episode = plain._dated_by_upload(plain.episode("https://example.com/ep/1"))

    assert episode.published == date(2025, 8, 21)
    assert session.heads == [(episode.pages[0].url, {**Plain.HEADERS, "Referer": "https://example.com/ep/1"})]


def test_dated_by_upload_leaves_the_episode_without_the_header(fake_session, fake_response):
    plain = Plain(fake_session({"cdn.example": fake_response(b"")}))
    assert plain._dated_by_upload(plain.episode("https://example.com/ep/1")).published is None

    plain = Plain(fake_session({}))  # the HEAD is a 404
    assert plain._dated_by_upload(plain.episode("https://example.com/ep/1")).published is None
    assert plain._dated_by_upload(replace(plain.episode("https://example.com/ep/1"), pages=())).published is None


def test_episode_readable_means_it_has_pages():
    assert not Episode(url="u", series_title="s", episode_title="e").readable
    assert Episode(url="u", series_title="s", episode_title="e", pages=(Page(url="p"),)).readable


def test_cookie_picks_the_one_for_the_host(fake_session):
    plain = Plain(fake_session({}))
    plain.session.cookies.set("XSRF-TOKEN", "a", domain="example.com")
    plain.session.cookies.set("XSRF-TOKEN", "b", domain=".other.example")
    plain.session.cookies.set("other", "c", domain="example.com")

    assert plain._cookie("XSRF-TOKEN", "example.com") == "a"
    assert plain._cookie("XSRF-TOKEN", "www.other.example") == "b"
    assert plain._cookie("XSRF-TOKEN", "nowhere.example") is None
    assert plain._cookie("missing", "example.com") is None


def test_neighbours_looks_either_side_of_an_item():
    assert neighbours(["a", "b", "c"], "b") == ("a", "c")
    assert neighbours(["a", "b", "c"], "a") == (None, "b")
    assert neighbours(["a", "b", "c"], "c") == ("b", None)
    assert neighbours(["a"], "a") == (None, None)
    assert neighbours(["a", "b"], "x") == (None, None)


class Listing(Extractor):
    """Lists three episodes, counting how often it was asked."""

    NAME = "listing"
    HOSTS = ("example.com",)

    def __init__(self):
        super().__init__()
        self.listed = 0

    def series_urls(self, url):
        self.listed += 1
        if url.endswith("/gone"):
            raise NotAnEpisodePageError("gone")
        return [f"https://example.com/ep/{n}" for n in (1, 2, 3)]

    def episode(self, url):
        raise NotImplementedError

    def image(self, page, episode):
        raise NotImplementedError


def test_listed_neighbours_reads_the_series_once():
    extractor = Listing()
    assert extractor._listed_neighbours("https://example.com/s", "https://example.com/ep/2") == (
        "https://example.com/ep/1",
        "https://example.com/ep/3",
    )
    assert extractor._listed_neighbours("https://example.com/s", "https://example.com/ep/1") == (
        None,
        "https://example.com/ep/2",
    )
    assert extractor._listed_neighbours("https://example.com/s", "https://example.com/ep/9") == (None, None)
    assert extractor.listed == 1


def test_listed_neighbours_shrugs_at_a_series_it_cannot_list():
    extractor = Listing()
    assert extractor._listed_neighbours("https://example.com/gone", "https://example.com/ep/2") == (None, None)
    assert extractor._listed_neighbours("https://example.com/gone", "https://example.com/ep/2") == (None, None)
    assert extractor.listed == 1
