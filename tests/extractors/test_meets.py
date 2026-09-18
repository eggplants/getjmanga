from __future__ import annotations

from http import HTTPStatus
from io import BytesIO

import pytest
from PIL import Image

from getjmanga.downloader import Downloader
from getjmanga.extractors.common import NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractors.meets import Meets

BASE_URL = "https://manga-meets.jp"
DIR = "f12f7ca2-e87b-4056-bb66-d029ec20418f"
OTHER_DIR = "0f75526c-56d7-4e86-bd7a-8f01ea69eba6"
COMIC_URL = f"{BASE_URL}/comics/{DIR}"
EPISODE_URL = f"{COMIC_URL}/1"
LOCKED_URL = f"{COMIC_URL}/2"
LAST_URL = f"{COMIC_URL}/3"
PROFILE_URL = f"{BASE_URL}/profiles/Lechiyama"
CDN = "https://res.cloudinary.com/damtwm3df/image/upload"


def entry(sort_volume, volume, title=""):
    return {
        "id": f"830c8c6e-1e3d-4053-a1fc-5e1dcb0883d{sort_volume}",
        "type": "episode",
        "attributes": {
            "key": f"830c8c6e-1e3d-4053-a1fc-5e1dcb0883d{sort_volume}",
            "title": title,
            "volume": volume,
            "sort_volume": sort_volume,
            "page_count": 2,
            "last_episode": False,
            "private": False,
            "review_status": 1,
            "draft": False,
            "published_at": "2026-09-02T11:49:00.000+09:00",
            "view_count": 76,
        },
        "relationships": {"comic": {"data": {"id": "4b9ceeb8-4f49-495b-88be-06d94a7411aa", "type": "comic"}}},
    }


def comic(dir_name, title):
    return {
        "id": "4b9ceeb8-4f49-495b-88be-06d94a7411aa",
        "type": "comic",
        "attributes": {
            "dir_name": dir_name,
            "authors": ["檸檬山れち"],
            "title": title,
            "series": True,
            "locked": False,
        },
    }


# `/api/comics/<dir_name>/episodes.json`; the episodes arrive out of order on purpose.
EPISODES = {
    "data": [entry(3, "3話"), entry(1, "1話"), entry(2, "2話", "読み切り")],
    "included": [
        {"id": "26629229-d894-45b7-a3b2-b2b2e551bc45", "type": "comic_genre", "attributes": {"name": "恋愛"}},
        comic(DIR, "私だけのエモサマー"),
    ],
}


def image(name, index):
    return {
        "order_index": index,
        "is_spread_start_page": False,
        "image": {
            "pc_url": f"{CDN}/c_fill,f_auto,w_720/v1788282113/{name}.jpg",
            "sp_url": f"{CDN}/c_fill,f_auto,w_720/v1788282113/{name}.jpg",
            "thumbnail_url": f"{CDN}/c_fill,f_auto,w_200/v1788282113/{name}.jpg",
            "original_url": f"{CDN}/f_auto/v1788282113/{name}.jpg",
            "pc_geometry": {"width": 720, "height": 1018},
            "sp_geometry": {"width": 720, "height": 1018},
            "thumbnail_geometry": {"width": 200, "height": 282},
            "original_geometry": {"width": 7016, "height": 9921},
        },
    }


# `/api/comics/<dir_name>/episodes/1/viewer.json`; the pages arrive out of order on purpose.
VIEWER = {
    "sort_volume": 1,
    "volume": "1話",
    "title": "",
    "page_count": 2,
    "comic": {"title": "私だけのエモサマー", "authors": ["檸檬山れち"], "comic_page_promotions": []},
    "episode_viewer_setting": {"page_direction": "horizontal", "is_start_spread_page": False},
    "episode_publish_spans": [{"publish_start": "2020-01-01T00:00:00+0900", "publish_end": None}],
    "episode_pages": [image("second", 2), image("first", 1)],
}

# `/api/profiles/<short_name>/comics.json`, the works in the site's order.
PROFILE_COMICS = {
    "data": [
        comic(DIR, "私だけのエモサマー"),
        comic(OTHER_DIR, "ライバルにはなれない"),
        comic(DIR, "私だけのエモサマー"),
    ],
    "included": [],
}


def jpeg_bytes(color=(1, 2, 3)):
    raw = BytesIO()
    Image.new("RGB", (8, 8), color).save(raw, "JPEG")
    return raw.getvalue()


