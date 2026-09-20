from __future__ import annotations

import json

import pytest
from PIL import Image

from getjmanga.downloader import Downloader
from getjmanga.extractor import Episode, Extractor, Page


class Canned(Extractor):
    """Hands out a fixed episode and paints every page a flat colour."""

    NAME = "canned"
    HOSTS = ("example.com",)

    def __init__(self, episode, mode="RGB"):
        super().__init__()
        self._episode = episode
        self.mode = mode
        self.fetched = []

    def episode(self, url):
        return self._episode

    def image(self, page, episode):
        self.fetched.append(page.url)
        return Image.new(self.mode, (4, 4), 0 if self.mode == "L" else (10, 20, 30, 40)[: len(self.mode)])


def episode(pages=3, series_title="Series", episode_title="Episode 1"):
    return Episode(
        url="https://example.com/ep/1",
        series_title=series_title,
        episode_title=episode_title,
        pages=tuple(Page(url=f"https://cdn.example/{n}.jpg", extra={"n": n}) for n in range(pages)),
        next_url="https://example.com/ep/2",
        metadata={"raw": True},
    )


def test_download_writes_every_page_under_series_and_episode(tmp_path):
    result = Downloader(Canned(episode()), tmp_path).download("https://example.com/ep/1")

    assert result.status == "saved"
    assert result.saved
    assert result.save_dir == tmp_path / "Series" / "Episode 1"
    assert sorted(path.name for path in result.save_dir.iterdir()) == ["0.jpg", "1.jpg", "2.jpg"]


def test_download_pads_page_numbers_to_the_page_count(tmp_path):
    result = Downloader(Canned(episode(pages=11)), tmp_path).download("u")
    names = sorted(path.name for path in result.save_dir.iterdir())
    assert names[0] == "00.jpg"
    assert names[-1] == "10.jpg"


def test_download_stops_after_the_first_page_when_asked(tmp_path):
    extractor = Canned(episode())
    result = Downloader(extractor, tmp_path, only_first=True).download("u")
    assert [path.name for path in result.save_dir.iterdir()] == ["0.jpg"]
    assert extractor.fetched == ["https://cdn.example/0.jpg"]


def test_download_leaves_an_existing_directory_alone(tmp_path):
    (tmp_path / "Series" / "Episode 1").mkdir(parents=True)
    extractor = Canned(episode())

    result = Downloader(extractor, tmp_path).download("u")

    assert result.status == "exists"
    assert not result.saved
    assert extractor.fetched == []
    # The episode was still read, so a bulk run knows where to go next.
    assert result.episode.next_url == "https://example.com/ep/2"


def test_download_writes_again_when_told_to(tmp_path):
    (tmp_path / "Series" / "Episode 1").mkdir(parents=True)
    result = Downloader(Canned(episode()), tmp_path, overwrite=True).download("u")
    assert result.status == "saved"


def test_download_reports_an_episode_without_pages_as_locked(tmp_path):
    result = Downloader(Canned(episode(pages=0)), tmp_path).download("u")

    assert result.status == "locked"
    assert not result.save_dir.exists()


def test_download_writes_metadata_when_asked(tmp_path):
    result = Downloader(Canned(episode()), tmp_path, save_metadata=True).download("u")

    metadata = json.loads((result.save_dir / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["series_title"] == "Series"
    assert metadata["next_url"] == "https://example.com/ep/2"
    assert metadata["metadata"] == {"raw": True}
    assert [page["extra"]["n"] for page in metadata["pages"]] == [0, 1, 2]


@pytest.mark.parametrize("mode", ["RGBA", "P", "L"])
def test_download_saves_any_image_mode_as_jpeg(tmp_path, mode):
    result = Downloader(Canned(episode(pages=1), mode=mode), tmp_path).download("u")
    with Image.open(result.save_dir / "0.jpg") as saved:
        assert saved.format == "JPEG"


def test_titles_are_made_safe_for_the_file_system(tmp_path):
    result = Downloader(Canned(episode(series_title="A/B: C?", episode_title="1/2")), tmp_path).download("u")
    assert result.save_dir == tmp_path / "A／B C" / "1／2"


def test_an_empty_title_still_gets_a_directory(tmp_path):
    result = Downloader(Canned(episode(series_title="", episode_title="?")), tmp_path).download("u")
    assert result.save_dir == tmp_path / "_" / "_"
