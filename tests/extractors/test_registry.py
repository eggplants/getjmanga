from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from getjmanga.errors import UnknownExtractorError, UnsupportedUrlError
from getjmanga.extractors import EXTRACTORS, Comici, GigaViewer, Piccoma, find_extractor, get_extractor


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
