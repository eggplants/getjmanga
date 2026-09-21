from __future__ import annotations

from datetime import date
from http import HTTPStatus
from io import BytesIO

import pytest
from PIL import Image

from getjmanga.downloader import Downloader
from getjmanga.errors import LoginError, NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractors.ynjn import API_URL, BASE_URL, GRID, YanJan, descramble

SERIES_URL = f"{BASE_URL}/title/931"
EPISODE_URL = f"{BASE_URL}/viewer/931/63568"
NEXT_URL = f"{BASE_URL}/viewer/931/63570"
LOCKED_URL = f"{BASE_URL}/viewer/931/80142"
LAST_URL = f"{BASE_URL}/viewer/931/318163"
CDN = "https://public.ynjn.jp/web_page/63568"
PAGE_URLS = [f"{CDN}/08_891184_891184_3_001_001.webp", f"{CDN}/08_891184_891184_3_002_001.webp"]


def manga_page(number, url, spread=2):
    return {
        "manga_page": {
            "image_horizontal_size": 844,
            "image_vertical_size": 1200,
            "page_id": 940256 + number,
            "page_image_url": url,
            "page_number": number,
            "spread": spread,
            "spread_page_number": (number + 1) // 2,
        },
    }


# `/viewer?title_id=931&episode_id=63568`, cut down to what is read: two pages, an advert and the end card.
VIEWER = {
    "data": {
        "pages": [
            manga_page(1, PAGE_URLS[0]),
            manga_page(2, PAGE_URLS[1], spread=1),
            {"topic": {"image_topic": {"id": 335, "image_url": "https://public.ynjn.jp/web_topic/335/ad.jpg"}}},
            {"end_page": {"next_action_sheet": {"episode_id": 63570}, "next_button_type": "WEB_NEXT"}},
        ],
        "viewer_navigation": {
            "id": 63568,
            "is_web_available": True,
            "name": "第1話 ケイトとエミリコ",
            "next_episode_id": 63570,
            "pre_episode_id": 0,
            "read_condition": "EPISODE_READ_CONDITION_FREE",
            "read_direction": 1,
            "sid": "dam9g59q16iddjkkalagvbpqq",
            "title_name": "シャドーハウス",
            "total_page": 2,
        },
    },
    "is_success": True,
}

# A ticket episode: no pages, the price on the action sheet and no neighbours named.
LOCKED = {
    "data": {
        "action_sheet": {
            "consume_gold": {"paid_gold": 50, "total": 50},
            "episode_id": 80142,
            "episode_name": "第63話 集められる生き人形",
            "read_condition": "EPISODE_READ_CONDITION_NORMAL_TICKET",
            "shortage_gold": 50,
            "title_id": 931,
            "title_name": "シャドーハウス",
        },
        "pages": [],
        "viewer_navigation": {
            "id": 80142,
            "is_web_available": False,
            "name": "第63話 集められる生き人形",
            "next_episode_id": 0,
            "pre_episode_id": 0,
            "read_condition": "EPISODE_READ_CONDITION_UNSPECIFIED",
            "title_name": "シャドーハウス",
            "total_page": 0,
        },
    },
    "is_success": True,
}

LAST_LOCKED = {
    "data": {
        "action_sheet": {"episode_id": 318163, "episode_name": "第248話 幸せな結末", "title_name": "シャドーハウス"},
        "pages": [],
        "viewer_navigation": {"id": 318163, "name": "第248話 幸せな結末", "next_episode_id": 0, "title_name": ""},
    },
    "is_success": True,
}

NOT_FOUND = {
    "data": {"error_code": "E00038", "message": "データが見つかりません", "title": "エラー"},
    "is_success": False,
}


def listing_entry(episode_id, name, condition="EPISODE_READ_CONDITION_FREE"):
    return {"cost": 0, "id": episode_id, "name": name, "reading_condition": condition}


# `/title/931/episode?is_get_all=true`, with the first episode listed twice.
LISTING = {
    "data": {
        "all_count": 4,
        "episodes": [
            listing_entry(63568, "第1話 ケイトとエミリコ"),
            listing_entry(63570, "第2話 壊れた人形は"),
            listing_entry(80142, "第63話 集められる生き人形", "EPISODE_READ_CONDITION_NORMAL_TICKET"),
            listing_entry(63568, "第1話 ケイトとエミリコ"),
            listing_entry(318163, "第248話 幸せな結末", "EPISODE_READ_CONDITION_GOLD"),
        ],
        "ticket": {"gauge_recovery_rate": 0, "gauge_recovery_time": "", "is_boost": False},
    },
    "is_success": True,
}

