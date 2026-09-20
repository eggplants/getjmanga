from __future__ import annotations

from http import HTTPStatus
from io import BytesIO

import pytest
from PIL import Image

from getjmanga.cipher import xor_unmask
from getjmanga.downloader import Downloader
from getjmanga.errors import NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractors.comicwalker import (
    API_URL,
    BASE_URL,
    IMAGE_SIZE,
    ComicWalker,
    episode_title,
)

WORK_URL = f"{BASE_URL}/detail/KC_001981_S"
EPISODE_URL = f"{BASE_URL}/detail/KC_001981_S/episodes/KC_0019810000100011_E"
SECOND_URL = f"{BASE_URL}/detail/KC_001981_S/episodes/KC_0019810000200011_E"
LOCKED_URL = f"{BASE_URL}/detail/KC_001981_S/episodes/KC_0019810000300011_E"
CDN = "https://cdn.comic-walker.com/images/1981/1/1/20240926/1"
NOT_FOUND_HTML = '<!DOCTYPE html><html lang="ja"><head><meta property="og:url" content="https://comic-walker.com/404"/>'


def entry(no, code, title, *, active=True, subtitle=""):
    return {
        "id": f"018d6d33-0000-7000-8000-{no:012d}",
        "code": code,
        "title": title,
        "subTitle": subtitle,
        "updateDate": "2024-09-26T02:00:00Z",
        "deliveryPeriod": "9999-12-31T14:59:59Z" if active else "2025-02-05T02:00:00Z",
        "isNew": False,
        "hasRead": False,
        "stores": [],
        "serviceId": "web",
        "internal": {"episodeNo": no, "pageCount": 2, "episodetype": "normal"},
        "type": "normal",
        "isActive": active,
    }


ENTRIES = [
    entry(1, "KC_0019810000100011_E", "第一話"),
    entry(2, "KC_0019810000200011_E", "第二話"),
    entry(3, "KC_0019810000300011_E", "第三話", active=False),
]

# The site's answer to `/api/contents/details/work`, cut down to what is read.
WORK = {
    "work": {"id": "018d6d33-0000-7000-8000-000000000000", "code": "KC_001981_S", "title": "月華国奇医伝"},
    "followerCount": 0,
    "firstEpisodes": {"total": 3, "result": ENTRIES},
    "latestEpisodes": {"total": 3, "result": list(reversed(ENTRIES))},
    "latestEpisodeId": ENTRIES[-1]["id"],
}


def manuscript(page, drm_hash, mode="xor"):
    return {
        "drmMode": mode,
        "drmHash": drm_hash,
        "drmImageUrl": f"{CDN}/{page}_abcdef.webp?Policy=eyJ&Signature=sig&Key-Pair-Id=K1",
        "page": page,
        "width": 1114,
        "height": 1600,
    }


# `/api/contents/viewer` for a readable episode; the pages arrive out of order on purpose.
MANUSCRIPTS = [manuscript(2, "d8114042de6da1a4"), manuscript(1, "62e07285b272877b")]
VIEWER = {
    "promotionsEnd": [],
    "labelLogo": "https://cdn.comic-walker.com/library/assets/logo.jpg",
    "scrollDirection": "rtl",
    "expiresAt": "2026-09-17T19:57:02.598022859Z",
    "startPosition": "left",
    "displayAds": True,
    "manuscripts": MANUSCRIPTS,
}
LOCKED_VIEWER = {**VIEWER, "manuscripts": []}


def webp_bytes(color=(1, 2, 3), size=(8, 8)):
    raw = BytesIO()
    Image.new("RGB", size, color).save(raw, "WEBP", lossless=True)
    return raw.getvalue()


@pytest.fixture
def client(fake_session, fake_response):
    """A `ComicWalker` over a session answering the work, the viewer and two masked pages.

    The viewer is asked for by `episodeId` in the query, so the routes here
    match on the URL with its query string appended.
    """

    class QuerySession(fake_session):
        def get(self, url, **kwargs):
            params = kwargs.get("params") or {}
            query = "&".join(f"{key}={value}" for key, value in params.items())
            self.calls.append(url)
            self.params_seen.append(kwargs.get("params"))
            self.headers_seen.append(kwargs.get("headers") or {})
            return self._route(f"{url}?{query}" if query else url)

    def build(extra=None):
        routes = {
            f"{API_URL}/details/work": fake_response(payload=WORK),
            f"{API_URL}/viewer?episodeId={ENTRIES[0]['id']}": fake_response(payload=VIEWER),
            f"{API_URL}/viewer?episodeId={ENTRIES[1]['id']}": fake_response(payload=VIEWER),
            f"{API_URL}/viewer?episodeId={ENTRIES[2]['id']}": fake_response(payload=LOCKED_VIEWER),
            f"{CDN}/1_": fake_response(xor_unmask(webp_bytes(), "62e07285b272877b"), content_type="image/webp"),
            f"{CDN}/2_": fake_response(
                xor_unmask(webp_bytes((4, 5, 6)), "d8114042de6da1a4"), content_type="image/webp"
            ),
        }
        session = QuerySession({**routes, **(extra or {})})
        return ComicWalker(session), session

    return build


