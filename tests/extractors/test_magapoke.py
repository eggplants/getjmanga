from __future__ import annotations

from datetime import date
from http import HTTPStatus
from io import BytesIO

import pytest
from httpx2 import HTTPStatusError
from PIL import Image

from getjmanga.downloader import Downloader
from getjmanga.errors import NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractors.magapoke import SEED_ALPHABETS, MagaPoke, scramble_seed
from getjmanga.viewers.kmanga import GRID, UNIT, service_hash, tile_order

HOST = "https://pocket.shonenmagazine.com"
TITLE_URL = f"{HOST}/title/03251"
EPISODE_URL = f"{HOST}/title/03251/episode/439321"
NEXT_URL = f"{HOST}/title/03251/episode/439322"
LOCKED_URL = f"{HOST}/title/03251/episode/439329"
SERIES_TITLE = "湘南純愛組！"
EPISODE_TITLE = "【第1話】リゾ・ラバ～与論島編～"

EPISODE_API = "https://api.pocket.shonenmagazine.com/web/episode"
TITLE_API = "https://api.pocket.shonenmagazine.com/web/title/detail"
VIEWER_API = "https://se-api.pocket.shonenmagazine.com/web/episode/viewer"
CDN = "https://mgpk-cdn.magazinepocket.com/static/web_titles/3251/episodes/439321"
PAGE_1 = f"{CDN}/7d9efb34.jpg?Expires=1789697817&Signature=x&Key-Pair-Id=K335WU8AARLF64"
PAGE_2 = f"{CDN}/a6505de8.jpg?Expires=1789697817&Signature=y&Key-Pair-Id=K335WU8AARLF64"

#: The seed string the site really served for the episode, and what the viewer makes of it.
SEED_TEXT = "fqnjqnxq6o"
SEED = 4072076018 ^ (3251 + 439321)


def episode_payload(*, episode_id=439321, name=EPISODE_TITLE, point=0):
    """What `/web/episode` answers, readable or not."""
    return {
        "status": "success",
        "response_code": 0,
        "error_message": "",
        "episode": {
            "title_id": 3251,
            "episode_id": episode_id,
            "episode_name": name,
            "start_time": "2026-06-09 11:00:00",
            "point": point,
            "is_page_visible": int(point == 0),
            "ticket_rental_enabled": int(point > 0),
        },
        "share": {
            "title_name": SERIES_TITLE,
            "twitter_post_text": f"「{SERIES_TITLE}/{name}」マガポケ",
            "url": f"{HOST}/title/03251/episode/{episode_id}",
        },
    }


def viewer_payload(*, seed=SEED_TEXT, next_id=439322, pages=(PAGE_1, PAGE_2)):
    """What `/web/episode/viewer` answers for a readable episode."""
    return {
        "status": "success",
        "response_code": 0,
        "error_message": "",
        "title_id": 3251,
        "episode_id": 439321,
        "bonus_point": 0,
        "page_start_position": 1,
        "direction": 0,
        "scramble_seed": seed,
        "page_list": list(pages),
        "previous_episode": None,
        "next_episode": {"title_id": 3251, "episode_id": next_id} if next_id else None,
    }


def title_payload(*, episode_ids=(439321, 439322, 439329, 439330, 439322)):
    """What `/web/title/detail` answers: the work and its episode ids, oldest first."""
    return {
        "status": "success",
        "response_code": 0,
        "error_message": "",
        "web_title": {
            "title_id": 3251,
            "title_name": SERIES_TITLE,
            "author_text": "藤沢とおる",
            "episode_id_list": list(episode_ids),
            "total_episode_count": len(episode_ids),
        },
    }


def error_payload(code, message):
    return {"status": "error", "response_code": code, "server_time": "2026-09-18 10:51:33", "error_message": message}


UNPURCHASED = error_payload(3105, "episode unpurchased.")
NOT_FOUND = error_payload(3100, "episode not found.")
NO_TITLE = error_payload(3000, "title not found.")


def json_response(fake_response, payload, status_code=HTTPStatus.OK):
    return fake_response(payload=payload, status_code=status_code, content_type="application/json")


@pytest.fixture
def client(fake_session, fake_response):
    def make(routes):
        # The work's credits come off `/web/title/detail`, asked for once per work.
        session = fake_session(
            {**routes, TITLE_API: routes.get(TITLE_API, json_response(fake_response, title_payload()))}
        )
        return MagaPoke(session), session

    return make


