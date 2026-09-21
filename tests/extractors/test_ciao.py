from __future__ import annotations

from http import HTTPStatus
from io import BytesIO

import pytest
from httpx import HTTPStatusError
from PIL import Image

from getjmanga.downloader import Downloader
from getjmanga.errors import LoginError, NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractors.ciao import Ciao, descramble, episode_url, service_hash
from getjmanga.viewers.kmanga import GRID, UNIT, tile_order

SERIES_URL = "https://ciao.shogakukan.co.jp/comics/title/00813/"
EPISODE_URL = "https://ciao.shogakukan.co.jp/comics/title/00813/episode/32965"
NEXT_URL = "https://ciao.shogakukan.co.jp/comics/title/00813/episode/33287"
LOCKED_URL = "https://ciao.shogakukan.co.jp/comics/title/00813/episode/34870"
SERIES_TITLE = "妖カツ！！"
SEED = 3213983297

API = "https://api.ciao.shogakukan.co.jp"
# A substring match, so it goes after `VIEWER_API` in every route table.
EPISODE_API = f"{API}/web/episode"
VIEWER_API = f"{API}/web/episode/viewer"
TITLE_API = f"{API}/title/list"
LOGIN_API = f"{API}/web/user/login"
PAGE_1 = "https://cdn.ciao.shogakukan.co.jp/static/web_titles/813/episodes/32965/8e15.jpg?s_ver=2&Expires=1&Signature=x"
PAGE_2 = "https://cdn.ciao.shogakukan.co.jp/static/web_titles/813/episodes/32965/4775.jpg?s_ver=2&Expires=1&Signature=y"


def episode_payload(*, episode_id=32965, name="第2話", visible=1, point=0):
    """What `/web/episode` answers."""
    return {
        "status": "success",
        "response_code": 0,
        "error_message": "",
        "episode": {
            "title_id": 813,
            "episode_id": episode_id,
            "episode_name": name,
            "start_time": "2025-12-19 17:00:00",
            "thumbnail_image_url": "https://cdn.ciao.shogakukan.co.jp/static/titles/813/episodes/32965/thumbnail.jpg",
            "point": point,
            "is_page_visible": visible,
            "ticket_rental_enabled": 0,
            "badge": 4 if visible else 6,
            "rental_period": "" if visible else "72時間",
        },
        "is_wpapi_load_reduction": 0,
        "share": {"title_name": SERIES_TITLE, "twitter_post_text": "", "url": f"{API}/ldg?t=813&e={episode_id}"},
    }


def title_payload(*, ids=(32964, 32965, 33287, 34870), name=SERIES_TITLE):
    """What `/title/list` answers for one work."""
    return {
        "status": "success",
        "response_code": 0,
        "error_message": "",
        "title_list": [
            {
                "title_id": 813,
                "title_name": name,
                "author_text": "永尾柚乃,えびなしお",
                "episode_order": 1,
                "first_episode_id": ids[0] if ids else None,
                "free_episode_count": len(ids),
                "total_episode_count": len(ids),
                "episode_id_list": list(ids),
            }
        ],
    }


def viewer_payload(*, seed=SEED, version=2, next_id=33287, pages=(PAGE_1, PAGE_2)):
    """What `/web/episode/viewer` answers for a readable episode."""
    return {
        "status": "success",
        "response_code": 0,
        "error_message": "",
        "title_id": 813,
        "episode_id": 32965,
        "page_start_position": 1,
        "direction": 0,
        "page_slider": 1,
        "scramble_seed": seed,
        "page_list": list(pages),
        "previous_episode": {"title_id": 813, "episode_id": 32964},
        "next_episode": {"title_id": 813, "episode_id": next_id} if next_id else None,
        "descriptor_id_list": [],
        "previous_advertisement_list": [],
        "post_advertisement_list": [],
        "scramble_ver": version,
    }


def error_payload(code, message):
    return {"status": "error", "response_code": code, "server_time": "2026-09-18 11:23:21", "error_message": message}


def api_error(fake_response, code, message):
    return fake_response(payload=error_payload(code, message), status_code=HTTPStatus.BAD_REQUEST)