@pytest.fixture
def client(fake_session, fake_response):
    """A `Meets` over a session answering the listing, the viewer and the pages."""

    def build(extra=None):
        routes = {
            f"/api/comics/{DIR}/episodes.json": fake_response(payload=EPISODES),
            f"/api/comics/{OTHER_DIR}/episodes.json": fake_response(
                payload={"data": [entry(1, "1話")], "included": []}
            ),
            f"/api/comics/{DIR}/episodes/1/viewer.json": fake_response(payload=VIEWER),
            f"/api/comics/{DIR}/episodes/2/viewer.json": fake_response(
                payload={"code": "not_found"}, status_code=HTTPStatus.NOT_FOUND
            ),
            f"/api/comics/{DIR}/episodes/3/viewer.json": fake_response(payload={**VIEWER, "episode_pages": []}),
            "/api/profiles/Lechiyama/comics.json": fake_response(payload=PROFILE_COMICS),
            "/api/comics/nope/": fake_response(payload={"code": "not_found"}, status_code=HTTPStatus.NOT_FOUND),
            "/api/profiles/nope/": fake_response(
                payload={"code": "profile-not-found"}, status_code=HTTPStatus.NOT_FOUND
            ),
            "/first.jpg": fake_response(jpeg_bytes(), content_type="image/jpeg"),
            "/second.jpg": fake_response(jpeg_bytes((250, 0, 0)), content_type="image/jpeg"),
        }
        session = fake_session({**routes, **(extra or {})})
        return Meets(session), session

    return build


# --- URLs ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        EPISODE_URL,
        f"{EPISODE_URL}/",
        f"{EPISODE_URL}?foo=bar",
        COMIC_URL,
        f"{COMIC_URL}/",
        PROFILE_URL,
        f"{PROFILE_URL}/",
        f"https://challenge-mee.manga-meets.jp/comics/{DIR}/1",
        f"https://challenge-mee.manga-meets.jp/comics/{DIR}",
    ],
)
def test_suitable_accepts_episode_work_and_profile_urls(url):
    assert Meets.suitable(url)


@pytest.mark.parametrize(
    "url",
    [
        f"http://manga-meets.jp/comics/{DIR}/1",
        f"https://manga-mee.jp/comics/{DIR}/1",
        f"https://www.manga-meets.jp/comics/{DIR}/1",
        f"{BASE_URL}/",
        f"{BASE_URL}/comics/categories/love",
        f"{COMIC_URL}/one",
        f"{BASE_URL}/contests/mee/spring/comics/{DIR}",
        f"{BASE_URL}/editors/mee_miura",
    ],
)
def test_suitable_rejects_other_urls(url):
    assert not Meets.suitable(url)


@pytest.mark.parametrize(
    ("url", "expected"),
    [(COMIC_URL, True), (f"{COMIC_URL}/", True), (PROFILE_URL, True), (EPISODE_URL, False), (f"{BASE_URL}/", False)],
)
def test_is_series(url, expected):
    assert Meets.is_series(url) is expected


# --- episodes --------------------------------------------------------------------------


def test_episode_reads_the_titles_the_pages_and_the_next(client):
    meets, session = client()
    episode = meets.episode(f"{EPISODE_URL}/")

    assert episode.url == EPISODE_URL
    assert episode.series_title == "私だけのエモサマー"
    assert episode.episode_title == "1話"
    assert [page.url for page in episode.pages] == [
        f"{CDN}/c_fill,f_auto,w_720/v1788282113/first.jpg",
        f"{CDN}/c_fill,f_auto,w_720/v1788282113/second.jpg",
    ]
    assert (episode.pages[0].width, episode.pages[0].height) == (720, 1018)
    assert episode.next_url == LOCKED_URL
    assert episode.metadata["episode"]["sort_volume"] == 1
    assert episode.metadata["viewer"] is VIEWER
    assert session.calls == [
        f"{BASE_URL}/api/comics/{DIR}/episodes.json",
        f"{BASE_URL}/api/comics/{DIR}/episodes/1/viewer.json",
    ]
    assert all(headers["Accept"] == "application/json" for headers in session.headers_seen)
    assert all(headers["Referer"] == f"{EPISODE_URL}/" for headers in session.headers_seen)


def test_episode_keeps_the_site_the_url_named(client):
    meets, session = client()
    episode = meets.episode(f"https://challenge-mee.manga-meets.jp/comics/{DIR}/1")

    assert episode.url == f"https://challenge-mee.manga-meets.jp/comics/{DIR}/1"
    assert episode.next_url == f"https://challenge-mee.manga-meets.jp/comics/{DIR}/2"
    assert session.calls[0].startswith("https://challenge-mee.manga-meets.jp/api/")


def test_episode_names_the_episode_after_the_volume(client):
    meets, _ = client()
    assert meets.episode(LOCKED_URL).episode_title == "2話 読み切り"


