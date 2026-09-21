from __future__ import annotations

from http import HTTPStatus
from io import BytesIO

import pytest
from PIL import Image, UnidentifiedImageError

from getjmanga.downloader import Downloader
from getjmanga.errors import LoginError, NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractors.mangabox import (
    API_URL,
    BASE_URL,
    LOGIN_URL,
    Mangabox,
    unmask,
)

SERIES_URL = f"{BASE_URL}/reader/616489/episodes/all/"
EPISODE_URL = f"{BASE_URL}/reader/616489/episodes/217816/"
LOCKED_URL = f"{BASE_URL}/reader/616489/episodes/217860/"
CDN = "https://image-a.mangabox.me/static/content/magazine_episode/616489/l/bba1/webp"

# The site's answer to `get_all_episodes_by_manga_id`, cut down to what is read.
LISTING = {
    "jsonrpc": "2.0",
    "id": None,
    "result": {
        "id": 616489,
        "title": "ネトラセ契約",
        "authors": [{"name": "後藤晶", "id": 13130, "role": "著"}],
        "totalEpisodeCount": 3,
        "episodes": [
            {"id": 217816, "episodeId": 217816, "volume": 1, "displayVolume": None, "numberOfPages": 2},
            {"id": 217817, "episodeId": 217817, "volume": 2, "displayVolume": None, "numberOfPages": 14},
            {"id": 217860, "episodeId": 217860, "volume": 45, "displayVolume": "第45話　完結", "numberOfPages": 11},
        ],
    },
}

# `/api/honshi/episode/<id>/images` for a readable episode.
IMAGE_URLS = [f"{CDN}/001.webp?1787726898", f"{CDN}/002.webp?1787726898"]
IMAGES = {
    "imageUrls": IMAGE_URLS,
    "mask": -7,
    "firstPage": "right",
    "manga": {"id": 616489, "title": "ネトラセ契約"},
    "episodeId": 217816,
    "volume": 1,
    "displayVolume": None,
}

NOT_FOUND = {"error": True, "statusCode": 404, "statusMessage": "Server Error", "message": ""}

LOGIN_FORM = """
<html><body><form action="/browser/auser/login_mail/exec/" method="POST">
<input type="hidden" name="token" value="UF0N5IMygSBHo8XByi2mvGFkh1alwmXdjMyTNbEM">
<input type="email" name="email"><input type="password" name="password">
</form></body></html>
"""
LOGIN_REFUSED = """
<html><body><form action="/browser/auser/login_mail/exec/" method="POST">
<input type="hidden" name="token" value="other"><input type="email" name="email">
<input type="password" name="password">
<p class="txt_err">
      ※
      メールアドレスかパスワードが間違っています。
</p></form></body></html>
"""
LOGIN_DONE = "<html><body><p>マイページ</p></body></html>"


def webp_bytes(color=(1, 2, 3), size=(8, 8)):
    raw = BytesIO()
    Image.new("RGB", size, color).save(raw, "WEBP", lossless=True)
    return raw.getvalue()


@pytest.fixture
def client(fake_session, fake_response):
    """A `Mangabox` over a session answering the listing, the images and a masked page."""

    def build(extra=None):
        routes = {
            "/jsonrpc": fake_response(payload=LISTING),
            "/episode/217816/images": fake_response(payload=IMAGES),
            "/episode/217860/images": fake_response(payload=NOT_FOUND, status_code=HTTPStatus.NOT_FOUND),
            "/episode/999/images": fake_response(payload=NOT_FOUND, status_code=HTTPStatus.NOT_FOUND),
            f"{API_URL}/image": fake_response(unmask(webp_bytes(), -7), content_type="binary/octet-stream"),
        }
        session = fake_session({**(extra or {}), **routes})
        return Mangabox(session), session

    return build


# --- URLs ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        EPISODE_URL,
        "https://www.mangabox.me/reader/616489/episodes/217816",
        "https://mangabox.me/reader/616489/episodes/217816/",
        "https://www.mangabox.me/reader/616489/",
        "https://www.mangabox.me/reader/616489",
        "https://www.mangabox.me/reader/616489/episodes/",
        SERIES_URL,
    ],
)
def test_suitable_accepts_episode_and_series_urls(url):
    assert Mangabox.suitable(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://www.mangabox.me/reader/616489/episodes/217816/",
        "https://www.mangabox.me/",
        "https://www.mangabox.me/reader/",
        "https://www.mangabox.me/reader/tag/genre/1113/",
        "https://www.mangabox.me/browser/store/manga/23055/",
        "https://piccoma.com/web/viewer/8195/1185884",
    ],
)
def test_suitable_rejects_other_urls(url):
    assert not Mangabox.suitable(url)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (EPISODE_URL, False),
        ("https://www.mangabox.me/reader/616489/", True),
        ("https://www.mangabox.me/reader/616489/episodes/", True),
        (SERIES_URL, True),
    ],
)
def test_is_series(url, expected):
    assert Mangabox.is_series(url) is expected