@pytest.fixture
def client(fake_session):
    def make(routes):
        session = fake_session(routes)
        return Ciao(session), session

    return make


@pytest.fixture
def readable_routes(fake_response):
    return {
        VIEWER_API: fake_response(payload=viewer_payload()),
        EPISODE_API: fake_response(payload=episode_payload()),
        TITLE_API: fake_response(payload=title_payload()),
    }


# --- the hash -----------------------------------------------------------------------


def test_service_hash_is_the_bundles_signature():
    # Worked out from the bundle: sorted `sha256(key)_sha512(value)` pairs joined by
    # commas, SHA-256'd, then SHA-512'd -- with no birthday suffix, unlike Nora's.
    params = {"version": "6.0.0", "platform": "3", "episode_id": "32965"}
    digest = service_hash(params)
    assert len(digest) == 128
    assert digest == service_hash({"episode_id": 32965, "platform": 3, "version": "6.0.0"})
    assert digest != service_hash({**params, "episode_id": "32966"})
    assert digest.startswith("edbb68162aaa72ac")


# --- descrambling -------------------------------------------------------------------


def tile_image(order, tile, extra):
    """A `GRID x GRID` image of flat-coloured tiles laid out in `order`, plus an edge strip."""
    width, height = tile[0] * GRID + extra[0], tile[1] * GRID + extra[1]
    image = Image.new("L", (width, height), 255)
    for destination, source in enumerate(order):
        x, y = destination % GRID * tile[0], destination // GRID * tile[1]
        image.paste(source * 10 + 10, (x, y, x + tile[0], y + tile[1]))
    return image


def scrambled_layout(seed=SEED):
    """Where the served page keeps each tile: the inverse of `tile_order()`."""
    layout = [0] * (GRID * GRID)
    for destination, source in enumerate(tile_order(seed)):
        layout[source] = destination
    return layout


def test_descramble_version_2_tiles_are_multiples_of_eight():
    # 4 x 24 + 5 = 101 wide: version 2 tiles are floor(101 / 8 / 4) * 8 = 24.
    original = tile_image(range(GRID * GRID), tile=(UNIT * 3, UNIT * 2), extra=(5, 3))
    scrambled = tile_image(scrambled_layout(), tile=(UNIT * 3, UNIT * 2), extra=(5, 3))
    assert scrambled.tobytes() != original.tobytes()
    assert descramble(scrambled, SEED, 2).tobytes() == original.tobytes()
    assert descramble(scrambled, SEED).tobytes() == original.tobytes()


def test_descramble_version_1_tiles_are_a_quarter_of_the_rounded_width():
    # 4 x 26 + 4 = 108 wide: rounded down to 104, a version 1 tile is 26 (not a
    # multiple of 8, which is where the two versions differ).
    tile, extra = (26, 30), (4, 2)
    original = tile_image(range(GRID * GRID), tile=tile, extra=extra)
    scrambled = tile_image(scrambled_layout(), tile=tile, extra=extra)
    assert descramble(scrambled, SEED, 1).tobytes() == original.tobytes()
    assert descramble(scrambled, SEED, 2).tobytes() != original.tobytes()


def test_descramble_leaves_the_edge_strip_and_the_size_alone():
    image = tile_image(range(GRID * GRID), tile=(26, 30), extra=(4, 2))
    for version in (1, 2):
        out = descramble(image, SEED, version)
        assert out.size == image.size
        assert out.histogram() == image.histogram()
        assert out.getpixel((image.width - 1, image.height - 1)) == 255


@pytest.mark.parametrize("seed", [0, 2**32])
def test_descramble_ignores_a_seed_out_of_range(seed):
    image = tile_image(range(GRID * GRID), tile=(26, 30), extra=(4, 2))
    assert descramble(image, seed, 1) is image


def test_descramble_ignores_an_image_too_small_to_tile():
    assert descramble(Image.new("L", (GRID - 1, 100), 0), SEED, 1).size == (GRID - 1, 100)
    assert descramble(Image.new("L", (GRID * UNIT - 1, 100), 0), SEED, 2).size == (GRID * UNIT - 1, 100)


