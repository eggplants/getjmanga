from __future__ import annotations

import base64
from http import HTTPStatus
from io import BytesIO

import pytest
from PIL import Image

from getjmanga.downloader import Downloader
from getjmanga.errors import GetjmangaError, LoginError, NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractors.corona import (
    API_ENVIRONMENT_KEY,
    API_URL,
    BASE_URL,
    FIREBASE_SIGN_IN_URL,
    Corona,
    descramble,
    parse_drm_hash,
)

COMIC_ID = "245224032125144"
LISTING_QUERY = "limit=100&sort=episode_order&order=asc&episode_status=free_viewing,only_for_subscription"
COMIC_URL = f"{BASE_URL}/comics/{COMIC_ID}"
EPISODE_URL = f"{BASE_URL}/episodes/245764154232569"
SECOND_URL = f"{BASE_URL}/episodes/245765062348538"
LOCKED_URL = f"{BASE_URL}/episodes/245766017945339"
CDN = "https://cdn.to-corona-ex.com/pages/images/2026/0303/1241"
NOT_FOUND = {
    "errors": [{"message": "指定されたエピソードが見つかりませんでした", "target": "episode", "code": "not_found"}]
}


def drm_hash(columns, rows, order):
    return base64.b64encode(bytes([columns, rows, *order])).decode()


# A 2x2 grid whose tiles are rotated one slot: destination n gets source (n + 1) % 4.
SHUFFLE = drm_hash(2, 2, [1, 2, 3, 0])


def entry(order, episode_id, title, status="free_viewing"):
    return {
        "comic_id": COMIC_ID,
        "episode_order": order,
        "episode_status": status,
        "id": episode_id,
        "published_at": "2026-03-09T11:00:00.000+09:00",
        "thumbnail_image_url": "https://cdn.to-corona-ex.com/episodes/thumbnails/x?X-Amz-Signature=sig",
        "title": title,
    }


ENTRIES = [
    entry(1, "245764154232569", " 第1話"),
    entry(2, "245765062348538", " 第2話"),
    entry(3, "245766017945339", "第3話", "only_for_subscription"),
]


def page(page_id, name, hash_):
    return {
        "drm_hash": hash_,
        "episode_id": "245764154232569",
        "id": page_id,
        "page_image_url": f"{CDN}/{name}?drm_hash={hash_}&Expires=1789784490&Signature=sig&Key-Pair-Id=K3PD8IYRVLYLP8",
    }


def described(episode_id, title, status="free_viewing", pages=()):
    return {
        "comic_description": "勇者とは――",
        "comic_good_count": 15389,
        "comic_id": COMIC_ID,
        "comic_title": "クズ勇者のその日暮らし@COMIC",
        "episode_google_adsense_enabled": True,
        "episode_id": episode_id,
        "episode_status": status,
        "episode_thumbnail_image_url": "https://cdn.to-corona-ex.com/episodes/thumbnails/x?X-Amz-Signature=sig",
        "episode_title": title,
        "is_first_view_spread": False,
        "page_direction": "rtl",
        "pages": list(pages),
    }


# `begin_reading` for the first episode: two pages, one scrambled and one not.
BEGIN = described("245764154232569", " 第1話", pages=[page("1", "aaaa", SHUFFLE), page("2", "bbbb", "")])
LOCKED = described("245766017945339", "第3話", "only_for_subscription")


def neighbours(previous=None, following=None):
    return {
        "comic_id": COMIC_ID,
        "next_episode": following,
        "next_episode_update_at": None,
        "previous_episode": previous,
    }


def png_bytes(image):
    raw = BytesIO()
    image.save(raw, "PNG")
    return raw.getvalue()


def tiled_image(columns=2, rows=2, tile=(8, 8), margin=(0, 0)):
    """One flat colour per tile, numbered row by row, plus an unshuffled edge strip."""
    width, height = columns * tile[0] + margin[0], rows * tile[1] + margin[1]
    image = Image.new("RGB", (width, height), (255, 255, 255))
    for index in range(columns * rows):
        row, col = divmod(index, columns)
        box = (col * tile[0], row * tile[1], (col + 1) * tile[0], (row + 1) * tile[1])
        image.paste((index * 40, 0, 0), box)
    return image