# --- the seed -----------------------------------------------------------------------


def test_scramble_seed_reads_the_string_through_the_odd_alphabet_and_xors_the_ids():
    # `fqnjqnxq6o` through `q6jtf2xnog` is 4072076018; checked against the site's WebAssembly.
    assert scramble_seed(3251, 439321, SEED_TEXT) == SEED
    assert tile_order(SEED) == [6, 12, 8, 11, 5, 3, 4, 2, 0, 7, 13, 15, 10, 14, 9, 1]


def test_scramble_seed_picks_the_alphabet_by_the_title_parity():
    # An even title id reads through `svdk0m7acl`: the alphabet itself spells 0123456789.
    assert scramble_seed(2, 5, SEED_ALPHABETS[0]) == 123456789 ^ 7
    assert tile_order(123456789 ^ 7) == [14, 4, 12, 3, 11, 1, 0, 8, 5, 2, 6, 15, 13, 10, 9, 7]
    assert scramble_seed(1, 1, SEED_TEXT) == 4072076018 ^ 2


@pytest.mark.parametrize(
    "text",
    [
        "",
        "abcdefghij",  # letters outside the alphabet
        "12345",  # digits are not in either alphabet
        "fqnjqnxq6oq",  # 11 digits: past 32 bits
    ],
)
def test_scramble_seed_rejects_what_the_viewer_cannot_parse(text):
    assert scramble_seed(3251, 439321, text) is None


def test_scramble_seed_wraps_to_32_bits():
    assert scramble_seed(3251, 2**32 - 1, "q") == (2**32 - 1 + 3251) & 0xFFFFFFFF


# --- URLs ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        EPISODE_URL,
        f"{EPISODE_URL}/",
        TITLE_URL,
        f"{TITLE_URL}/",
        f"{HOST}/title/00045/episode/458",
    ],
)
def test_suitable_accepts_episode_and_work_page_urls(url):
    assert MagaPoke.suitable(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://pocket.shonenmagazine.com/title/03251/episode/439321",
        f"{HOST}/",
        f"{HOST}/title/3251",  # the site wants five digits
        f"{HOST}/title/3251/episode/439321",
        f"{HOST}/title/03251/episode/439321/embed",
        f"{HOST}/title/list",
        f"{HOST}/episode/439321",
        "https://shonenmagazine.com/title/03251",
        "https://nora.gakken.jp/comic/page-buttigumi/?episode_id=142",
    ],
)
def test_suitable_rejects_other_urls(url):
    assert not MagaPoke.suitable(url)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (TITLE_URL, True),
        (f"{TITLE_URL}/", True),
        (EPISODE_URL, False),
        (f"{HOST}/title/list", False),
    ],
)
def test_is_series_is_a_work_page(url, expected):
    assert MagaPoke.is_series(url) is expected


# --- episodes -----------------------------------------------------------------------


def test_episode_reads_the_titles_the_pages_and_the_next_episode(client, fake_response):
    magapoke, session = client(
        {
            EPISODE_API: json_response(fake_response, episode_payload()),
            VIEWER_API: json_response(fake_response, viewer_payload()),
        },
    )
    episode = magapoke.episode(EPISODE_URL)

    assert episode.url == EPISODE_URL
    assert episode.series_title == SERIES_TITLE
    assert episode.episode_title == EPISODE_TITLE
    assert (episode.writer, episode.publisher) == ("藤沢とおる", "講談社")
    assert (episode.published, episode.number) == (date(2026, 6, 9), 1)
    assert [page.url for page in episode.pages] == [PAGE_1, PAGE_2]
    assert all(page.extra == {"seed": SEED} for page in episode.pages)
    assert episode.next_url == NEXT_URL
    assert episode.metadata["title_id"] == 3251
    assert episode.metadata["episode"]["episode"]["episode_name"] == EPISODE_TITLE
    assert episode.metadata["viewer"]["scramble_seed"] == SEED_TEXT

    # The episode is looked up on the plain API host, the viewer on the secure one, both signed.
    assert session.calls == [EPISODE_API, VIEWER_API, TITLE_API]
    assert session.params_seen == [{"episode_id": "439321"}, {"episode_id": "439321"}, {"title_id": "3251"}]
    for headers in session.headers_seen[:2]:
        assert headers["x-manga-hash"] == service_hash({"episode_id": "439321"})
        assert headers["x-manga-is-crawler"] == "false"
        assert headers["x-manga-platform"] == "3"
        assert headers["Origin"] == HOST
        assert "User-Agent" in headers