def test_withheld_viewer_means_locked(client):
    meets, _ = client()
    episode = meets.episode(LOCKED_URL)

    assert episode.pages == ()
    assert episode.next_url == LAST_URL
    assert episode.series_title == "私だけのエモサマー"
    assert episode.metadata["viewer"] is None


def test_viewer_without_pages_means_locked_and_the_last_has_no_next(client):
    meets, _ = client()
    episode = meets.episode(LAST_URL)

    assert episode.pages == ()
    assert episode.next_url is None


def test_unknown_episode_is_not_an_episode_page(client):
    meets, _ = client()
    with pytest.raises(NotAnEpisodePageError, match="no episode 9"):
        meets.episode(f"{COMIC_URL}/9")


def test_unknown_work_is_not_an_episode_page(client):
    meets, _ = client()
    with pytest.raises(NotAnEpisodePageError, match="no work nope"):
        meets.episode(f"{BASE_URL}/comics/nope/1")


def test_episode_rejects_a_work_url(client):
    meets, _ = client()
    with pytest.raises(UnsupportedUrlError):
        meets.episode(COMIC_URL)


# --- series ----------------------------------------------------------------------------


def test_series_urls_lists_a_work_first_episode_first(client):
    meets, _ = client()
    assert meets.series_urls(f"{COMIC_URL}/") == [EPISODE_URL, LOCKED_URL, LAST_URL]


def test_series_urls_walks_every_work_of_a_profile_without_repeats(client):
    meets, _ = client()
    assert meets.series_urls(PROFILE_URL) == [
        EPISODE_URL,
        LOCKED_URL,
        LAST_URL,
        f"{BASE_URL}/comics/{OTHER_DIR}/1",
    ]


def test_series_urls_raises_on_an_empty_listing(client, fake_response):
    meets, _ = client({f"/api/comics/{DIR}/episodes.json": fake_response(payload={"data": []})})
    with pytest.raises(NotAnEpisodePageError):
        meets.series_urls(COMIC_URL)


def test_series_urls_raises_on_an_unknown_work_or_profile(client):
    meets, _ = client()
    with pytest.raises(NotAnEpisodePageError, match="no work nope"):
        meets.series_urls(f"{BASE_URL}/comics/nope")
    with pytest.raises(NotAnEpisodePageError, match="no author nope"):
        meets.series_urls(f"{BASE_URL}/profiles/nope")


def test_series_urls_rejects_an_episode_url(client):
    meets, _ = client()
    with pytest.raises(UnsupportedUrlError):
        meets.series_urls(EPISODE_URL)


# --- images and the download -----------------------------------------------------------


def test_image_sends_the_episode_as_referer(client):
    meets, session = client()
    episode = meets.episode(EPISODE_URL)

    pixel = meets.image(episode.pages[1], episode).getpixel((0, 0))
    assert isinstance(pixel, tuple)
    assert pixel[0] > 200
    assert session.headers_seen[-1]["Referer"] == EPISODE_URL


# --- the real site ---------------------------------------------------------------------

# One episode per known host, free to read without an account.
TEST_URLS: dict[str, str] = {
    "manga-meets.jp": "https://manga-meets.jp/comics/5f7bcdf8-22b6-4d8b-93ce-a9b9b05c2e25/1",
    "challenge-mee.manga-meets.jp": "https://challenge-mee.manga-meets.jp/comics/5f7bcdf8-22b6-4d8b-93ce-a9b9b05c2e25/1",
}


@pytest.mark.network
@pytest.mark.parametrize("host", TEST_URLS)
def test_site_download(tmp_path, host):
    result = Downloader(Meets(), tmp_path, only_first=True).download(TEST_URLS[host])
    assert result.status == "saved"
    assert (result.save_dir / "0.jpg").stat().st_size > 0
    assert result.episode.next_url == TEST_URLS[host][:-1] + "2"


@pytest.mark.network
def test_work_page_lists_episodes():
    urls = Meets().series_urls("https://manga-meets.jp/comics/5f7bcdf8-22b6-4d8b-93ce-a9b9b05c2e25")
    assert urls[0] == TEST_URLS["manga-meets.jp"]
    assert len(urls) > 1
    assert all(Meets.suitable(url) for url in urls)


@pytest.mark.network
def test_profile_page_lists_works():
    urls = Meets().series_urls("https://manga-meets.jp/profiles/Lechiyama")
    assert "https://manga-meets.jp/comics/f12f7ca2-e87b-4056-bb66-d029ec20418f/1" in urls
    assert all(Meets.suitable(url) for url in urls)