# --- URLs ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        EPISODE_URL,
        f"{EPISODE_URL}/",
        f"{EPISODE_URL}?co=true",
        WORK_URL,
        f"{WORK_URL}/",
        "https://comic-walker.com/detail/KC_020034_S/episodes/KC_0200340000200011_E",
    ],
)
def test_suitable_accepts_episode_and_work_urls(url):
    assert ComicWalker.suitable(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://comic-walker.com/detail/KC_001981_S/episodes/KC_0019810000100011_E",
        "https://comic-walker.com/",
        "https://comic-walker.com/free",
        "https://comic-walker.com/detail/",
        "https://comic-walker.com/contents/detail/KC_001981_S",
        "https://comic-walker.com/label/asuka",
        "https://comic-walker.com/search/genre/018a178c-a909-7ad4-8d88-e4f324c36061",
        "https://cdn.comic-walker.com/detail/KC_001981_S",
        "https://piccoma.com/web/viewer/8195/1185884",
    ],
)
def test_suitable_rejects_other_urls(url):
    assert not ComicWalker.suitable(url)


@pytest.mark.parametrize(
    ("url", "expected"),
    [(EPISODE_URL, False), (WORK_URL, True), (f"{WORK_URL}/", True)],
)
def test_is_series(url, expected):
    assert ComicWalker.is_series(url) is expected


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ({"title": "第一話", "subTitle": ""}, "第一話"),
        ({"title": "SCENE-1", "subTitle": "出会い"}, "SCENE-1 出会い"),
        ({"title": None, "code": "KC_1_E"}, "KC_1_E"),
    ],
)
def test_episode_title(data, expected):
    assert episode_title(data) == expected


# --- episodes --------------------------------------------------------------------------


def test_episode_reads_the_titles_the_pages_and_the_next_episode(client):
    comicwalker, session = client()
    episode = comicwalker.episode(EPISODE_URL)

    assert episode.url == EPISODE_URL
    assert episode.series_title == "月華国奇医伝"
    assert episode.episode_title == "第一話"
    # Pages come back in `page` order, whatever order the site listed them in.
    assert [page.url for page in episode.pages] == [MANUSCRIPTS[1]["drmImageUrl"], MANUSCRIPTS[0]["drmImageUrl"]]
    assert episode.pages[0].extra == {"drm_mode": "xor", "drm_hash": "62e07285b272877b"}
    assert (episode.pages[0].width, episode.pages[0].height) == (1114, 1600)
    assert (episode.prev_url, episode.next_url) == (None, SECOND_URL)
    assert episode.metadata["episode"]["code"] == "KC_0019810000100011_E"
    assert episode.metadata["viewer"] == VIEWER
    assert episode.metadata["work"]["title"] == "月華国奇医伝"

    # The work is asked for by code, the viewer by id at the desktop size, both as JSON with a Referer.
    assert session.calls == [f"{API_URL}/details/work", f"{API_URL}/viewer"]
    assert session.params_seen[0] == {"workCode": "KC_001981_S"}
    assert session.params_seen[1] == {"episodeId": ENTRIES[0]["id"], "imageSizeType": IMAGE_SIZE}
    assert all(headers["Accept"] == "application/json" for headers in session.headers_seen)
    assert all(headers["Referer"] == EPISODE_URL for headers in session.headers_seen)


def test_episode_accepts_a_trailing_slash_and_a_query(client):
    comicwalker, _ = client()
    episode = comicwalker.episode(f"{EPISODE_URL}/?co=true")
    assert episode.url == EPISODE_URL
    assert len(episode.pages) == 2


def test_locked_episode_has_no_pages_but_keeps_its_titles(client):
    comicwalker, _ = client()
    episode = comicwalker.episode(LOCKED_URL)

    assert episode.pages == ()
    assert not episode.readable
    assert episode.series_title == "月華国奇医伝"
    assert episode.episode_title == "第三話"
    assert (episode.prev_url, episode.next_url) == (SECOND_URL, None)
    assert episode.metadata["viewer"] == LOCKED_VIEWER


def test_last_readable_episode_names_the_locked_one_next(client):
    comicwalker, _ = client()
    episode = comicwalker.episode(SECOND_URL)
    assert episode.episode_title == "第二話"
    assert (episode.prev_url, episode.next_url) == (EPISODE_URL, LOCKED_URL)


def test_viewer_answering_404_means_locked(client, fake_response):
    comicwalker, _ = client(
        {
            f"{API_URL}/viewer?episodeId={ENTRIES[0]['id']}": fake_response(
                text=NOT_FOUND_HTML, status_code=HTTPStatus.NOT_FOUND
            )
        }
    )
    episode = comicwalker.episode(EPISODE_URL)
    assert not episode.readable
    assert episode.next_url == SECOND_URL
    assert episode.metadata["viewer"] is None