# --- URLs ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        EPISODE_URL,
        f"{EPISODE_URL}/",
        SERIES_URL,
        "https://ciao.shogakukan.co.jp/comics/title/00813",
        "https://ciao.shogakukan.co.jp/comics/title/23/episode/4035",
    ],
)
def test_suitable_accepts_episode_and_work_urls(url):
    assert Ciao.suitable(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://ciao.shogakukan.co.jp/comics/title/00813/episode/32965",
        "https://ciao.shogakukan.co.jp/",
        "https://ciao.shogakukan.co.jp/comics/",
        "https://ciao.shogakukan.co.jp/comics/title/00813/latest/free",
        "https://ciao.shogakukan.co.jp/comics/magazine/1",
        "https://ciao.shogakukan.co.jp/title/00813/episode/32965",
        "https://www.corocoro.jp/title/00813/episode/32965",
    ],
)
def test_suitable_rejects_other_urls(url):
    assert not Ciao.suitable(url)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (SERIES_URL, True),
        ("https://ciao.shogakukan.co.jp/comics/title/00813", True),
        (EPISODE_URL, False),
        ("https://ciao.shogakukan.co.jp/comics/", False),
    ],
)
def test_is_series_is_a_work_page(url, expected):
    assert Ciao.is_series(url) is expected


# --- episodes -----------------------------------------------------------------------


def test_episode_reads_the_titles_the_pages_and_the_next_episode(client, readable_routes):
    ciao, session = client(readable_routes)
    episode = ciao.episode("https://ciao.shogakukan.co.jp/comics/title/00813/episode/32965/")

    assert episode.url == EPISODE_URL
    assert episode.series_title == SERIES_TITLE
    assert episode.episode_title == "第2話"
    assert (episode.writer, episode.publisher) == ("永尾柚乃, えびなしお", "小学館")
    assert [page.url for page in episode.pages] == [PAGE_1, PAGE_2]
    assert all(page.extra == {"seed": SEED, "version": 2} for page in episode.pages)
    assert (episode.prev_url, episode.next_url) == (episode_url(813, 32964), NEXT_URL)
    assert episode.metadata["episode"]["episode_id"] == 32965
    assert episode.metadata["title"]["title_name"] == SERIES_TITLE
    assert episode.metadata["viewer"]["scramble_seed"] == SEED

    # Every call is signed the bundle's way and sent as if from the site.
    assert session.calls == [f"{API}/web/episode", TITLE_API, VIEWER_API]
    assert session.params_seen[0] == {"version": "6.0.0", "platform": "3", "episode_id": "32965"}
    assert session.params_seen[1] == {"version": "6.0.0", "platform": "3", "title_id_list": "813"}
    assert session.params_seen[2] == {"version": "6.0.0", "platform": "3", "episode_id": "32965"}
    for headers, params in zip(session.headers_seen, session.params_seen, strict=True):
        assert headers["x-bambi-hash"] == service_hash(params)
        assert headers["x-bambi-is-crawler"] == "false"
        assert headers["Origin"] == "https://ciao.shogakukan.co.jp"
        assert "User-Agent" in headers


def test_episode_with_a_version_1_seed_says_so(client, readable_routes, fake_response):
    readable_routes[VIEWER_API] = fake_response(payload=viewer_payload(seed=57487861, version=1))
    ciao, _ = client(readable_routes)
    episode = ciao.episode(EPISODE_URL)
    assert all(page.extra == {"seed": 57487861, "version": 1} for page in episode.pages)


def test_episode_without_a_seed_has_nothing_to_undo(client, readable_routes, fake_response):
    readable_routes[VIEWER_API] = fake_response(payload=viewer_payload(seed=None))
    ciao, _ = client(readable_routes)
    episode = ciao.episode(EPISODE_URL)
    assert all(page.extra == {} for page in episode.pages)


def test_episode_takes_the_next_one_from_the_work_listing_first(client, readable_routes, fake_response):
    # The listing says 33287 follows; the viewer's `next_episode` only fills in when the
    # listing does not know the episode.
    readable_routes[VIEWER_API] = fake_response(payload=viewer_payload(next_id=99))
    ciao, _ = client(readable_routes)
    assert ciao.episode(EPISODE_URL).next_url == NEXT_URL

    readable_routes[TITLE_API] = fake_response(payload=title_payload(ids=(1, 2)))
    ciao, _ = client(readable_routes)
    assert ciao.episode(EPISODE_URL).next_url == episode_url(813, 99)