def test_episode_at_the_end_of_a_series_has_no_next_url(client, fake_response):
    magapoke, _ = client(
        {
            EPISODE_API: json_response(fake_response, episode_payload()),
            VIEWER_API: json_response(fake_response, viewer_payload(next_id=None)),
        },
    )
    assert magapoke.episode(EPISODE_URL).next_url is None


@pytest.mark.parametrize("seed", [None, "", "abcdefghij", "svdk0m7acl"])
def test_episode_without_a_usable_seed_has_nothing_to_undo(client, fake_response, seed):
    # `svdk0m7acl` is the even alphabet; through the odd one it does not parse.
    magapoke, _ = client(
        {
            EPISODE_API: json_response(fake_response, episode_payload()),
            VIEWER_API: json_response(fake_response, viewer_payload(seed=seed)),
        },
    )
    episode = magapoke.episode(EPISODE_URL)
    assert len(episode.pages) == 2
    assert all(page.extra == {} for page in episode.pages)


def test_episode_whose_seed_comes_out_zero_has_nothing_to_undo(client, fake_response):
    # `ffj2nj` spells 442572 in the odd alphabet, which is 3251 + 439321: the XOR is 0,
    # and the viewer leaves a page alone for a zero seed.
    assert scramble_seed(3251, 439321, "ffj2nj") == 0
    magapoke, _ = client(
        {
            EPISODE_API: json_response(fake_response, episode_payload()),
            VIEWER_API: json_response(fake_response, viewer_payload(seed="ffj2nj")),
        },
    )
    episode = magapoke.episode(EPISODE_URL)
    assert len(episode.pages) == 2
    assert all(page.extra == {} for page in episode.pages)


def test_locked_episode_has_no_pages_and_walks_the_work_listing_for_the_next(client, fake_response):
    magapoke, session = client(
        {
            EPISODE_API: json_response(
                fake_response, episode_payload(episode_id=439329, name="【第9話】危険な情事", point=60)
            ),
            VIEWER_API: json_response(fake_response, UNPURCHASED, HTTPStatus.BAD_REQUEST),
            TITLE_API: json_response(fake_response, title_payload()),
        },
    )
    episode = magapoke.episode(LOCKED_URL)

    assert episode.pages == ()
    assert episode.url == LOCKED_URL
    assert episode.series_title == SERIES_TITLE
    assert episode.episode_title == "【第9話】危険な情事"
    assert (episode.prev_url, episode.next_url) == (
        f"{HOST}/title/03251/episode/439322",
        f"{HOST}/title/03251/episode/439330",
    )
    assert episode.metadata["viewer"] == UNPURCHASED
    assert session.calls[-1] == TITLE_API
    assert session.params_seen[-1] == {"title_id": "3251"}


def test_locked_last_episode_has_no_next_url(client, fake_response):
    magapoke, _ = client(
        {
            EPISODE_API: json_response(fake_response, episode_payload(episode_id=439330, point=60)),
            VIEWER_API: json_response(fake_response, UNPURCHASED, HTTPStatus.BAD_REQUEST),
            TITLE_API: json_response(fake_response, title_payload(episode_ids=(439321, 439329, 439330))),
        },
    )
    episode = magapoke.episode(f"{HOST}/title/03251/episode/439330")
    assert episode.pages == ()
    assert (episode.prev_url, episode.next_url) == (LOCKED_URL, None)


def test_locked_episode_the_work_does_not_list_has_no_next_url(client, fake_response):
    magapoke, _ = client(
        {
            EPISODE_API: json_response(fake_response, episode_payload(episode_id=439999, point=60)),
            VIEWER_API: json_response(
                fake_response, error_payload(3104, "episode unreleased."), HTTPStatus.BAD_REQUEST
            ),
            TITLE_API: json_response(fake_response, NO_TITLE, HTTPStatus.BAD_REQUEST),
        },
    )
    episode = magapoke.episode(f"{HOST}/title/03251/episode/439999")
    assert episode.pages == ()
    assert episode.next_url is None


def test_unknown_episode_is_not_an_episode(client, fake_response):
    magapoke, session = client({EPISODE_API: json_response(fake_response, NOT_FOUND, HTTPStatus.BAD_REQUEST)})
    with pytest.raises(NotAnEpisodePageError, match="no episode"):
        magapoke.episode(f"{HOST}/title/03251/episode/1")
    assert len(session.calls) == 1


