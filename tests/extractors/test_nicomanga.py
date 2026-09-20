from __future__ import annotations

from http import HTTPStatus
from io import BytesIO

import pytest
from PIL import Image

from getjmanga.downloader import Downloader
from getjmanga.errors import NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractors.nicomanga import API_URL, BASE_URL, NicoManga, unmask

SERIES_URL = f"{BASE_URL}/comic/30941"
EPISODE_URL = f"{BASE_URL}/watch/mg276964"
LOCKED_URL = f"{BASE_URL}/watch/mg1131439"
FINISHED_URL = f"{BASE_URL}/watch/mg489461"
DRM_HASH = "4dd530fbba62f25d12d39afeac63cb7d4a30f0fe_10357"
DRM_URL = f"https://drm.cdn.nicomanga.jp/image/{DRM_HASH}/7522492p.webp?1565699880"
PLAIN_URL = "https://deliver.cdn.nicomanga.jp/thumb/aHR0cHM6Ly9zYXBp.webp"


def api(result):
    return {"meta": {"status": 200}, "data": {"result": result}}


def refusal(code, status=HTTPStatus.FORBIDDEN):
    return {"meta": {"status": int(status), "error_code": code}, "data": None}


def episode_entry(episode_id, title, sell_status="free", content_id=30941):
    return {
        "id": episode_id,
        "meta": {
            "content_id": content_id,
            "title": title,
            "number": 1,
            "share_url": f"{BASE_URL}/watch/mg{episode_id}",
            "price": 0,
            "is_playable": True,
        },
        "own_status": {"sell_status": sell_status},
    }


def frame(frame_id, url, drm_hash=None, width=650, height=924):
    return {
        "id": frame_id,
        "meta": {"width": width, "height": height, "is_spread": False, "source_url": url, "drm_hash": drm_hash},
    }


# `contents/30941`, cut down to what is read.
CONTENT = {
    "id": 30941,
    "meta": {
        "title": "異世界のんびり農家",
        "display_author_name": "剣康之(作画) 内藤騎之介(原作)",
        "content_type": "official",
        "share_url": SERIES_URL,
    },
}
# `contents/30941/episodes`.
LISTING = [
    episode_entry(276964, "第1話"),
    episode_entry(278075, "第2話"),
    episode_entry(1131439, "第331話", sell_status="selling"),
    episode_entry(489461, "告知イラスト⑭", sell_status="publication_finished"),
]
FRAMES = [frame(2907472, DRM_URL, DRM_HASH), frame(2907473, PLAIN_URL)]


def webp_bytes(color=(1, 2, 3), size=(8, 8)):
    raw = BytesIO()
    Image.new("RGB", size, color).save(raw, "WEBP", lossless=True)
    return raw.getvalue()


@pytest.fixture
def client(fake_session, fake_response):
    """A `NicoManga` over a session answering the app API and both CDNs."""

    def build(extra=None):
        routes = {
            f"{API_URL}/episodes/276964/frames": fake_response(payload=api(FRAMES)),
            f"{API_URL}/episodes/276964": fake_response(payload=api(LISTING[0])),
            f"{API_URL}/episodes/278075/frames": fake_response(payload=api([frame(1, PLAIN_URL)])),
            f"{API_URL}/episodes/278075": fake_response(payload=api(LISTING[1])),
            f"{API_URL}/episodes/1131439/frames": fake_response(payload=refusal("NOT_PURCHASED"), status_code=403),
            f"{API_URL}/episodes/1131439": fake_response(payload=api(LISTING[2])),
            f"{API_URL}/episodes/489461": fake_response(payload=refusal("PUBLICATION_FINISHED"), status_code=403),
            f"{API_URL}/episodes/1": fake_response(payload=refusal("NOT_FOUND", 404), status_code=404),
            f"{API_URL}/contents/30941/episodes": fake_response(payload=api(LISTING)),
            f"{API_URL}/contents/30941": fake_response(payload=api(CONTENT)),
            "drm.cdn.nicomanga.jp": fake_response(
                unmask(webp_bytes(), DRM_HASH), content_type="application/octet-stream"
            ),
            "deliver.cdn.nicomanga.jp": fake_response(webp_bytes((7, 8, 9)), content_type="image/webp"),
        }
        # `extra` goes first (more specific routes must win the substring match) and overrides.
        merged = dict(extra or {})
        for needle, response in routes.items():
            merged.setdefault(needle, response)
        session = fake_session(merged)
        return NicoManga(session), session

    return build


