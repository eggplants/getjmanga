from __future__ import annotations

from io import BytesIO

import pytest
from httpx import HTTPStatusError
from PIL import Image

from getjmanga.errors import LoginError, UnsupportedUrlError
from getjmanga.extractor import Episode, Extractor, Page


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
        Plain(session)._get("https://example.com/")  # noqa: SLF001


def test_a_default_extractor_brings_its_own_session():
    assert Plain().session is not None


def test_episode_readable_means_it_has_pages():
    assert not Episode(url="u", series_title="s", episode_title="e").readable
    assert Episode(url="u", series_title="s", episode_title="e", pages=(Page(url="p"),)).readable


def test_cookie_picks_the_one_for_the_host(fake_session):
    plain = Plain(fake_session({}))
    plain.session.cookies.set("XSRF-TOKEN", "a", domain="example.com")
    plain.session.cookies.set("XSRF-TOKEN", "b", domain=".other.example")
    plain.session.cookies.set("other", "c", domain="example.com")

    assert plain._cookie("XSRF-TOKEN", "example.com") == "a"  # noqa: SLF001
    assert plain._cookie("XSRF-TOKEN", "www.other.example") == "b"  # noqa: SLF001
    assert plain._cookie("XSRF-TOKEN", "nowhere.example") is None  # noqa: SLF001
    assert plain._cookie("missing", "example.com") is None  # noqa: SLF001
