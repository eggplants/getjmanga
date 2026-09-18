from __future__ import annotations

from getjmanga import EXTRACTORS, __version__


def test_version_is_available():
    assert __version__


def test_every_extractor_is_named_and_lists_hosts():
    names = [extractor.NAME for extractor in EXTRACTORS]
    assert len(names) == len(set(names))
    for extractor in EXTRACTORS:
        assert extractor.NAME
        assert extractor.HOSTS
        assert extractor.URL_FORMS
