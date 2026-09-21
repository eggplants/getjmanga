from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta, timezone
from http import HTTPStatus
from io import BytesIO

import pytest
from PIL import Image

from getjmanga.downloader import Downloader
from getjmanga.errors import NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractors.pixivcomic import (
    API_URL,
    BASE_URL,
    SHUFFLE_KEY_HEADER,
    PixivComic,
    block_order,
    descramble,
    read_headers,
    xoshiro128,
)

WORK_URL = f"{BASE_URL}/works/13564"
EPISODE_URL = f"{BASE_URL}/viewer/stories/244715"
LOCKED_URL = f"{BASE_URL}/viewer/stories/3229"
SALT = "q8Jyg18EeVYTXs4yXAashXyO2i0Ubp6NIMe04L-Ow_I"
KEY = "ce28153d-db2a-4bcc-a8c2-6f1ad0cbd673"
CDN = "https://img-comic.pximg.net/c/q90_gridshuffle32:32/images/page/244715"

# The viewer page: a Next.js shell whose `__NEXT_DATA__` carries the salt.
VIEWER_HTML = (
    '<html><body><div id="__next"></div><script id="__NEXT_DATA__" type="application/json">'
    '{"props":{"pageProps":{"salt":"' + SALT + '","htmlMetaData":{"title":"1 第1話-1 | 異世界皇子"},'
    '"id":"244715","workId":13564},"__N_SSP":true},"page":"/viewer/stories/[id]","query":{"id":"244715"}}'
    "</script></body></html>"
)
VIEWER_HTML_WITHOUT_SALT = (
    '<html><body><script id="__NEXT_DATA__" type="application/json">'
    '{"props":{"pageProps":{}},"page":"/works/[workId]"}</script></body></html>'
)

# `/api/app/episodes/244715/read_v4`, cut down to what is read.
PAGES = [
    {"url": f"{CDN}/a/1.jpg?1", "height": 1024, "width": 721, "gridsize": 32, "key": KEY},
    {"url": f"{CDN}/b/2.jpg?1", "height": 1024, "width": 721, "gridsize": 32, "key": KEY},
]
READABLE = {
    "data": {
        "reading_episode": {
            "id": 244715,
            "numbering_title": "1",
            "sub_title": "第1話-1",
            "viewer_path": "/viewer/stories/244715",
            "is_tateyomi": False,
            "sales_type": "free",
            "read_start_at": 1387508400000,
            "is_purchased": False,
            "state": "readable",
            "title": "1 第1話-1",
            "two_page_layout": "left",
            "work_id": 13564,
            "work_title": "異世界皇子、おしかけ求婚に参りました",
            "pages": PAGES,
            "prev_episode": {"id": 244714, "numbering_title": "0", "sub_title": "プロローグ", "state": "readable"},
            "next_episode": {"id": 244716, "numbering_title": "2", "sub_title": "第1話-2", "state": "readable"},
        }
    }
}
LOCKED = {
    "data": {
        "reading_episode": {
            "id": 3229,
            "numbering_title": "第4話",
            "sub_title": "",
            "sales_type": "sell",
            "sales_price": 55,
            "is_purchased": False,
            "state": "login_required",
            "title": "第4話",
            "work_id": 785,
            "work_title": "働かないふたり",
            "pages": [],
            "next_episode": {"id": 3277, "numbering_title": "第5話", "sub_title": "", "state": "login_required"},
        }
    }
}
NOT_FOUND = {"error": {"user_message": "指定のエピソードは存在しないか、既に公開期間を過ぎています"}}
BAD_REQUEST = {"error": {"user_message": "不正なリクエストです"}}


def listing(entries, next_page=None):
    return {"data": {"episodes": entries, "next_page_number": next_page}}


def entry(episode_id, state="readable"):
    return {
        "state": state,
        "episode": {"id": episode_id, "viewer_path": f"/viewer/stories/{episode_id}", "state": state},
    }


LISTING_PAGE_1 = listing(
    [
        entry(244715),
        entry(244716),
        {"state": "not_publishing", "message": "4〜15 掲載期間が終了しました"},
        entry(3229, "login_required"),
        entry(244716),
    ],
    next_page=2,
)
LISTING_PAGE_2 = listing([entry(246171)])


def jpeg_bytes(image):
    raw = BytesIO()
    image.save(raw, "JPEG", quality=95)
    return raw.getvalue()