# --- URLs ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        EPISODE_URL,
        f"{EPISODE_URL}/",
        "https://sp.manga.nicovideo.jp/watch/mg276964",
        "https://seiga.nicovideo.jp/watch/mg276964",
        SERIES_URL,
        f"{SERIES_URL}/",
        "https://sp.manga.nicovideo.jp/comic/30941",
        "https://seiga.nicovideo.jp/comic/30941",
    ],
)
def test_suitable_accepts_episode_and_series_urls(url):
    assert NicoManga.suitable(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://manga.nicovideo.jp/watch/mg276964",
        "https://manga.nicovideo.jp/",
        "https://manga.nicovideo.jp/watch/276964",
        "https://manga.nicovideo.jp/watch/sm9",
        "https://manga.nicovideo.jp/official/dradraflat",
        "https://manga.nicovideo.jp/manga/list?category=other",
        "https://www.nicovideo.jp/watch/sm9",
        "https://seiga.nicovideo.jp/seiga/im1",
        "https://comic-walker.com/detail/KC_002492_S",
    ],
)
def test_suitable_rejects_other_urls(url):
    assert not NicoManga.suitable(url)


@pytest.mark.parametrize(
    ("url", "expected"),
    [(EPISODE_URL, False), (SERIES_URL, True), ("https://sp.manga.nicovideo.jp/comic/30941/", True)],
)
def test_is_series(url, expected):
    assert NicoManga.is_series(url) is expected


# --- unmasking -------------------------------------------------------------------------


def test_unmask_matches_the_real_header():
    # The first twelve bytes of a real masked frame, whose plain form is `RIFF<size>WEBP`.
    masked = bytes.fromhex("1f9c76bd6075f35d1a9072ab")
    assert unmask(masked, DRM_HASH)[:4] == b"RIFF"
    assert unmask(masked, DRM_HASH)[8:12] == b"WEBP"


# --- episodes --------------------------------------------------------------------------


def test_episode_reads_the_titles_the_pages_and_the_next_episode(client):
    nico, session = client()
    episode = nico.episode(EPISODE_URL)

    assert episode.url == EPISODE_URL
    assert episode.series_title == "異世界のんびり農家"
    assert episode.episode_title == "第1話"
    assert [page.url for page in episode.pages] == [DRM_URL, PLAIN_URL]
    assert episode.pages[0].extra == {"drm_hash": DRM_HASH}
    assert episode.pages[1].extra == {"drm_hash": None}
    assert (episode.pages[0].width, episode.pages[0].height) == (650, 924)
    assert episode.next_url == f"{BASE_URL}/watch/mg278075"
    assert episode.metadata["frames"] == FRAMES
    assert episode.metadata["content"]["title"] == "異世界のんびり農家"
    assert episode.metadata["error_code"] is None

    assert session.calls == [
        f"{API_URL}/episodes/276964",
        f"{API_URL}/contents/30941",
        f"{API_URL}/contents/30941/episodes",
        f"{API_URL}/episodes/276964/frames",
    ]
    assert session.params_seen[-1] == {"enable_webp": "true"}
    assert all(headers["Referer"] == EPISODE_URL for headers in session.headers_seen)
    assert session.headers_seen[-1]["Accept"] == "application/json"


@pytest.mark.parametrize(
    "url",
    [
        f"{EPISODE_URL}/",
        "https://sp.manga.nicovideo.jp/watch/mg276964",
        "https://seiga.nicovideo.jp/watch/mg276964",
    ],
)
def test_episode_accepts_every_host_and_answers_the_canonical_url(client, url):
    nico, _ = client()
    episode = nico.episode(url)
    assert episode.url == EPISODE_URL
    assert len(episode.pages) == 2


def test_locked_episode_has_no_pages_but_keeps_its_titles_and_next(client):
    nico, _ = client()
    episode = nico.episode(LOCKED_URL)

    assert episode.pages == ()
    assert not episode.readable
    assert episode.series_title == "異世界のんびり農家"
    assert episode.episode_title == "第331話"
    assert episode.next_url == FINISHED_URL
    assert episode.metadata["frames"] is None
    assert episode.metadata["error_code"] == "NOT_PURCHASED"


def test_finished_episode_is_locked_and_named_by_a_cached_listing(client):
    nico, _ = client()
    # Standalone the API refuses to describe it at all: locked, with placeholder titles.
    alone = nico.episode(FINISHED_URL)
    assert alone.pages == ()
    assert alone.episode_title == "mg489461"
    assert alone.next_url is None
    assert alone.metadata["error_code"] == "PUBLICATION_FINISHED"

    # Walked into from an earlier episode, the listing names it.
    nico.episode(EPISODE_URL)
    walked = nico.episode(FINISHED_URL)
    assert walked.pages == ()
    assert walked.series_title == "異世界のんびり農家"
    assert walked.episode_title == "告知イラスト⑭"
    assert walked.next_url is None


def test_last_episode_has_no_next(client, fake_response):
    nico, _ = client(
        {
            f"{API_URL}/episodes/489461/frames": fake_response(payload=api([frame(1, PLAIN_URL)])),
            f"{API_URL}/episodes/489461": fake_response(payload=api(LISTING[3])),
        }
    )
    episode = nico.episode(FINISHED_URL)
    assert episode.episode_title == "告知イラスト⑭"
    assert len(episode.pages) == 1
    assert episode.next_url is None


def test_unknown_episode_is_not_an_episode_page(client):
    nico, _ = client()
    with pytest.raises(NotAnEpisodePageError, match="no episode mg1 "):
        nico.episode(f"{BASE_URL}/watch/mg1")


