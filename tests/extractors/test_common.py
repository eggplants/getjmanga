from __future__ import annotations

import importlib.util
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image
from requests import HTTPError

from getjmanga.extractors import (
    EXTRACTORS,
    Comici,
    GigaViewer,
    Piccoma,
    UnknownExtractorError,
    UnsupportedUrlError,
    find_extractor,
    get_extractor,
)
from getjmanga.extractors.common import Episode, Extractor, LoginError, Page


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
    with pytest.raises(HTTPError):
        Plain(session)._get("https://example.com/")  # noqa: SLF001


def test_a_default_extractor_brings_its_own_session():
    assert Plain().session is not None


def test_episode_readable_means_it_has_pages():
    assert not Episode(url="u", series_title="s", episode_title="e").readable
    assert Episode(url="u", series_title="s", episode_title="e", pages=(Page(url="p"),)).readable


# --- the registry ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://shonenjumpplus.com/episode/13932016480028799982", GigaViewer),
        ("https://takecomic.jp/episodes/74f33031e13cd", Comici),
        ("https://piccoma.com/web/viewer/8195/1185884", Piccoma),
    ],
)
def test_find_extractor_picks_by_url(url, expected):
    assert find_extractor(url) is expected


def test_find_extractor_rejects_an_unknown_site():
    with pytest.raises(UnsupportedUrlError, match="no extractor takes"):
        find_extractor("https://example.com/episodes/1")


def test_get_extractor_looks_up_by_name():
    for extractor in EXTRACTORS:
        assert get_extractor(extractor.NAME) is extractor


def test_get_extractor_rejects_an_unknown_name():
    with pytest.raises(UnknownExtractorError, match="pick one of"):
        get_extractor("nope")


def test_every_known_host_has_a_site_test():
    """Each host of each extractor is named once in a test module's `TEST_URLS`, by a URL that extractor takes."""
    tested = {}
    for path in sorted(Path(__file__).parent.glob("test_*.py")):
        spec = importlib.util.spec_from_file_location(path.stem, path)
        assert spec is not None
        assert spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for key, url in getattr(module, "TEST_URLS", {}).items():
            tested[key.split("/", 1)[0]] = url  # `host/imprint` keys share one host

    assert set(tested) == {host for extractor in EXTRACTORS for host in extractor.HOSTS}
    for host, url in tested.items():
        assert host in find_extractor(url).HOSTS, url


def test_cookie_picks_the_one_for_the_host(fake_session):
    plain = Plain(fake_session({}))
    plain.session.cookies.set("XSRF-TOKEN", "a", domain="example.com")
    plain.session.cookies.set("XSRF-TOKEN", "b", domain=".other.example")
    plain.session.cookies.set("other", "c", domain="example.com")

    assert plain._cookie("XSRF-TOKEN", "example.com") == "a"  # noqa: SLF001
    assert plain._cookie("XSRF-TOKEN", "www.other.example") == "b"  # noqa: SLF001
    assert plain._cookie("XSRF-TOKEN", "nowhere.example") is None  # noqa: SLF001
    assert plain._cookie("missing", "example.com") is None  # noqa: SLF001