def test_last_episode_has_no_next(client, readable_routes, fake_response):
    readable_routes[TITLE_API] = fake_response(payload=title_payload(ids=(32964, 32965)))
    readable_routes[VIEWER_API] = fake_response(payload=viewer_payload(next_id=None))
    ciao, _ = client(readable_routes)
    episode = ciao.episode(EPISODE_URL)
    assert (episode.prev_url, episode.next_url) == (episode_url(813, 32964), None)


def test_priced_episode_has_no_pages_and_leaves_the_viewer_alone(client, fake_response):
    ciao, session = client(
        {
            EPISODE_API: fake_response(payload=episode_payload(episode_id=34870, name="第10話", visible=0, point=30)),
            TITLE_API: fake_response(payload=title_payload(ids=(32965, 34870, 35000))),
        },
    )
    # No viewer route at all: asking for it would fail the test with "no route".
    episode = ciao.episode(LOCKED_URL)

    assert episode.pages == ()
    assert episode.next_url == episode_url(813, 35000)
    assert episode.series_title == SERIES_TITLE
    assert episode.episode_title == "第10話"
    assert episode.metadata["viewer"] is None
    assert episode.metadata["episode"]["point"] == 30
    assert VIEWER_API not in session.calls


def test_episode_the_viewer_calls_unreleased_has_no_pages(client, readable_routes, fake_response):
    readable_routes[VIEWER_API] = api_error(fake_response, 3104, "episode unreleased.")
    ciao, _ = client(readable_routes)
    episode = ciao.episode(EPISODE_URL)

    assert episode.pages == ()
    assert episode.next_url == NEXT_URL
    assert episode.metadata["viewer"] == {}


def test_unknown_episode_is_not_an_episode(client, fake_response):
    ciao, _ = client({EPISODE_API: api_error(fake_response, 3100, "episode not found.")})
    with pytest.raises(NotAnEpisodePageError, match="no episode 999999"):
        ciao.episode("https://ciao.shogakukan.co.jp/comics/title/00813/episode/999999")


def test_episode_of_an_unknown_work_still_reads(client, readable_routes, fake_response):
    # The work listing is a convenience: without it the share text names the series.
    readable_routes[TITLE_API] = api_error(fake_response, 3000, "title not found.")
    ciao, _ = client(readable_routes)
    episode = ciao.episode(EPISODE_URL)

    assert episode.series_title == SERIES_TITLE
    assert [page.url for page in episode.pages] == [PAGE_1, PAGE_2]
    assert episode.next_url == NEXT_URL  # from the viewer, this time
    assert episode.metadata["title"] == {}


def test_other_api_errors_propagate(client, readable_routes, fake_response):
    readable_routes[EPISODE_API] = api_error(fake_response, 1003, "invalid hash.")
    ciao, _ = client(readable_routes)
    with pytest.raises(HTTPStatusError):
        ciao.episode(EPISODE_URL)

    readable_routes[EPISODE_API] = fake_response(text="", status_code=HTTPStatus.INTERNAL_SERVER_ERROR)
    ciao, _ = client(readable_routes)
    with pytest.raises(HTTPStatusError):
        ciao.episode(EPISODE_URL)


def test_episode_rejects_a_work_url(client):
    ciao, _ = client({})
    with pytest.raises(UnsupportedUrlError):
        ciao.episode(SERIES_URL)


# --- series -------------------------------------------------------------------------


def test_series_urls_lists_the_episodes_in_reading_order_deduplicated(client, fake_response):
    ciao, session = client({TITLE_API: fake_response(payload=title_payload(ids=(32964, 32965, 32964, 33287)))})
    urls = ciao.series_urls("https://ciao.shogakukan.co.jp/comics/title/00813")

    assert urls == [episode_url(813, 32964), EPISODE_URL, NEXT_URL]
    assert all(Ciao.suitable(url) and not Ciao.is_series(url) for url in urls)
    assert session.calls == [TITLE_API]
    assert session.params_seen == [{"version": "6.0.0", "platform": "3", "title_id_list": "813"}]