LOGIN_REFUSED = {
    "data": {"error_code": "E00001", "message": "エラーが発生しました。", "title": "エラー"},
    "is_success": False,
}
LOGIN_DONE = {"data": {}, "is_success": True}


def tiled_image(size=(8 * GRID + 3, 12 * GRID + 2)):
    """An image whose 4x4 tiles each carry one colour, plus a leftover strip."""
    image = Image.new("RGB", size, (255, 255, 255))
    tile_w, tile_h = size[0] // GRID, size[1] // GRID
    for row in range(GRID):
        for col in range(GRID):
            colour = (row * 60, col * 60, 128)
            image.paste(colour, (col * tile_w, row * tile_h, (col + 1) * tile_w, (row + 1) * tile_h))
    return image


def scramble(image):
    """What the CDN serves: `descramble()` is a transpose, so it is its own inverse."""
    return descramble(image)


def webp_bytes(image):
    raw = BytesIO()
    image.save(raw, "WEBP", lossless=True)
    return raw.getvalue()


# The work page's Nuxt payload, cut down: one flat array, values by index.
TITLE_HTML = (
    '<html><body><script type="application/json" data-nuxt-data="nuxt-app" data-ssr="true" id="__NUXT_DATA__">'
    '[{"title":1},{"author":2,"name":4,"titleId":5},[3],"ソウマトウ","シャドーハウス",931]'
    "</script></body></html>"
)


@pytest.fixture
def client(fake_session, fake_response):
    """A `YanJan` over a session answering the viewer, the listing and a scrambled page."""

    def build(extra=None):
        routes = {
            "episode_id=63568": fake_response(payload=VIEWER),
            "episode_id=80142": fake_response(payload=LOCKED),
            "episode_id=318163": fake_response(payload=LAST_LOCKED),
            "episode_id=1": fake_response(payload=NOT_FOUND, status_code=HTTPStatus.INTERNAL_SERVER_ERROR),
            f"{API_URL}/title/931/episode": fake_response(payload=LISTING),
            f"{API_URL}/title/999999/episode": fake_response(
                payload=NOT_FOUND, status_code=HTTPStatus.INTERNAL_SERVER_ERROR
            ),
            "public.ynjn.jp": fake_response(webp_bytes(scramble(tiled_image())), content_type="binary/octet-stream"),
            f"{BASE_URL}/title/": fake_response(text=TITLE_HTML),
        }
        session = fake_session({**routes, **(extra or {})})
        return YanJan(session), session

    return build


# --- URLs ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        EPISODE_URL,
        f"{EPISODE_URL}/",
        SERIES_URL,
        f"{SERIES_URL}/",
        f"{BASE_URL}/episodeList/931",
    ],
)
def test_suitable_accepts_episode_and_series_urls(url):
    assert YanJan.suitable(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://ynjn.jp/viewer/931/63568",
        f"{BASE_URL}/",
        f"{BASE_URL}/viewer/931",
        f"{BASE_URL}/viewer/comic/12345",
        f"{BASE_URL}/comicList/931",
        f"{BASE_URL}/comic/12345",
        f"{BASE_URL}/titles/feature/?d=1",
        "https://www.ynjn.jp/viewer/931/63568",
        "https://piccoma.com/web/viewer/8195/1185884",
    ],
)
def test_suitable_rejects_other_urls(url):
    assert not YanJan.suitable(url)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (EPISODE_URL, False),
        (SERIES_URL, True),
        (f"{BASE_URL}/episodeList/931/", True),
    ],
)
def test_is_series(url, expected):
    assert YanJan.is_series(url) is expected


# --- descrambling ----------------------------------------------------------------------


def test_descramble_moves_tile_row_col_to_col_row():
    original = tiled_image()
    served = scramble(original)
    tile_w, tile_h = original.size[0] // GRID, original.size[1] // GRID
    # Tile (row 1, col 3) of the original sits at (row 3, col 1) on the CDN file.
    assert served.getpixel((1 * tile_w, 3 * tile_h)) == original.getpixel((3 * tile_w, 1 * tile_h)) == (60, 180, 128)