def test_raw_pages_are_kept_as_served(client, fake_response):
    raw = {**VIEWER, "manuscripts": [manuscript(1, "00000000", mode="raw")]}
    comicwalker, _ = client(
        {
            f"{API_URL}/viewer?episodeId={ENTRIES[0]['id']}": fake_response(payload=raw),
            f"{CDN}/1_": fake_response(webp_bytes((7, 8, 9)), content_type="image/webp"),
        }
    )
    episode = comicwalker.episode(EPISODE_URL)
    assert episode.pages[0].extra == {"drm_mode": "raw", "drm_hash": "00000000"}
    assert comicwalker.image(episode.pages[0], episode).convert("RGB").getpixel((0, 0)) == (7, 8, 9)


def test_unknown_episode_is_not_an_episode_page(client):
    comicwalker, _ = client()
    with pytest.raises(NotAnEpisodePageError, match="no episode KC_0019810009900011_E"):
        comicwalker.episode(f"{BASE_URL}/detail/KC_001981_S/episodes/KC_0019810009900011_E")


def test_unknown_work_is_not_an_episode_page(fake_session, fake_response):
    # The API answers an unknown work with the site's HTML 404 page.
    session = fake_session(
        {f"{API_URL}/details/work": fake_response(text=NOT_FOUND_HTML, status_code=HTTPStatus.NOT_FOUND)}
    )
    with pytest.raises(NotAnEpisodePageError, match="no work KC_000000_S"):
        ComicWalker(session).episode(f"{BASE_URL}/detail/KC_000000_S/episodes/KC_0000000000000000_E")


def test_work_answer_without_a_work_is_not_an_episode_page(fake_session, fake_response):
    session = fake_session({f"{API_URL}/details/work": fake_response(text=NOT_FOUND_HTML)})
    with pytest.raises(NotAnEpisodePageError, match="no work"):
        ComicWalker(session).episode(EPISODE_URL)


def test_episode_rejects_a_work_url(client):
    comicwalker, _ = client()
    with pytest.raises(UnsupportedUrlError):
        comicwalker.episode(WORK_URL)


# --- series ----------------------------------------------------------------------------


@pytest.mark.parametrize("url", [WORK_URL, f"{WORK_URL}/"])
def test_series_urls_lists_the_episodes_in_order(client, url):
    comicwalker, _ = client()
    assert comicwalker.series_urls(url) == [EPISODE_URL, SECOND_URL, LOCKED_URL]
    assert all(ComicWalker.suitable(candidate) for candidate in comicwalker.series_urls(url))


def test_series_urls_drops_a_repeated_entry(fake_session, fake_response):
    twice = {**WORK, "firstEpisodes": {"total": 3, "result": [ENTRIES[0], ENTRIES[0], ENTRIES[1]]}}
    session = fake_session({f"{API_URL}/details/work": fake_response(payload=twice)})
    assert ComicWalker(session).series_urls(WORK_URL) == [EPISODE_URL, SECOND_URL]


def test_series_urls_rejects_an_episode_url(client):
    comicwalker, _ = client()
    with pytest.raises(UnsupportedUrlError):
        comicwalker.series_urls(EPISODE_URL)


def test_series_urls_raises_on_an_empty_listing(fake_session, fake_response):
    empty = {**WORK, "firstEpisodes": {"total": 0, "result": []}}
    session = fake_session({f"{API_URL}/details/work": fake_response(payload=empty)})
    with pytest.raises(NotAnEpisodePageError, match="lists no episode"):
        ComicWalker(session).series_urls(WORK_URL)


# --- images ----------------------------------------------------------------------------


def test_image_fetches_the_signed_url_and_unmasks_it(client):
    comicwalker, session = client()
    episode = comicwalker.episode(EPISODE_URL)
    first = comicwalker.image(episode.pages[0], episode)
    second = comicwalker.image(episode.pages[1], episode)

    assert first.convert("RGB").getpixel((0, 0)) == (1, 2, 3)
    assert second.convert("RGB").getpixel((0, 0)) == (4, 5, 6)
    assert session.calls[-2:] == [episode.pages[0].url, episode.pages[1].url]
    assert session.headers_seen[-1]["Referer"] == EPISODE_URL


# --- the real site ---------------------------------------------------------------------

# One free episode per known host: the first episode of a long-running series.
TEST_URLS: dict[str, str] = {
    "comic-walker.com": "https://comic-walker.com/detail/KC_001981_S/episodes/KC_0019810000100011_E",
}


@pytest.mark.network
@pytest.mark.parametrize("host", TEST_URLS)
def test_site_download(tmp_path, host):
    result = Downloader(ComicWalker(), tmp_path, only_first=True).download(TEST_URLS[host])
    assert result.status == "saved"
    assert (result.save_dir / "0.jpg").stat().st_size > 0


@pytest.mark.network
def test_site_locked_episode_has_no_pages():
    # 第三話's free period ended in 2025; the site lists it but serves no pages.
    episode = ComicWalker().episode(LOCKED_URL)
    assert not episode.readable
    assert episode.episode_title == "第三話"
    assert episode.next_url is not None


@pytest.mark.network
def test_site_work_lists_episodes():
    urls = ComicWalker().series_urls(WORK_URL)
    assert urls[0] == EPISODE_URL
    assert len(urls) > 100
    assert all(ComicWalker.suitable(url) for url in urls)