def test_series_urls_raises_on_an_empty_listing(client, fake_response):
    ciao, _ = client({TITLE_API: fake_response(payload=title_payload(ids=()))})
    with pytest.raises(NotAnEpisodePageError, match="lists no episode"):
        ciao.series_urls(SERIES_URL)


def test_series_urls_raises_on_an_unknown_work(client, fake_response):
    ciao, _ = client({TITLE_API: api_error(fake_response, 3000, "title not found.")})
    with pytest.raises(NotAnEpisodePageError, match="no work 99999"):
        ciao.series_urls("https://ciao.shogakukan.co.jp/comics/title/99999/")


def test_series_urls_rejects_an_episode_url(client):
    ciao, _ = client({})
    with pytest.raises(UnsupportedUrlError):
        ciao.series_urls(EPISODE_URL)


# --- images and the download --------------------------------------------------------


def encoded(image):
    raw = BytesIO()
    image.save(raw, "PNG")
    return raw.getvalue()


def test_image_descrambles_with_the_pages_seed_and_version(client, readable_routes, fake_response):
    tile, extra = (26, 30), (4, 2)
    original = tile_image(range(GRID * GRID), tile=tile, extra=extra)
    readable_routes[VIEWER_API] = fake_response(payload=viewer_payload(version=1))
    readable_routes["cdn.ciao.shogakukan.co.jp"] = fake_response(encoded(tile_image(scrambled_layout(), tile, extra)))
    ciao, session = client(readable_routes)
    episode = ciao.episode(EPISODE_URL)

    assert ciao.image(episode.pages[0], episode).convert("L").tobytes() == original.tobytes()
    assert session.calls[-1] == PAGE_1
    assert session.headers_seen[-1]["Referer"] == EPISODE_URL


# --- logging in ---------------------------------------------------------------------


def test_login_posts_the_signed_form(client, fake_response):
    ciao, session = client(
        {LOGIN_API: fake_response(payload={"status": "success", "response_code": 0, "error_message": ""})}
    )
    ciao.login(EPISODE_URL, "someone@example.com", "hunter2")

    url, form = session.posts[0]
    assert url == LOGIN_API
    assert form == {"version": "6.0.0", "platform": "3", "email": "someone@example.com", "password": "hunter2"}


def test_login_raises_with_the_site_reason(client, fake_response):
    ciao, _ = client({LOGIN_API: api_error(fake_response, 2000, "user not exist.")})
    with pytest.raises(LoginError, match="user not exist"):
        ciao.login(EPISODE_URL, "someone@example.com", "wrong")


def test_login_raises_on_a_non_json_answer(client, fake_response):
    ciao, _ = client({LOGIN_API: fake_response(text="<html>maintenance</html>")})
    with pytest.raises(LoginError, match="no reason given"):
        ciao.login(EPISODE_URL, "someone@example.com", "hunter2")


# --- the real site --------------------------------------------------------------------

# One episode per known host, free to read without an account.
TEST_URLS: dict[str, str] = {
    "ciao.shogakukan.co.jp": "https://ciao.shogakukan.co.jp/comics/title/00813/episode/32965",
}
#: A `scramble_ver` 1 episode, the first of a long-running series.
VERSION_1_URL = "https://ciao.shogakukan.co.jp/comics/title/00023/episode/4035"


@pytest.mark.network
@pytest.mark.parametrize("url", [*TEST_URLS.values(), VERSION_1_URL], ids=["version-2", "version-1"])
def test_site_download(tmp_path, url):
    result = Downloader(Ciao(), tmp_path, only_first=True).download(url)
    assert result.status == "saved"
    assert (result.save_dir / "0.jpg").stat().st_size > 0


@pytest.mark.network
def test_work_page_lists_episodes():
    urls = Ciao().series_urls("https://ciao.shogakukan.co.jp/comics/title/00023/")
    assert urls[0] == VERSION_1_URL
    assert all(Ciao.suitable(url) for url in urls)


@pytest.mark.network
def test_priced_episode_is_locked():
    episode = Ciao().episode("https://ciao.shogakukan.co.jp/comics/title/00023/episode/9027")
    assert episode.pages == ()
    assert episode.next_url is not None