def test_descramble_leaves_the_edge_strip_alone():
    original = tiled_image()
    served = scramble(original)
    width, height = original.size
    assert served.getpixel((width - 1, 0)) == (255, 255, 255)
    assert served.getpixel((0, height - 1)) == (255, 255, 255)
    assert descramble(served).getpixel((width - 1, height - 1)) == (255, 255, 255)


# --- episodes --------------------------------------------------------------------------


def test_episode_reads_the_titles_the_pages_and_the_next_episode(client):
    ynjn, session = client()
    episode = ynjn.episode(EPISODE_URL)

    assert episode.url == EPISODE_URL
    assert episode.series_title == "シャドーハウス"
    assert (episode.writer, episode.publisher) == ("ソウマトウ", "集英社")
    assert episode.episode_title == "第1話 ケイトとエミリコ"
    assert [page.url for page in episode.pages] == PAGE_URLS
    assert [(page.width, page.height) for page in episode.pages] == [(844, 1200), (844, 1200)]
    assert [page.extra["page_number"] for page in episode.pages] == [1, 2]
    assert (episode.prev_url, episode.next_url) == (None, NEXT_URL)
    assert episode.metadata == VIEWER["data"]

    # The viewer API with the ids as query parameters, from the site's origin, then the
    # listing for the previous episode, which the viewer never names.
    assert session.calls == [
        f"{API_URL}/viewer?title_id=931&episode_id=63568",
        f"{API_URL}/title/931/episode?is_get_all=true",
        f"{BASE_URL}/title/931",
    ]
    assert session.params_seen[0] is None
    assert session.headers_seen[0]["Origin"] == BASE_URL
    assert session.headers_seen[0]["Accept"] == "application/json"


def test_episode_is_dated_by_its_first_page_upload(client, fake_response, uploaded):
    ynjn, _ = client({"public.ynjn.jp": fake_response(b"", headers=uploaded)})
    assert ynjn.episode(EPISODE_URL).published == date(2025, 8, 21)


def test_episode_accepts_a_trailing_slash(client):
    ynjn, _ = client()
    assert ynjn.episode(f"{EPISODE_URL}/").url == EPISODE_URL


def test_locked_episode_has_no_pages_and_the_next_from_the_listing(client):
    ynjn, session = client()
    episode = ynjn.episode(LOCKED_URL)

    assert episode.pages == ()
    assert not episode.readable
    assert episode.series_title == "シャドーハウス"
    assert episode.episode_title == "第63話 集められる生き人形"
    assert (episode.prev_url, episode.next_url) == (NEXT_URL, f"{BASE_URL}/viewer/931/63568")
    assert episode.metadata["action_sheet"]["shortage_gold"] == 50
    assert session.calls == [
        f"{API_URL}/viewer?title_id=931&episode_id=80142",
        f"{API_URL}/title/931/episode?is_get_all=true",
        f"{BASE_URL}/title/931",
    ]


def test_last_locked_episode_has_no_next(client):
    ynjn, _ = client()
    episode = ynjn.episode(LAST_URL)
    assert episode.pages == ()
    assert (episode.prev_url, episode.next_url) == (EPISODE_URL, None)
    assert episode.series_title == "シャドーハウス"


def test_locked_episode_of_an_unknown_series_still_returns(client):
    # The viewer answers the episode (it ignores `title_id`), the listing does not know the title.
    ynjn, _ = client()
    episode = ynjn.episode(f"{BASE_URL}/viewer/999999/80142")
    assert episode.pages == ()
    assert episode.next_url is None


def test_unknown_episode_is_not_an_episode_page(client):
    ynjn, _ = client()
    with pytest.raises(NotAnEpisodePageError, match="no episode 1 "):
        ynjn.episode(f"{BASE_URL}/viewer/931/1")


def test_episode_rejects_a_series_url(client):
    ynjn, _ = client()
    with pytest.raises(UnsupportedUrlError):
        ynjn.episode(SERIES_URL)


def test_episode_raises_on_a_failing_status_without_an_envelope(client, fake_response):
    ynjn, _ = client({"episode_id=63568": fake_response(text="down", status_code=HTTPStatus.BAD_GATEWAY)})
    with pytest.raises(Exception, match="502"):
        ynjn.episode(EPISODE_URL)