# --- unmasking -------------------------------------------------------------------------


def test_unmask_restores_a_decodable_image():
    masked = unmask(webp_bytes((10, 20, 30)), -38)
    with pytest.raises(UnidentifiedImageError):
        Image.open(BytesIO(masked))
    assert Image.open(BytesIO(unmask(masked, -38))).convert("RGB").getpixel((0, 0)) == (10, 20, 30)


# --- episodes --------------------------------------------------------------------------


def test_episode_reads_the_titles_the_pages_and_the_next_episode(client):
    mangabox, session = client()
    episode = mangabox.episode(EPISODE_URL)

    assert episode.url == EPISODE_URL
    assert episode.series_title == "ネトラセ契約"
    assert (episode.writer, episode.publisher) == ("後藤晶 (著)", "マンガボックス")
    assert episode.episode_title == "第1話"
    assert [page.url for page in episode.pages] == IMAGE_URLS
    assert all(page.extra == {"mask": -7} for page in episode.pages)
    assert episode.next_url == f"{BASE_URL}/reader/616489/episodes/217817/"
    assert episode.metadata["images"] == IMAGES
    assert episode.metadata["episode"]["id"] == 217816

    # The listing is asked for with the viewer's JSON-RPC call, and the API with a site Referer.
    url, body = session.posts[0]
    assert url == f"{API_URL}/jsonrpc"
    assert body == {
        "jsonrpc": "2.0",
        "method": "get_all_episodes_by_manga_id",
        "params": {"mangaId": 616489, "withTags": 1},
    }
    assert session.calls == [f"{API_URL}/episode/217816/images"]
    assert session.headers_seen[-1]["Referer"] == EPISODE_URL


def test_episode_accepts_the_bare_host_and_no_trailing_slash(client):
    mangabox, _ = client()
    episode = mangabox.episode("https://mangabox.me/reader/616489/episodes/217816")
    assert episode.url == EPISODE_URL
    assert len(episode.pages) == 2


def test_locked_episode_has_no_pages_but_keeps_its_titles(client):
    mangabox, _ = client()
    episode = mangabox.episode(LOCKED_URL)

    assert episode.pages == ()
    assert not episode.readable
    assert episode.series_title == "ネトラセ契約"
    assert episode.episode_title == "第45話　完結"
    assert (episode.prev_url, episode.next_url) == (f"{BASE_URL}/reader/616489/episodes/217817/", None)
    assert episode.metadata["images"] is None


def test_last_readable_episode_names_the_locked_one_next(client, fake_response):
    mangabox, _ = client(
        {"/episode/217817/images": fake_response(payload={**IMAGES, "episodeId": 217817, "volume": 2})}
    )
    episode = mangabox.episode(f"{BASE_URL}/reader/616489/episodes/217817/")
    assert episode.episode_title == "第2話"
    assert episode.next_url == LOCKED_URL


def test_unknown_episode_is_not_an_episode_page(client):
    mangabox, _ = client()
    with pytest.raises(NotAnEpisodePageError, match="no episode 999"):
        mangabox.episode(f"{BASE_URL}/reader/616489/episodes/999/")


def test_unknown_series_is_not_an_episode_page(fake_session, fake_response):
    session = fake_session({"/jsonrpc": fake_response(payload=NOT_FOUND, status_code=HTTPStatus.NOT_FOUND)})
    with pytest.raises(NotAnEpisodePageError, match="no series 999"):
        Mangabox(session).episode(f"{BASE_URL}/reader/999/episodes/1/")


def test_episode_off_the_listing_is_still_read_when_the_api_serves_it(client, fake_response):
    # An episode the listing does not carry but `images` answers: readable, no next.
    mangabox, _ = client({"/episode/424242/images": fake_response(payload={**IMAGES, "volume": 7})})
    episode = mangabox.episode(f"{BASE_URL}/reader/616489/episodes/424242/")
    assert episode.episode_title == "第7話"
    assert len(episode.pages) == 2
    assert episode.next_url is None


def test_episode_rejects_a_series_url(client):
    mangabox, _ = client()
    with pytest.raises(UnsupportedUrlError):
        mangabox.episode(SERIES_URL)


# --- series ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    ["https://www.mangabox.me/reader/616489/", "https://www.mangabox.me/reader/616489/episodes/", SERIES_URL],
)
def test_series_urls_lists_the_episodes_in_order_without_repeats(client, url):
    mangabox, _ = client()
    assert mangabox.series_urls(url) == [
        EPISODE_URL,
        f"{BASE_URL}/reader/616489/episodes/217817/",
        LOCKED_URL,
    ]
    assert all(Mangabox.suitable(candidate) for candidate in mangabox.series_urls(url))