def test_url_that_is_not_an_episode_asks_the_api_nothing(client):
    magapoke, session = client({})
    with pytest.raises(NotAnEpisodePageError, match="not an episode page"):
        magapoke.episode(TITLE_URL)
    assert session.calls == []


def test_other_api_errors_propagate(client, fake_response):
    magapoke, _ = client(
        {
            EPISODE_API: json_response(fake_response, episode_payload()),
            VIEWER_API: fake_response(text="", status_code=HTTPStatus.INTERNAL_SERVER_ERROR),
        },
    )
    with pytest.raises(HTTPStatusError):
        magapoke.episode(EPISODE_URL)


# --- series -------------------------------------------------------------------------


def test_series_urls_lists_the_episodes_oldest_first_deduplicated(client, fake_response):
    magapoke, session = client({TITLE_API: json_response(fake_response, title_payload())})
    urls = magapoke.series_urls(TITLE_URL)

    assert urls == [EPISODE_URL, NEXT_URL, LOCKED_URL, f"{HOST}/title/03251/episode/439330"]
    assert all(MagaPoke.suitable(url) and not MagaPoke.is_series(url) for url in urls)
    assert session.calls == [TITLE_API]
    assert session.params_seen == [{"title_id": "3251"}]
    assert session.headers_seen[0]["x-manga-hash"] == service_hash({"title_id": "3251"})


def test_series_urls_raises_on_an_empty_listing(client, fake_response):
    magapoke, _ = client({TITLE_API: json_response(fake_response, title_payload(episode_ids=()))})
    with pytest.raises(NotAnEpisodePageError, match="lists no episode"):
        magapoke.series_urls(TITLE_URL)


def test_series_urls_raises_without_a_work(client, fake_response):
    magapoke, _ = client({TITLE_API: json_response(fake_response, NO_TITLE, HTTPStatus.BAD_REQUEST)})
    with pytest.raises(NotAnEpisodePageError, match="no work"):
        magapoke.series_urls(f"{HOST}/title/99999")


@pytest.mark.parametrize("url", [EPISODE_URL, f"{HOST}/title/list"])
def test_series_urls_refuses_a_url_that_is_not_a_work_page(client, url):
    magapoke, session = client({})
    with pytest.raises(UnsupportedUrlError):
        magapoke.series_urls(url)
    assert session.calls == []


# --- downloading --------------------------------------------------------------------


def tile_image(order, tile=(UNIT * 3, UNIT * 2), extra=(5, 3)):
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


def _png(image):
    raw = BytesIO()
    image.save(raw, "PNG")
    return raw.getvalue()


def test_image_descrambles_with_the_derived_seed(client, fake_response):
    scrambled = tile_image(scrambled_layout())
    magapoke, session = client(
        {
            EPISODE_API: json_response(fake_response, episode_payload()),
            VIEWER_API: json_response(fake_response, viewer_payload()),
            "7d9efb34.jpg": fake_response(_png(scrambled), content_type="image/png"),
        },
    )
    episode = magapoke.episode(EPISODE_URL)

    image = magapoke.image(episode.pages[0], episode)

    assert image.convert("L").tobytes() == tile_image(range(GRID * GRID)).tobytes()
    assert session.calls[-1] == PAGE_1
    assert session.headers_seen[-1]["Referer"] == EPISODE_URL


# --- logging in ---------------------------------------------------------------------


# --- the real site ------------------------------------------------------------------

# One episode per known host, free to read without an account: the scrambled
# first episode of 湘南純愛組 (Shonan Junai Gumi), a completed series the site keeps up.
TEST_URLS: dict[str, str] = {
    "pocket.shonenmagazine.com": EPISODE_URL,
}


@pytest.mark.network
@pytest.mark.parametrize("host", TEST_URLS)
def test_site_download(tmp_path, host):
    result = Downloader(MagaPoke(), tmp_path, only_first=True).download(TEST_URLS[host])
    assert result.status == "saved"
    assert (result.save_dir / "0.jpg").stat().st_size > 0


@pytest.mark.network
def test_work_page_lists_episodes_oldest_first():
    urls = MagaPoke().series_urls(TITLE_URL)
    assert urls[0] == EPISODE_URL
    assert all(MagaPoke.suitable(url) for url in urls)
