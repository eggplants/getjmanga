from __future__ import annotations

import base64
import hashlib
import hmac
import json
from http import HTTPStatus
from io import BytesIO
from urllib.parse import parse_qs

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from PIL import Image

from getjmanga.downloader import Downloader
from getjmanga.errors import GetjmangaError, LoginError, NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractors.beltoon import (
    API_URL,
    BASE_URL,
    EMAIL_LOGIN_SECRET,
    FALLBACK_SCRAMBLE_KEY,
    BeLToon,
    decrypt_index,
    descramble,
)

EPISODE_URL = f"{BASE_URL}/viewer/12s1/1"
LOCKED_URL = f"{BASE_URL}/viewer/12s1/2"
SERIES_URL = f"{BASE_URL}/detail/12s1"

IMAGE_URL = "https://image.balcony.studio/jp/ep/1389/52369/{}.webp?Policy=abc&Signature=def&Key-Pair-Id=ghi"

# The work API's episode rows, as the site lists them: reading order, one free.
EPISODES = [
    {"id": 2, "alias": "1", "title": "1話", "orderNo": 1, "isLogin": False, "possessionCoin": 0},
    {"id": 1058, "alias": "2", "title": "2話", "orderNo": 2, "isLogin": True, "possessionCoin": 0},
    {"id": 1067, "alias": "3", "title": "3話 【1部 完】", "orderNo": 3, "isLogin": True, "possessionCoin": 62},
]
WORK = {
    "id": 2,
    "alias": "12s1",
    "title": "片思い〜報われない恋をした〜",
    "isAdult": True,
    "episodes": EPISODES,
    "creators": [
        {"creatorId": 3, "name": "TR", "type": "ORIGINAL"},
        {"creatorId": 1256, "name": "黄金期", "type": "AUTHOR"},
    ],
}

#: A permutation: source tile `i` lands in slot `INDEX[i]`.
INDEX = [5, 0, 15, 8, 1, 14, 3, 10, 13, 6, 9, 2, 12, 7, 4, 11]
KEY = "3XMSKDsPwBzPDnw7n16M84RtYk0PtmH6"


def image_entry(order, **overrides):
    entry = {
        "code": None,
        "defaultHeight": 64,
        "type": None,
        "order": order,
        "width": 32,
        "height": 64,
        "imagePath": IMAGE_URL.format(order),
        "contentText": None,
        "scrambleIndex": None,
        "expiredAt": 1789706080304,
        "line": None,
        "point": None,
    }
    return {**entry, **overrides}


def viewer_result(images=None, **overrides):
    result = {
        "contentId": 2,
        "episodeId": 2,
        "title": "1話",
        "subTitle": "",
        "contentType": "IMAGE",
        "contentsType": "COMIC",
        "isScramble": False,
        "prevEpisode": None,
        "nextEpisode": None,
        "viewerType": "SCROLL",
        "paperDirection": "LTR",
        "contentsAlias": "12s1",
        "contentsTitle": "片思い〜報われない恋をした〜",
        "episodeAlias": "1",
        "images": [image_entry(2), image_entry(1)] if images is None else images,
    }
    return {**result, **overrides}


def viewer_html(episode_data, page="/viewer/[alias]/[epAlias]"):
    data = {"props": {"pageProps": {"isFirst": True, "episodeData": episode_data}}, "page": page}
    return f'<html><body><script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script></body></html>'


def encrypt_index(index, key=KEY):
    plain = json.dumps(index).encode()
    padding = 16 - len(plain) % 16
    plain += bytes([padding]) * padding
    key_bytes = key.encode()
    encryptor = Cipher(algorithms.AES(key_bytes), modes.CBC(key_bytes[:16])).encryptor()
    return base64.b64encode(encryptor.update(plain) + encryptor.finalize()).decode()


def tiled_image(size=(32, 64), grid=4):
    """One flat colour per tile, so a shuffle is visible."""
    image = Image.new("RGB", size)
    tile_width, tile_height = size[0] // grid, size[1] // grid
    for slot in range(grid * grid):
        left, top = slot % grid * tile_width, slot // grid * tile_height
        image.paste((slot * 16, 255 - slot * 16, 100), (left, top, left + tile_width, top + tile_height))
    return image