def scramble(image, hash_):
    """Do what the site does: put source tile `order[n]` where the viewer will look for it."""
    columns, rows, order = parse_drm_hash(hash_)
    width, height = image.size
    tile_width, tile_height = (width - width % 8) // columns, (height - height % 8) // rows
    out = image.copy()
    # The viewer copies tile order[n] to slot n, so the served file has tile n at slot order[n].
    for dest, src in enumerate(order):
        dest_row, dest_col = divmod(dest, columns)
        src_row, src_col = divmod(src, columns)
        box = (dest_col * tile_width, dest_row * tile_height, (dest_col + 1) * tile_width, (dest_row + 1) * tile_height)
        out.paste(image.crop(box), (src_col * tile_width, src_row * tile_height))
    return out


@pytest.fixture
def client(fake_session, fake_response):
    """A `Corona` over a session answering the listing, the viewer calls and two pages.

    The listing and `end_reading` are asked for with query parameters, so the
    routes match on the URL with its query appended.
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
            f"{API_URL}/episodes/245764154232569/begin_reading": fake_response(payload=BEGIN),
            f"{API_URL}/episodes/245764154232569/end_reading": fake_response(
                payload=neighbours(following=ENTRIES[1]),
            ),
            f"{API_URL}/episodes/245766017945339/begin_reading": fake_response(
                payload=LOCKED, status_code=HTTPStatus.PAYMENT_REQUIRED
            ),
            f"{API_URL}/episodes/245766017945339/end_reading": fake_response(
                payload=neighbours(previous=ENTRIES[1], following=None),
            ),
            f"{API_URL}/episodes/1/begin_reading": fake_response(payload=NOT_FOUND, status_code=HTTPStatus.NOT_FOUND),
            f"{API_URL}/comics/{COMIC_ID}": fake_response(
                payload={
                    "authors": [
                        {"creator_id": "1", "name": "OFURO", "role": "漫画"},
                        {"creator_id": "2", "name": "珍比良", "role": "原作"},
                    ],
                    "title": "クズ勇者のその日暮らし@COMIC",
                },
            ),
            f"{API_URL}/episodes?comic_id={COMIC_ID}&{LISTING_QUERY}&after_than=c1": fake_response(
                payload={"resources": [ENTRIES[2], ENTRIES[1]], "next_cursor": None},
            ),
            f"{API_URL}/episodes?comic_id={COMIC_ID}&limit=100": fake_response(
                payload={"resources": ENTRIES[:2], "next_cursor": "c1"},
            ),
            f"{API_URL}/episodes?comic_id=1&": fake_response(payload=NOT_FOUND, status_code=HTTPStatus.NOT_FOUND),
            f"{API_URL}/episodes?comic_id=2&": fake_response(payload={"resources": [], "next_cursor": None}),
            f"{CDN}/aaaa": fake_response(png_bytes(scramble(tiled_image(), SHUFFLE)), content_type="image/png"),
            f"{CDN}/bbbb": fake_response(png_bytes(tiled_image()), content_type="image/png"),
        }
        # A route handed in by a test wins over the canned one, and is matched first.
        merged = dict(extra or {})
        for needle, response in routes.items():
            merged.setdefault(needle, response)
        session = QuerySession(merged)
        return Corona(session), session

    return build


# --- URLs -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        EPISODE_URL,
        f"{EPISODE_URL}/",
        COMIC_URL,
        f"{COMIC_URL}/",
    ],
)
def test_suitable_accepts_the_site_pages(url):
    assert Corona.suitable(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://to-corona-ex.com/episodes/245764154232569",
        "https://www.to-corona-ex.com/episodes/245764154232569",
        "https://to-corona-ex.com/",
        "https://to-corona-ex.com/comics",
        "https://to-corona-ex.com/genres/41114554925057/comics",
        "https://to-corona-ex.com/episodes/abc",
        "https://example.com/episodes/245764154232569",
    ],
)
def test_suitable_rejects_other_urls(url):
    assert not Corona.suitable(url)


def test_is_series_tells_a_work_page_from_an_episode():
    assert Corona.is_series(COMIC_URL)
    assert Corona.is_series(f"{COMIC_URL}/")
    assert not Corona.is_series(EPISODE_URL)


# --- an episode -----------------------------------------------------------------------


def test_episode_reads_the_titles_the_pages_and_the_next_episode(client):
    corona, session = client()
    episode = corona.episode(f"{EPISODE_URL}/")

    assert episode.url == EPISODE_URL
    assert episode.series_title == "クズ勇者のその日暮らし@COMIC"
    assert episode.episode_title == " 第1話"
    assert (episode.writer, episode.publisher) == ("OFURO (漫画), 珍比良 (原作)", "TOブックス")
    assert [page.url.split("?")[0] for page in episode.pages] == [f"{CDN}/aaaa", f"{CDN}/bbbb"]
    assert [page.extra for page in episode.pages] == [{"drm_hash": SHUFFLE}, {"drm_hash": ""}]
    assert episode.next_url == SECOND_URL
    assert episode.metadata["episode"] == BEGIN
    assert episode.metadata["neighbours"]["next_episode"]["id"] == "245765062348538"

    assert session.calls == [
        f"{API_URL}/episodes/245764154232569/begin_reading",
        f"{API_URL}/episodes/245764154232569/end_reading",
        f"{API_URL}/comics/{COMIC_ID}",
    ]
    assert session.params_seen == [
        None,
        {"previous_and_next_episode_status": "free_viewing,only_for_subscription"},
        None,
    ]
    for headers in session.headers_seen:
        assert headers["X-API-Environment-Key"] == API_ENVIRONMENT_KEY
        assert headers["Referer"] == EPISODE_URL
        assert headers["Origin"] == BASE_URL
        assert "Authorization" not in headers


def test_episode_locked_behind_the_subscription_has_no_pages_but_a_next_url(client):
    corona, _ = client()
    episode = corona.episode(LOCKED_URL)

    assert episode.pages == ()
    assert not episode.readable
    assert episode.series_title == "クズ勇者のその日暮らし@COMIC"
    assert episode.episode_title == "第3話"
    assert (episode.prev_url, episode.next_url) == (SECOND_URL, None)
    assert episode.metadata["episode"]["episode_status"] == "only_for_subscription"


def test_episode_raises_for_an_unknown_episode(client):
    corona, _ = client()
    with pytest.raises(NotAnEpisodePageError, match="no episode 1 "):
        corona.episode(f"{BASE_URL}/episodes/1")


def test_episode_raises_for_a_work_url(client):
    corona, _ = client()
    with pytest.raises(UnsupportedUrlError):
        corona.episode(COMIC_URL)


def test_episode_survives_a_failing_end_reading(client, fake_response):
    corona, _ = client({f"{API_URL}/episodes/245764154232569/end_reading": fake_response(status_code=500)})
    episode = corona.episode(EPISODE_URL)
    assert len(episode.pages) == 2
    assert episode.next_url is None
    assert episode.metadata["neighbours"] == {}


def test_episode_raises_on_a_server_error(client, fake_response):
    corona, _ = client(
        {f"{API_URL}/episodes/245764154232569/begin_reading": fake_response(status_code=HTTPStatus.BAD_GATEWAY)},
    )
    with pytest.raises(Exception, match="502"):
        corona.episode(EPISODE_URL)


def test_api_rereads_the_environment_key_after_a_403(client, fake_response):
    home = '<script src="/_next/static/chunks/pages/_app-abc123.js" defer=""></script>'
    bundle = 'n.defaults.headers.common["X-API-Environment-Key"]="fresh-key=";let i=async'
    corona, session = client(
        {
            f"{API_URL}/episodes/245764154232569/begin_reading": [
                fake_response(status_code=HTTPStatus.FORBIDDEN),
                fake_response(payload=BEGIN),
            ],
            f"{BASE_URL}/_next/static/chunks/pages/_app-abc123.js": fake_response(text=bundle),
            f"{BASE_URL}/": fake_response(text=home),
        },
    )
    episode = corona.episode(EPISODE_URL)

    assert len(episode.pages) == 2
    assert session.calls[:4] == [
        f"{API_URL}/episodes/245764154232569/begin_reading",
        f"{BASE_URL}/",
        f"{BASE_URL}/_next/static/chunks/pages/_app-abc123.js",
        f"{API_URL}/episodes/245764154232569/begin_reading",
    ]
    assert session.headers_seen[-1]["X-API-Environment-Key"] == "fresh-key="


def test_api_gives_up_when_the_bundle_carries_no_key(client, fake_response):
    corona, session = client(
        {
            f"{API_URL}/episodes/245764154232569/begin_reading": fake_response(status_code=HTTPStatus.FORBIDDEN),
            f"{BASE_URL}/": fake_response(text="<html>nothing here</html>"),
        },
    )
    with pytest.raises(Exception, match="403"):
        corona.episode(EPISODE_URL)
    assert session.calls.count(f"{API_URL}/episodes/245764154232569/begin_reading") == 1


# --- a series -------------------------------------------------------------------------


def test_series_urls_follows_the_cursor_and_deduplicates(client):
    corona, session = client()
    urls = corona.series_urls(f"{COMIC_URL}/")

    assert urls == [EPISODE_URL, SECOND_URL, LOCKED_URL]
    assert all(Corona.suitable(url) for url in urls)
    assert session.params_seen[0] == {
        "comic_id": COMIC_ID,
        "limit": 100,
        "sort": "episode_order",
        "order": "asc",
        "episode_status": "free_viewing,only_for_subscription",
    }
    assert session.params_seen[1]["after_than"] == "c1"
    assert session.headers_seen[0]["Referer"] == f"{COMIC_URL}/"


def test_series_urls_raises_for_an_unknown_work(client):
    corona, _ = client()
    with pytest.raises(NotAnEpisodePageError, match="no work 1 "):
        corona.series_urls(f"{BASE_URL}/comics/1")


def test_series_urls_raises_for_an_empty_listing(client):
    corona, _ = client()
    with pytest.raises(NotAnEpisodePageError, match="no episode"):
        corona.series_urls(f"{BASE_URL}/comics/2")


def test_series_urls_rejects_an_episode_url(client):
    corona, _ = client()
    with pytest.raises(UnsupportedUrlError):
        corona.series_urls(EPISODE_URL)


# --- descrambling ---------------------------------------------------------------------


def test_parse_drm_hash_reads_the_grid_and_the_order():
    assert parse_drm_hash("BAQAAQMCCQ8EBgUHCggMDQ4L") == (4, 4, [0, 1, 3, 2, 9, 15, 4, 6, 5, 7, 10, 8, 12, 13, 14, 11])
    assert parse_drm_hash(SHUFFLE) == (2, 2, [1, 2, 3, 0])


@pytest.mark.parametrize(
    "bad",
    [
        "not base64!",
        base64.b64encode(b"\x04").decode(),
        drm_hash(2, 2, [0, 1, 2]),
        drm_hash(2, 2, [0, 1, 1, 2]),
        drm_hash(3, 2, [0, 1, 2, 3]),
    ],
)
def test_parse_drm_hash_rejects_a_hash_that_is_no_grid(bad):
    with pytest.raises(GetjmangaError):
        parse_drm_hash(bad)


@pytest.mark.parametrize(
    ("columns", "rows", "order", "margin"),
    [
        (4, 4, [0, 1, 3, 2, 9, 15, 4, 6, 5, 7, 10, 8, 12, 13, 14, 11], (5, 0)),
        (3, 2, [5, 4, 3, 2, 1, 0], (6, 6)),
    ],
)
def test_descramble_restores_a_tiled_image(columns, rows, order, margin):
    hash_ = drm_hash(columns, rows, order)
    original = tiled_image(columns, rows, margin=margin)
    served = scramble(original, hash_)
    assert served.tobytes() != original.tobytes()

    restored = descramble(served, hash_)

    assert restored.size == original.size
    assert restored.tobytes() == original.tobytes()
    assert served.tobytes() != original.tobytes()  # a copy, not in place


def test_descramble_keeps_the_edge_strip():
    image = tiled_image(2, 2, margin=(5, 3))
    image.paste((0, 0, 255), (16, 0, 21, 19))
    image.paste((0, 255, 0), (0, 16, 16, 19))
    restored = descramble(scramble(image, SHUFFLE), SHUFFLE)
    assert restored.getpixel((18, 5)) == (0, 0, 255)
    assert restored.getpixel((5, 17)) == (0, 255, 0)


def test_image_descrambles_a_page_and_leaves_a_plain_one_alone(client):
    corona, session = client()
    episode = corona.episode(EPISODE_URL)

    scrambled = corona.image(episode.pages[0], episode)
    assert scrambled.tobytes() == tiled_image().tobytes()
    assert session.headers_seen[-1]["Referer"] == EPISODE_URL
    assert "X-API-Environment-Key" not in session.headers_seen[-1]

    plain = corona.image(episode.pages[1], episode)
    assert plain.tobytes() == tiled_image().tobytes()


# --- logging in -----------------------------------------------------------------------


def test_login_posts_the_credentials_to_firebase_and_sends_the_token(client, fake_response):
    corona, session = client({FIREBASE_SIGN_IN_URL: fake_response(payload={"idToken": "tok.en", "email": "a@b"})})
    corona.login(EPISODE_URL, "someone@example.com", "hunter2")

    url, body = session.posts[0]
    assert url == FIREBASE_SIGN_IN_URL
    assert body == {"email": "someone@example.com", "password": "hunter2", "returnSecureToken": True}

    corona.episode(EPISODE_URL)
    assert session.headers_seen[-1]["Authorization"] == "Bearer tok.en"


def test_login_raises_with_the_firebase_reason(client, fake_response):
    refusal = {"error": {"code": 400, "message": "INVALID_LOGIN_CREDENTIALS", "errors": []}}
    corona, _ = client({FIREBASE_SIGN_IN_URL: fake_response(payload=refusal, status_code=HTTPStatus.BAD_REQUEST)})
    with pytest.raises(LoginError, match="INVALID_LOGIN_CREDENTIALS"):
        corona.login(EPISODE_URL, "someone@example.com", "wrong")
    assert corona._token is None


def test_login_raises_when_no_token_comes_back(client, fake_response):
    corona, _ = client({FIREBASE_SIGN_IN_URL: fake_response(text="<html>nope</html>", status_code=HTTPStatus.OK)})
    with pytest.raises(LoginError, match="HTTP 200"):
        corona.login(EPISODE_URL, "someone@example.com", "hunter2")


# --- the real site --------------------------------------------------------------------

# One episode per known host, free to read without an account.
TEST_URLS: dict[str, str] = {
    "to-corona-ex.com": EPISODE_URL,
}


@pytest.mark.network
@pytest.mark.parametrize("host", TEST_URLS)
def test_site_download(tmp_path, host):
    result = Downloader(Corona(), tmp_path, only_first=True).download(TEST_URLS[host])
    assert result.status == "saved"
    assert (result.save_dir / "0.jpg").stat().st_size > 0


@pytest.mark.network
def test_site_locked_episode_has_no_pages():
    episode = Corona().episode(LOCKED_URL)
    assert episode.pages == ()
    assert episode.episode_title == "第3話"


@pytest.mark.network
def test_site_work_page_lists_episodes():
    urls = Corona().series_urls(COMIC_URL)
    assert urls[:3] == [EPISODE_URL, SECOND_URL, LOCKED_URL]
    assert all(Corona.suitable(url) for url in urls)