def test_series_urls_drops_a_repeated_entry(fake_session, fake_response):
    twice = {
        "jsonrpc": "2.0",
        "id": None,
        "result": {
            "id": 1,
            "title": "x",
            "episodes": [{"id": 5, "volume": 1}, {"id": 5, "volume": 1}, {"id": 6, "volume": 2}],
        },
    }
    session = fake_session({"/jsonrpc": fake_response(payload=twice)})
    assert Mangabox(session).series_urls("https://www.mangabox.me/reader/1/") == [
        f"{BASE_URL}/reader/1/episodes/5/",
        f"{BASE_URL}/reader/1/episodes/6/",
    ]


def test_series_urls_rejects_an_episode_url(client):
    mangabox, _ = client()
    with pytest.raises(UnsupportedUrlError):
        mangabox.series_urls(EPISODE_URL)


def test_series_urls_raises_on_an_empty_listing(fake_session, fake_response):
    empty = {"jsonrpc": "2.0", "id": None, "result": {"id": 1, "title": "x", "episodes": []}}
    session = fake_session({"/jsonrpc": fake_response(payload=empty)})
    with pytest.raises(NotAnEpisodePageError, match="lists no episode"):
        Mangabox(session).series_urls("https://www.mangabox.me/reader/1/")


# --- images ----------------------------------------------------------------------------


def test_image_goes_through_the_proxy_and_is_unmasked(client):
    mangabox, session = client()
    episode = mangabox.episode(EPISODE_URL)
    image = mangabox.image(episode.pages[0], episode)

    assert image.convert("RGB").getpixel((0, 0)) == (1, 2, 3)
    assert session.calls[-1] == f"{API_URL}/image"
    assert session.params_seen[-1] == {"d": IMAGE_URLS[0]}
    assert session.headers_seen[-1]["Referer"] == EPISODE_URL


# --- logging in ------------------------------------------------------------------------


def test_login_posts_the_form_with_its_token(fake_session, fake_response):
    session = fake_session(
        {
            f"{LOGIN_URL}exec/": fake_response(text=LOGIN_DONE),
            LOGIN_URL: fake_response(text=LOGIN_FORM),
        },
    )
    Mangabox(session).login(EPISODE_URL, "someone@example.com", "hunter2")

    assert session.calls == [LOGIN_URL]
    url, body = session.posts[0]
    assert url == f"{LOGIN_URL}exec/"
    assert body == {
        "token": "UF0N5IMygSBHo8XByi2mvGFkh1alwmXdjMyTNbEM",
        "email": "someone@example.com",
        "password": "hunter2",
    }


def test_login_raises_with_the_site_reason(fake_session, fake_response):
    session = fake_session(
        {
            f"{LOGIN_URL}exec/": fake_response(text=LOGIN_REFUSED),
            LOGIN_URL: fake_response(text=LOGIN_FORM),
        },
    )
    with pytest.raises(LoginError, match="間違っています"):
        Mangabox(session).login(EPISODE_URL, "someone@example.com", "wrong")


def test_login_raises_when_the_form_comes_back_without_a_reason(fake_session, fake_response):
    session = fake_session(
        {
            f"{LOGIN_URL}exec/": fake_response(text=LOGIN_FORM),
            LOGIN_URL: fake_response(text=LOGIN_FORM),
        },
    )
    with pytest.raises(LoginError, match="refused the credentials"):
        Mangabox(session).login(EPISODE_URL, "someone@example.com", "wrong")


def test_login_raises_without_a_form(fake_session, fake_response):
    session = fake_session({LOGIN_URL: fake_response(text="<html><body>maintenance</body></html>")})
    with pytest.raises(LoginError, match="no login form"):
        Mangabox(session).login(EPISODE_URL, "someone@example.com", "hunter2")
    assert session.posts == []


# --- the real site ---------------------------------------------------------------------

# One free episode per known host: the first episode of a long-running series.
TEST_URLS: dict[str, str] = {
    "www.mangabox.me": "https://www.mangabox.me/reader/100868/episodes/39548/",
    "mangabox.me": "https://mangabox.me/reader/616489/episodes/217816/",
}


@pytest.mark.network
@pytest.mark.geoblocked
@pytest.mark.parametrize("host", TEST_URLS)
def test_site_download(tmp_path, host):
    result = Downloader(Mangabox(), tmp_path, only_first=True).download(TEST_URLS[host])
    assert result.status == "saved"
    assert (result.save_dir / "0.jpg").stat().st_size > 0


@pytest.mark.network
@pytest.mark.geoblocked
def test_site_locked_episode_has_no_pages():
    episode = Mangabox().episode("https://www.mangabox.me/reader/616489/episodes/217860/")
    assert not episode.readable
    assert episode.episode_title == "第45話　完結"


@pytest.mark.network
@pytest.mark.geoblocked
def test_site_series_lists_episodes():
    urls = Mangabox().series_urls("https://www.mangabox.me/reader/616489/")
    assert urls[0] == "https://www.mangabox.me/reader/616489/episodes/217816/"
    assert all(Mangabox.suitable(url) for url in urls)