def scrambled(image, index, grid=4):
    """Do what the site does: tile `index[i]` of the original is served at slot `i`."""
    width, height = image.size
    tile_width, tile_height = width // grid, height // grid
    served = Image.new(image.mode, image.size)
    for source, target in enumerate(index):
        left, top = target % grid * tile_width, target // grid * tile_height
        tile = image.crop((left, top, left + tile_width, top + tile_height))
        served.paste(tile, (source % grid * tile_width, source // grid * tile_height))
    return served


def png_bytes(image):
    raw = BytesIO()
    image.save(raw, "PNG")
    return raw.getvalue()


@pytest.fixture
def client(fake_session, fake_response):
    def build(routes=None):
        merged = dict(routes or {})
        merged.setdefault("/viewer/", fake_response(text=viewer_html({"result": viewer_result()})))
        merged.setdefault(f"{API_URL}/contents/12s1", fake_response(payload={"result": "SUCCESS", "data": WORK}))
        merged.setdefault("image.balcony.studio", fake_response(png_bytes(tiled_image()), content_type="image/webp"))
        session = fake_session(merged)
        return BeLToon(session), session

    return build


# --- descrambling -------------------------------------------------------------------


def test_descramble_restores_a_tiled_image():
    original = tiled_image()
    restored = descramble(scrambled(original, INDEX), INDEX)
    assert restored.tobytes() == original.tobytes()


def test_descramble_rejects_a_broken_index():
    with pytest.raises(GetjmangaError, match="not a permutation"):
        descramble(tiled_image(), [0] * 16)


def test_decrypt_index_reads_the_permutation():
    assert decrypt_index(encrypt_index(INDEX), KEY) == INDEX


def test_decrypt_index_rejects_the_wrong_key():
    with pytest.raises(GetjmangaError, match="padding"):
        decrypt_index(encrypt_index(INDEX), "x" * 32)


def test_decrypt_index_rejects_a_key_of_the_wrong_size():
    with pytest.raises(GetjmangaError, match="cannot be decrypted"):
        decrypt_index(encrypt_index(INDEX), "short")


# --- urls ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (EPISODE_URL, True),
        (EPISODE_URL + "/", True),
        (f"{BASE_URL}/viewer/picnse00026/p1", True),
        (f"{BASE_URL}/viewer/M_628661/1?isSample=true", True),
        (SERIES_URL, True),
        (SERIES_URL + "/", True),
        ("http://www.beltoon.jp/viewer/12s1/1", False),
        ("https://beltoon.jp/viewer/12s1/1", False),
        (f"{BASE_URL}/viewer/12s1", False),
        (f"{BASE_URL}/comment/12s1/1", False),
        (f"{BASE_URL}/", False),
        ("https://example.com/viewer/12s1/1", False),
    ],
)
def test_suitable(url, expected):
    assert BeLToon.suitable(url) is expected


def test_is_series_means_a_work_page():
    assert BeLToon.is_series(SERIES_URL)
    assert not BeLToon.is_series(EPISODE_URL)
    assert not BeLToon.is_series("https://example.com/detail/12s1")


# --- reading an episode ------------------------------------------------------------


def test_episode_reads_the_titles_the_pages_and_the_next_episode(client):
    beltoon, _ = client()
    episode = beltoon.episode(EPISODE_URL)

    assert episode.series_title == "片思い〜報われない恋をした〜"
    assert episode.episode_title == "1話"
    assert (episode.writer, episode.publisher) == ("TR, 黄金期", "レジンエンターテインメント")
    # Pages come back in `order`, whatever order the site listed them in.
    assert [page.url for page in episode.pages] == [IMAGE_URL.format(1), IMAGE_URL.format(2)]
    assert episode.pages[0].width == 32
    assert episode.pages[0].height == 64
    assert episode.pages[0].extra == {"scramble": []}
    assert episode.next_url == LOCKED_URL
    assert episode.metadata["episodeId"] == 2
    assert "images" not in episode.metadata
    json.dumps(episode.metadata)