# --- series ----------------------------------------------------------------------------


@pytest.mark.parametrize("url", [SERIES_URL, f"{BASE_URL}/episodeList/931/"])
def test_series_urls_lists_every_episode_once_in_order(client, url):
    ynjn, session = client()
    assert ynjn.series_urls(url) == [
        f"{BASE_URL}/viewer/931/63568",
        f"{BASE_URL}/viewer/931/63570",
        f"{BASE_URL}/viewer/931/80142",
        f"{BASE_URL}/viewer/931/318163",
    ]
    assert session.calls == [f"{API_URL}/title/931/episode?is_get_all=true"]
    assert session.params_seen[0] is None


def test_series_urls_rejects_an_episode_url(client):
    ynjn, _ = client()
    with pytest.raises(UnsupportedUrlError):
        ynjn.series_urls(EPISODE_URL)


def test_series_urls_raises_for_an_unknown_series(client):
    ynjn, _ = client()
    with pytest.raises(NotAnEpisodePageError, match="no series 999999"):
        ynjn.series_urls(f"{BASE_URL}/title/999999")


def test_series_urls_raises_on_an_empty_listing(client, fake_response):
    empty = {"data": {"all_count": 0, "episodes": [], "ticket": None}, "is_success": True}
    ynjn, _ = client({f"{API_URL}/title/931/episode": fake_response(payload=empty)})
    with pytest.raises(NotAnEpisodePageError, match="lists no episode"):
        ynjn.series_urls(SERIES_URL)


# --- images ----------------------------------------------------------------------------


def test_image_descrambles_the_served_page(client):
    ynjn, session = client()
    episode = ynjn.episode(EPISODE_URL)
    image = ynjn.image(episode.pages[0], episode)

    assert image.size == tiled_image().size
    assert image.convert("RGB").tobytes() == tiled_image().tobytes()
    assert session.calls[-1] == PAGE_URLS[0]
    assert session.headers_seen[-1]["Referer"] == EPISODE_URL


# --- logging in ------------------------------------------------------------------------


def test_login_posts_the_credentials_as_json(client, fake_response):
    ynjn, session = client({f"{API_URL}/auth/login": fake_response(payload=LOGIN_DONE)})
    ynjn.login(EPISODE_URL, "someone@example.com", "hunter2")

    assert session.posts == [(f"{API_URL}/auth/login", {"email": "someone@example.com", "password": "hunter2"})]


def test_login_raises_with_the_site_reason(client, fake_response):
    ynjn, _ = client(
        {f"{API_URL}/auth/login": fake_response(payload=LOGIN_REFUSED, status_code=HTTPStatus.UNAUTHORIZED)},
    )
    with pytest.raises(LoginError, match="エラーが発生しました"):
        ynjn.login(EPISODE_URL, "someone@example.com", "wrong")


def test_login_raises_without_an_envelope(client, fake_response):
    ynjn, _ = client({f"{API_URL}/auth/login": fake_response(text="<html>maintenance</html>")})
    with pytest.raises(LoginError, match="refused the credentials"):
        ynjn.login(EPISODE_URL, "someone@example.com", "hunter2")


# --- the real site ---------------------------------------------------------------------

# One episode per known host, free to read without an account.
TEST_URLS: dict[str, str] = {
    "ynjn.jp": "https://ynjn.jp/viewer/931/63568",
}


@pytest.mark.network
@pytest.mark.parametrize("host", TEST_URLS)
def test_site_download(tmp_path, host):
    result = Downloader(YanJan(), tmp_path, only_first=True).download(TEST_URLS[host])
    assert result.status == "saved"
    assert (result.save_dir / "0.jpg").stat().st_size > 0


@pytest.mark.network
def test_site_lists_the_series():
    urls = YanJan().series_urls(SERIES_URL)
    assert urls[0] == EPISODE_URL
    assert all(YanJan.suitable(url) for url in urls)


@pytest.mark.network
def test_site_locked_episode_returns_without_pages():
    episode = YanJan().episode(LOCKED_URL)
    assert episode.pages == ()
    assert episode.next_url == f"{BASE_URL}/viewer/931/80143"