def test_episode_without_json_is_not_an_episode_page(fake_session, fake_response):
    session = fake_session({f"{API_URL}/episodes/5": fake_response(text="<html>maintenance</html>", status_code=404)})
    with pytest.raises(NotAnEpisodePageError, match="no episode mg5"):
        NicoManga(session).episode(f"{BASE_URL}/watch/mg5")


def test_episode_rejects_a_series_url(client):
    nico, _ = client()
    with pytest.raises(UnsupportedUrlError):
        nico.episode(SERIES_URL)


def test_api_failure_raises(fake_session, fake_response):
    session = fake_session({f"{API_URL}/episodes/5": fake_response(text="", status_code=503)})
    with pytest.raises(Exception, match="HTTP 503"):
        NicoManga(session).episode(f"{BASE_URL}/watch/mg5")


# --- series ----------------------------------------------------------------------------


@pytest.mark.parametrize("url", [SERIES_URL, f"{SERIES_URL}/", "https://sp.manga.nicovideo.jp/comic/30941"])
def test_series_urls_lists_the_episodes_in_order(client, url):
    nico, _ = client()
    urls = nico.series_urls(url)
    assert urls == [EPISODE_URL, f"{BASE_URL}/watch/mg278075", LOCKED_URL, FINISHED_URL]
    assert all(NicoManga.suitable(candidate) for candidate in urls)


def test_series_urls_drops_a_repeated_entry(fake_session, fake_response):
    twice = [
        episode_entry(5, "a", content_id=1),
        episode_entry(5, "a", content_id=1),
        episode_entry(6, "b", content_id=1),
    ]
    session = fake_session({f"{API_URL}/contents/1/episodes": fake_response(payload=api(twice))})
    assert NicoManga(session).series_urls(f"{BASE_URL}/comic/1") == [f"{BASE_URL}/watch/mg5", f"{BASE_URL}/watch/mg6"]


def test_series_urls_rejects_an_episode_url(client):
    nico, _ = client()
    with pytest.raises(UnsupportedUrlError):
        nico.series_urls(EPISODE_URL)


@pytest.mark.parametrize(
    "answer",
    [api([]), refusal("NOT_FOUND", 404)],
)
def test_series_urls_raises_on_an_empty_or_unknown_listing(fake_session, fake_response, answer):
    status = answer["meta"]["status"]
    session = fake_session({f"{API_URL}/contents/1/episodes": fake_response(payload=answer, status_code=status)})
    with pytest.raises(NotAnEpisodePageError, match="lists no episode"):
        NicoManga(session).series_urls(f"{BASE_URL}/comic/1")


# --- images ----------------------------------------------------------------------------


def test_image_unmasks_a_drm_frame(client):
    nico, session = client()
    episode = nico.episode(EPISODE_URL)
    image = nico.image(episode.pages[0], episode)

    assert image.convert("RGB").getpixel((0, 0)) == (1, 2, 3)
    assert session.calls[-1] == DRM_URL
    assert session.headers_seen[-1]["Referer"] == EPISODE_URL
    assert session.headers_seen[-1]["Accept"].startswith("image/webp")
    assert "Origin" not in session.headers_seen[-1]


def test_image_leaves_a_plain_frame_alone(client):
    nico, _ = client()
    episode = nico.episode(EPISODE_URL)
    assert nico.image(episode.pages[1], episode).convert("RGB").getpixel((0, 0)) == (7, 8, 9)


# --- logging in ------------------------------------------------------------------------


# --- the real site ---------------------------------------------------------------------

# One free episode per known host: the first episode of a long-running official
# series (DRM-masked frames) and of a user-posted one (plain frames).
TEST_URLS: dict[str, str] = {
    "manga.nicovideo.jp": "https://manga.nicovideo.jp/watch/mg276964",
    "sp.manga.nicovideo.jp": "https://sp.manga.nicovideo.jp/watch/mg1130489",
    "seiga.nicovideo.jp": "https://seiga.nicovideo.jp/watch/mg10940",
}


@pytest.mark.network
@pytest.mark.parametrize("host", TEST_URLS)
def test_site_download(tmp_path, host):
    result = Downloader(NicoManga(), tmp_path, only_first=True).download(TEST_URLS[host])
    assert result.status == "saved"
    assert (result.save_dir / "0.jpg").stat().st_size > 0


@pytest.mark.network
def test_site_locked_episode_has_no_pages():
    episode = NicoManga().episode(LOCKED_URL)
    assert not episode.readable
    assert episode.series_title == "異世界のんびり農家"
    assert episode.episode_title == "第331話"
    assert episode.metadata["error_code"] == "NOT_PURCHASED"


@pytest.mark.network
def test_site_series_lists_episodes():
    urls = NicoManga().series_urls(SERIES_URL)
    assert urls[0] == EPISODE_URL
    assert LOCKED_URL in urls
    assert all(NicoManga.suitable(url) for url in urls)