def test_episode_sets_the_age_gate_cookie_and_sends_the_platform_headers(client):
    beltoon, session = client()
    beltoon.episode(EPISODE_URL)

    assert session.cookies.get("not-login-adult", domain="www.beltoon.jp") == "Y"
    assert session.calls[0] == EPISODE_URL
    assert session.calls[1] == f"{API_URL}/contents/12s1"
    assert session.params_seen[1] == {"isNotLoginAdult": "true", "isPorch": "false"}
    assert session.headers_seen[1]["x-balcony-id"] == "BELTOON_JP"
    assert session.headers_seen[1]["x-platform"] == "WEB"
    assert session.headers_seen[1]["Accept"] == "application/json"


def test_episode_keeps_the_sample_flag_and_normalises_the_url(client):
    beltoon, session = client()
    episode = beltoon.episode(f"{BASE_URL}/viewer/12s1/1/?isSample=true&utm=x")
    assert episode.url == f"{EPISODE_URL}?isSample=true"
    assert session.calls[0] == f"{EPISODE_URL}?isSample=true"


def test_episode_stops_at_the_last_episode(client, fake_response):
    beltoon, _ = client({"/viewer/": fake_response(text=viewer_html({"result": viewer_result(episodeAlias="3")}))})
    episode = beltoon.episode(f"{BASE_URL}/viewer/12s1/3")
    assert (episode.prev_url, episode.next_url) == (LOCKED_URL, None)


def test_episode_skips_rows_without_an_image(client, fake_response):
    images = [image_entry(1), image_entry(2, imagePath=None)]
    beltoon, _ = client({"/viewer/": fake_response(text=viewer_html({"result": viewer_result(images=images)}))})
    assert [page.url for page in beltoon.episode(EPISODE_URL).pages] == [IMAGE_URL.format(1)]


def test_episode_reads_a_plain_scramble_index(client, fake_response):
    images = [image_entry(1, scrambleIndex=INDEX)]
    result = viewer_result(images=images, isScramble=True)
    beltoon, session = client({"/viewer/": fake_response(text=viewer_html({"result": result}))})
    episode = beltoon.episode(EPISODE_URL)
    assert episode.pages[0].extra == {"scramble": INDEX}
    assert session.posts == []


def test_episode_asks_for_the_key_and_decrypts_the_scramble_index(client, fake_response):
    images = [
        image_entry(1, line="ln", point=encrypt_index(INDEX)),
        image_entry(2, line="ln", point=encrypt_index(INDEX)),
    ]
    result = viewer_result(images=images, isScramble=True)
    beltoon, session = client(
        {
            "/viewer/": fake_response(text=viewer_html({"result": result})),
            "/contents/images/2/2": fake_response(payload={"result": "SUCCESS", "data": KEY}),
        },
    )
    episode = beltoon.episode(EPISODE_URL)

    assert [page.extra["scramble"] for page in episode.pages] == [INDEX, INDEX]
    assert session.posts == [(f"{API_URL}/contents/images/2/2", {"line": "ln"})]


def test_episode_falls_back_to_the_bundled_key(client, fake_response):
    images = [image_entry(1, line="ln", point=encrypt_index(INDEX, FALLBACK_SCRAMBLE_KEY))]
    result = viewer_result(images=images, isScramble=True)
    beltoon, _ = client(
        {
            "/viewer/": fake_response(text=viewer_html({"result": result})),
            "/contents/images/2/2": fake_response(payload={"result": "ERROR", "error": {"code": "UNKNOWN"}}),
        },
    )
    assert beltoon.episode(EPISODE_URL).pages[0].extra["scramble"] == INDEX


def test_a_locked_episode_has_no_pages_but_a_next_url(client, fake_response):
    error = {"code": "NOT_LOGIN_USER", "message": "NotLoginUser", "detail": ""}
    beltoon, _ = client({"/viewer/": fake_response(text=viewer_html({"error": error}))})
    episode = beltoon.episode(LOCKED_URL)

    assert episode.pages == ()
    assert episode.series_title == "片思い〜報われない恋をした〜"
    assert episode.episode_title == "2話"
    assert episode.writer == "TR, 黄金期"
    assert (episode.prev_url, episode.next_url) == (EPISODE_URL, f"{BASE_URL}/viewer/12s1/3")
    assert episode.metadata["error"] == error
    json.dumps(episode.metadata)