def striped_page(width=100, height=70, gridsize=32):
    """A page whose every block is one flat colour keyed on its row and column."""
    image = Image.new("RGB", (width, height))
    for x in range(width):
        for y in range(height):
            image.putpixel((x, y), (min(255, (x // gridsize) * 60 + 10), min(255, (y // gridsize) * 60 + 10), 128))
    return image


def scramble(image, key, gridsize=32):
    """Shuffle a page the way the image server does, the inverse of `descramble()`."""
    width, height = image.size
    rows, columns = -(-height // gridsize), width // gridsize
    scrambled = image.copy()
    for row, sources in enumerate(block_order(key, rows, columns)):
        top, bottom = row * gridsize, min((row + 1) * gridsize, height)
        for destination, source in enumerate(sources):
            block = image.crop((destination * gridsize, top, (destination + 1) * gridsize, bottom))
            scrambled.paste(block, (source * gridsize, top))
    return scrambled


@pytest.fixture
def client(fake_session, fake_response):
    """A `PixivComic` over a session answering the viewer page, the API and the CDN."""

    def build(extra=None):
        routes = {
            f"{API_URL}/episodes/244715/read_v4": fake_response(payload=READABLE),
            f"{API_URL}/episodes/3229/read_v4": fake_response(payload=LOCKED),
            f"{API_URL}/episodes/999/read_v4": fake_response(payload=NOT_FOUND, status_code=HTTPStatus.NOT_FOUND),
            f"{API_URL}/works/13564/episodes/v2": [
                fake_response(payload=LISTING_PAGE_1),
                fake_response(payload=LISTING_PAGE_2),
            ],
            f"{API_URL}/works/999/episodes/v2": fake_response(payload=NOT_FOUND, status_code=HTTPStatus.NOT_FOUND),
            f"{API_URL}/works/v5/13564": fake_response(
                payload={"data": {"official_work": {"id": 13564, "name": "異世界皇子", "author": "紺乃みる/加藤沙羽"}}}
            ),
            f"{API_URL}/works/v5/785": fake_response(
                payload={"data": {"official_work": {"id": 785, "author": "吉田覚"}}}
            ),
            "/viewer/stories/999": fake_response(text="not found", status_code=HTTPStatus.NOT_FOUND),
            "/viewer/stories/": fake_response(text=VIEWER_HTML),
            CDN: fake_response(jpeg_bytes(scramble(striped_page(), KEY)), content_type="image/jpeg"),
        }
        # Overrides keep the route order, so the `/viewer/stories/` catch-all stays last.
        session = fake_session({**routes, **(extra or {})})
        return PixivComic(session), session

    return build


# --- URLs ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        EPISODE_URL,
        f"{EPISODE_URL}/",
        WORK_URL,
        f"{WORK_URL}/",
        "https://comic.pixiv.net/works/785",
    ],
)
def test_suitable_accepts_viewer_and_work_urls(url):
    assert PixivComic.suitable(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://comic.pixiv.net/viewer/stories/244715",
        "https://comic.pixiv.net/",
        "https://comic.pixiv.net/viewer/stories/",
        "https://comic.pixiv.net/magazines/371",
        "https://comic.pixiv.net/store/variants/2194450_1",
        "https://comic.pixiv.net/works/13564/episodes",
        "https://www.pixiv.net/artworks/13564",
        "https://palcy.jp/comics/1",
    ],
)
def test_suitable_rejects_other_urls(url):
    assert not PixivComic.suitable(url)


@pytest.mark.parametrize(
    ("url", "expected"),
    [(EPISODE_URL, False), (WORK_URL, True), (f"{WORK_URL}/", True)],
)
def test_is_series(url, expected):
    assert PixivComic.is_series(url) is expected


# --- signing ---------------------------------------------------------------------------


def test_read_headers_stamp_utc_and_hash_the_time_with_the_salt():
    at = datetime(2026, 9, 18, 12, 30, 45, tzinfo=timezone(timedelta(hours=9)))
    headers = read_headers(SALT, at)
    assert headers == {
        "X-Client-Time": "2026-09-18T03:30:45Z",
        "X-Client-Hash": hashlib.sha256(f"2026-09-18T03:30:45Z{SALT}".encode()).hexdigest(),
    }


# --- descrambling ----------------------------------------------------------------------


def test_xoshiro128_matches_the_reference_sequence():
    # xoshiro128** seeded with (1, 2, 3, 4): the first outputs of the reference implementation.
    values = xoshiro128((1, 2, 3, 4))
    assert [next(values) for _ in range(4)] == [11520, 0, 5927040, 70819200]


def test_block_order_pins_the_viewers_permutation():
    # The first row of a 721 x 1024 page (22 columns) under the real key, as the viewer's own `shuffle()` computes it.
    assert block_order(KEY, 1, 22)[0] == [11, 21, 10, 7, 19, 20, 2, 18, 13, 1, 0, 15, 5, 17, 3, 8, 9, 12, 4, 6, 14, 16]


def test_descramble_restores_a_scrambled_page():
    original = striped_page()
    scrambled = scramble(original, KEY)
    assert scrambled.tobytes() != original.tobytes()
    assert descramble(scrambled, KEY).tobytes() == original.tobytes()


def test_descramble_leaves_the_right_strip_and_partial_bottom_row_alone():
    original = striped_page(width=100, height=70)
    scrambled = scramble(original, KEY)
    # The 4 px on the right are not part of any block, so they are served as they are.
    assert scrambled.crop((96, 0, 100, 70)).tobytes() == original.crop((96, 0, 100, 70)).tobytes()
    restored = descramble(scrambled, KEY)
    assert restored.size == original.size
    assert restored.crop((0, 64, 100, 70)).tobytes() == original.crop((0, 64, 100, 70)).tobytes()


def test_descramble_returns_a_too_narrow_image_as_is():
    tiny = Image.new("RGB", (10, 10), (1, 2, 3))
    assert descramble(tiny, KEY) is tiny
    assert descramble(tiny, KEY, 0) is tiny


# --- episodes --------------------------------------------------------------------------


def test_episode_reads_the_titles_the_pages_and_the_next_episode(client):
    pixiv, session = client()
    episode = pixiv.episode(EPISODE_URL)

    assert episode.url == EPISODE_URL
    assert episode.series_title == "異世界皇子、おしかけ求婚に参りました"
    assert episode.episode_title == "1 第1話-1"
    assert (episode.writer, episode.publisher) == ("紺乃みる/加藤沙羽", "ピクシブ")
    assert episode.published == date(2013, 12, 20)
    assert [page.url for page in episode.pages] == [page["url"] for page in PAGES]
    assert all(page.extra == {"key": KEY, "gridsize": 32} for page in episode.pages)
    assert episode.pages[0].width == 721
    assert (episode.prev_url, episode.next_url) == (
        f"{BASE_URL}/viewer/stories/244714",
        f"{BASE_URL}/viewer/stories/244716",
    )
    assert episode.metadata == READABLE["data"]["reading_episode"]

    # The viewer page is read for the salt, then the API is asked with the signed
    # headers; the work's description, for its author, comes last.
    assert session.calls == [EPISODE_URL, f"{API_URL}/episodes/244715/read_v4", f"{API_URL}/works/v5/13564"]
    sent = session.headers_seen[-2]
    assert sent["X-Requested-With"] == "pixivcomic"
    assert sent["Referer"] == EPISODE_URL
    assert sent["X-Client-Hash"] == hashlib.sha256(f"{sent['X-Client-Time']}{SALT}".encode()).hexdigest()


def test_episode_accepts_a_trailing_slash(client):
    pixiv, _ = client()
    assert pixiv.episode(f"{EPISODE_URL}/").url == EPISODE_URL


def test_locked_episode_has_no_pages_but_keeps_its_titles_and_the_next(client):
    pixiv, _ = client()
    episode = pixiv.episode(LOCKED_URL)

    assert episode.pages == ()
    assert not episode.readable
    assert episode.series_title == "働かないふたり"
    assert episode.episode_title == "第4話"
    assert episode.next_url == f"{BASE_URL}/viewer/stories/3277"
    assert episode.metadata["state"] == "login_required"


def test_last_episode_has_no_next(client, fake_response):
    last = {"data": {"reading_episode": {**READABLE["data"]["reading_episode"], "next_episode": None}}}
    pixiv, _ = client({f"{API_URL}/episodes/244715/read_v4": fake_response(payload=last)})
    assert pixiv.episode(EPISODE_URL).next_url is None


def test_episode_title_falls_back_to_the_numbering_and_sub_title(client, fake_response):
    untitled = {**READABLE["data"]["reading_episode"]}
    del untitled["title"]
    pixiv, _ = client(
        {f"{API_URL}/episodes/244715/read_v4": fake_response(payload={"data": {"reading_episode": untitled}})}
    )
    assert pixiv.episode(EPISODE_URL).episode_title == "1 第1話-1"


def test_unknown_episode_is_not_an_episode_page(client):
    pixiv, _ = client()
    with pytest.raises(NotAnEpisodePageError, match="no viewer page"):
        pixiv.episode(f"{BASE_URL}/viewer/stories/999")


def test_ended_episode_is_not_an_episode_page(client):
    # The viewer page still renders (the salt is read), but the API answers 404.
    pixiv, _ = client()
    pixiv.episode(EPISODE_URL)
    with pytest.raises(NotAnEpisodePageError, match="no episode 999"):
        pixiv.episode(f"{BASE_URL}/viewer/stories/999")


def test_a_page_without_a_salt_is_not_an_episode_page(fake_session, fake_response):
    session = fake_session({"/viewer/stories/": fake_response(text=VIEWER_HTML_WITHOUT_SALT)})
    with pytest.raises(NotAnEpisodePageError, match="no viewer salt"):
        PixivComic(session).episode(EPISODE_URL)


def test_a_refused_signature_surfaces_as_an_http_error(client, fake_response):
    pixiv, _ = client(
        {f"{API_URL}/episodes/244715/read_v4": fake_response(payload=BAD_REQUEST, status_code=HTTPStatus.BAD_REQUEST)}
    )
    with pytest.raises(Exception, match="HTTP 400"):
        pixiv.episode(EPISODE_URL)


def test_episode_rejects_a_work_url(client):
    pixiv, _ = client()
    with pytest.raises(UnsupportedUrlError):
        pixiv.episode(WORK_URL)


# --- series ----------------------------------------------------------------------------


def test_series_urls_walks_every_page_in_order_without_repeats_or_ended_runs(client):
    pixiv, session = client()
    urls = pixiv.series_urls(WORK_URL)

    assert urls == [
        f"{BASE_URL}/viewer/stories/244715",
        f"{BASE_URL}/viewer/stories/244716",
        LOCKED_URL,
        f"{BASE_URL}/viewer/stories/246171",
    ]
    assert all(PixivComic.suitable(url) for url in urls)
    assert session.params_seen == [{"order": "asc", "page": 1}, {"order": "asc", "page": 2}]
    assert session.headers_seen[-1]["X-Requested-With"] == "pixivcomic"


def test_series_urls_rejects_an_episode_url(client):
    pixiv, _ = client()
    with pytest.raises(UnsupportedUrlError):
        pixiv.series_urls(EPISODE_URL)


def test_series_urls_raises_on_an_unknown_work(client):
    pixiv, _ = client()
    with pytest.raises(NotAnEpisodePageError, match="no work 999"):
        pixiv.series_urls(f"{BASE_URL}/works/999")


def test_series_urls_raises_on_an_empty_listing(fake_session, fake_response):
    session = fake_session({"/episodes/v2": fake_response(payload=listing([]))})
    with pytest.raises(NotAnEpisodePageError, match="lists no episode"):
        PixivComic(session).series_urls(WORK_URL)


# --- images ----------------------------------------------------------------------------


def test_image_sends_the_shuffle_key_and_descrambles(client):
    pixiv, session = client()
    episode = pixiv.episode(EPISODE_URL)
    image = pixiv.image(episode.pages[0], episode)

    assert session.calls[-1] == PAGES[0]["url"]
    assert session.headers_seen[-1][SHUFFLE_KEY_HEADER] == KEY
    assert session.headers_seen[-1]["Referer"] == EPISODE_URL
    # JPEG is lossy: compare against the original block by block, loosely.
    original = striped_page()
    for x, y in [(0, 0), (40, 10), (90, 30), (98, 68), (5, 66)]:
        assert all(abs(a - b) < 12 for a, b in zip(image.getpixel((x, y)), original.getpixel((x, y)), strict=True))


def test_image_without_a_key_is_served_as_is(client, fake_response):
    plain = {**PAGES[0], "key": None}
    unkeyed = {"data": {"reading_episode": {**READABLE["data"]["reading_episode"], "pages": [plain]}}}
    pixiv, session = client(
        {
            f"{API_URL}/episodes/244715/read_v4": fake_response(payload=unkeyed),
            CDN: fake_response(jpeg_bytes(Image.new("RGB", (64, 64), (200, 100, 50))), content_type="image/jpeg"),
        }
    )
    episode = pixiv.episode(EPISODE_URL)
    image = pixiv.image(episode.pages[0], episode)
    assert SHUFFLE_KEY_HEADER not in session.headers_seen[-1]
    assert image.size == (64, 64)


# --- logging in ------------------------------------------------------------------------


# --- the real site ---------------------------------------------------------------------

# One free episode per known host: the first episode of a long-running series.
TEST_URLS: dict[str, str] = {
    "comic.pixiv.net": "https://comic.pixiv.net/viewer/stories/3031",
}


@pytest.mark.network
@pytest.mark.parametrize("host", TEST_URLS)
def test_site_download(tmp_path, host):
    result = Downloader(PixivComic(), tmp_path, only_first=True).download(TEST_URLS[host])
    assert result.status == "saved"
    assert (result.save_dir / "0.jpg").stat().st_size > 0


@pytest.mark.network
def test_work_page_lists_episodes():
    urls = PixivComic().series_urls("https://comic.pixiv.net/works/785")
    assert "https://comic.pixiv.net/viewer/stories/3031" in urls
    assert all(PixivComic.suitable(url) for url in urls)


@pytest.mark.network
def test_login_only_episode_is_locked():
    episode = PixivComic().episode(LOCKED_URL)
    assert episode.pages == ()
    assert episode.next_url is not None