def test_an_adult_gated_episode_counts_as_locked(client, fake_response):
    error = {"code": "ADULT_ONLY_CONTENTS", "message": "", "detail": ""}
    beltoon, _ = client({"/viewer/": fake_response(text=viewer_html({"error": error}))})
    assert beltoon.episode(EPISODE_URL).pages == ()


@pytest.mark.parametrize("code", ["NOT_FOUND_EPISODE", "NOT_EXIST", "INVALID_CONTENTS"])
def test_episode_refuses_an_episode_that_does_not_exist(client, fake_response, code):
    error = {"code": code, "message": "", "detail": ""}
    beltoon, _ = client({"/viewer/": fake_response(text=viewer_html({"error": error}))})
    with pytest.raises(NotAnEpisodePageError, match=code):
        beltoon.episode(f"{BASE_URL}/viewer/12s1/9999")


def test_episode_refuses_a_page_without_viewer_data(client, fake_response):
    beltoon, _ = client({"/viewer/": fake_response(text="<html><body>maintenance</body></html>")})
    with pytest.raises(NotAnEpisodePageError, match="__NEXT_DATA__"):
        beltoon.episode(EPISODE_URL)


def test_episode_refuses_an_ebook_episode(client, fake_response):
    result = viewer_result(contentType="EPUB", images=[image_entry(None, imagePath="https://mbj.balcony.studio/x.zip")])
    beltoon, _ = client({"/viewer/": fake_response(text=viewer_html({"result": result}))})
    with pytest.raises(NotAnEpisodePageError, match="EPUB"):
        beltoon.episode(EPISODE_URL)


def test_episode_refuses_a_work_page(client):
    beltoon, _ = client()
    with pytest.raises(UnsupportedUrlError, match="not a viewer"):
        beltoon.episode(SERIES_URL)


# --- listing a work --------------------------------------------------------------------


def test_series_urls_lists_the_episodes_in_reading_order(client, fake_response):
    shuffled = {**WORK, "episodes": [EPISODES[2], EPISODES[0], EPISODES[1], EPISODES[0]]}
    beltoon, _ = client({f"{API_URL}/contents/12s1": fake_response(payload={"result": "SUCCESS", "data": shuffled})})
    assert beltoon.series_urls(SERIES_URL) == [f"{BASE_URL}/viewer/12s1/{alias}" for alias in ("1", "2", "3")]


def test_series_urls_rejects_a_work_without_episodes(client, fake_response):
    empty = {**WORK, "episodes": []}
    beltoon, _ = client({f"{API_URL}/contents/12s1": fake_response(payload={"result": "SUCCESS", "data": empty})})
    with pytest.raises(NotAnEpisodePageError, match="lists no episode"):
        beltoon.series_urls(SERIES_URL)


def test_series_urls_rejects_a_work_that_does_not_exist(client, fake_response):
    answer = {"result": "ERROR", "error": {"code": "NOT_EXIST", "message": "NotExist", "detail": ""}}
    beltoon, _ = client({f"{API_URL}/contents/12s1": fake_response(payload=answer)})
    with pytest.raises(NotAnEpisodePageError, match="no work"):
        beltoon.series_urls(SERIES_URL)


def test_series_urls_rejects_a_viewer_url(client):
    beltoon, _ = client()
    with pytest.raises(UnsupportedUrlError, match="not a work page"):
        beltoon.series_urls(EPISODE_URL)


# --- downloading --------------------------------------------------------------------------


def test_download_writes_the_pages(client, tmp_path):
    beltoon, session = client()
    result = Downloader(beltoon, tmp_path).download(EPISODE_URL)

    assert result.status == "saved"
    assert result.save_dir == tmp_path / "www.beltoon.jp" / "片思い〜報われない恋をした〜" / "1話"
    assert sorted(path.name for path in result.save_dir.iterdir()) == ["0.jpg", "1.jpg"]
    with Image.open(result.save_dir / "0.jpg") as saved:
        assert saved.size == (32, 64)
        assert saved.getpixel((4, 4)) == pytest.approx((0, 255, 100), abs=8)
    assert session.headers_seen[-1]["Referer"] == EPISODE_URL


# --- logging in -----------------------------------------------------------------------------


def test_login_posts_the_signed_credentials(client, fake_response):
    beltoon, session = client(
        {
            "/api/auth/csrf": fake_response(payload={"csrfToken": "tok"}),
            "/api/auth/callback/EmailLogin": fake_response(payload={"url": f"{BASE_URL}/callback/login-success"}),
        },
    )
    beltoon.login(EPISODE_URL, "someone@example.com", "hunter2")

    url, body = session.posts[0]
    assert url == f"{BASE_URL}/api/auth/callback/EmailLogin"
    assert body["email"] == "someone@example.com"
    assert body["password"] == "hunter2"
    assert body["csrfToken"] == "tok"
    assert body["json"] == "true"
    assert body["platform"] == "WEB"
    expected = hmac.new(EMAIL_LOGIN_SECRET.encode(), b"someone@example.comhunter2", hashlib.sha1).hexdigest()
    assert body["qnwjdghldnjsrkdlq"] == expected
    assert parse_qs(body["callbackUrl"].split("?", 1)[1]) == {"callback": ["/"]}


def test_login_raises_with_the_site_reason(client, fake_response):
    landing = f"{BASE_URL}/api/auth/error?error=NOT_REGISTERED_USER%26id%3Dsomeone%2540example.com%26type%3Demail"
    beltoon, _ = client(
        {
            "/api/auth/csrf": fake_response(payload={"csrfToken": "tok"}),
            "/api/auth/callback/EmailLogin": fake_response(
                payload={"url": landing}, status_code=HTTPStatus.UNAUTHORIZED
            ),
        },
    )
    with pytest.raises(LoginError, match="NOT_REGISTERED_USER"):
        beltoon.login(EPISODE_URL, "someone@example.com", "wrong")


def test_login_raises_when_the_site_names_no_landing_page(client, fake_response):
    beltoon, _ = client(
        {
            "/api/auth/csrf": fake_response(payload={"csrfToken": "tok"}),
            "/api/auth/callback/EmailLogin": fake_response(
                text="<html>oops</html>", status_code=HTTPStatus.BAD_GATEWAY
            ),
        },
    )
    with pytest.raises(LoginError, match="HTTP 502"):
        beltoon.login(EPISODE_URL, "someone@example.com", "hunter2")


def test_login_raises_without_a_csrf_token(client, fake_response):
    beltoon, _ = client({"/api/auth/csrf": fake_response(payload={})})
    with pytest.raises(LoginError, match="CSRF"):
        beltoon.login(EPISODE_URL, "someone@example.com", "hunter2")


# --- the real site -----------------------------------------------------------------------------

# One episode per known host, free to read without an account.
TEST_URLS: dict[str, str] = {
    "www.beltoon.jp": "https://www.beltoon.jp/viewer/12s1/1",
}


@pytest.mark.network
@pytest.mark.parametrize("host", TEST_URLS)
def test_site_download(tmp_path, host):
    result = Downloader(BeLToon(), tmp_path, only_first=True).download(TEST_URLS[host])
    assert result.status == "saved"
    assert (result.save_dir / "0.jpg").stat().st_size > 0


@pytest.mark.network
def test_site_paper_episode_download(tmp_path):
    result = Downloader(BeLToon(), tmp_path, only_first=True).download("https://www.beltoon.jp/viewer/picnse00026/p1")
    assert result.status == "saved"
    assert (result.save_dir / "0.jpg").stat().st_size > 0


@pytest.mark.network
def test_site_locked_episode_has_no_pages():
    episode = BeLToon().episode("https://www.beltoon.jp/viewer/12s1/11")
    assert episode.pages == ()
    assert episode.episode_title == "11話"
    assert episode.next_url == "https://www.beltoon.jp/viewer/12s1/12"


@pytest.mark.network
def test_site_work_page_lists_episodes():
    urls = BeLToon().series_urls("https://www.beltoon.jp/detail/12s1")
    assert urls[0] == "https://www.beltoon.jp/viewer/12s1/1"
    assert all(BeLToon.suitable(url) for url in urls)
