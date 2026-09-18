from __future__ import annotations

import base64
import hashlib
import hmac
import json
import struct
from http import HTTPStatus
from io import BytesIO

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from PIL import Image

from getjmanga.downloader import Downloader
from getjmanga.extractors.common import GetjmangaError, LoginError, NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractors.drecomi import API_URL, BASE_URL, LIST_LIMIT, Drecomi, decrypt, verify

SERIES_URL = f"{BASE_URL}/series/CD20013"
EPISODE_URL = f"{BASE_URL}/series/CD20013/episodes/CD20013-001-001"
SECOND_URL = f"{BASE_URL}/series/CD20013/episodes/CD20013-001-002"
LOCKED_URL = f"{BASE_URL}/series/CD20013/episodes/CD20013-001-003"
CDN = "https://cdn.drecomi-plus.jp/contents/CD20013/episodes"

SESSION_KEY = base64.b64encode(bytes(range(32))).decode()
HMAC_KEY = base64.b64encode(bytes(range(32, 64))).decode()
IV1 = base64.b64encode(bytes(range(16))).decode()
IV2 = base64.b64encode(bytes(range(16, 32))).decode()


def encrypt(plain, key=SESSION_KEY, iv=IV1):
    padding = 16 - len(plain) % 16
    encryptor = Cipher(algorithms.AES(base64.b64decode(key)), modes.CBC(base64.b64decode(iv))).encryptor()
    return encryptor.update(plain + bytes([padding]) * padding) + encryptor.finalize()


def sign(data, iv, content_id, page_number, *, hmac_key=HMAC_KEY, content_type="episode"):
    pairs = [
        {"k": "content_id", "v": str(content_id)},
        {"k": "content_type", "v": content_type},
        {"k": "page_number", "v": str(page_number)},
    ]
    aad = json.dumps(pairs, separators=(",", ":")).encode()
    message = b"".join(struct.pack(">II", 0, len(part)) + part for part in (data, base64.b64decode(iv), aad))
    return base64.b64encode(hmac.new(base64.b64decode(hmac_key), message, hashlib.sha256).digest()).decode()


def webp_bytes(color=(1, 2, 3), size=(8, 8)):
    raw = BytesIO()
    Image.new("RGB", size, color).save(raw, "WEBP", lossless=True)
    return raw.getvalue()


PAGE1_ENC = encrypt(webp_bytes((1, 2, 3)), iv=IV1)
PAGE2_ENC = encrypt(webp_bytes((4, 5, 6)), iv=IV2)


def detail(code, number, name, price=0, series="CD20013"):
    return {
        "actual_price": price,
        "code": code,
        "episode_number": number,
        "first_page_blank": True,
        "id": 1036 + number,
        "is_latest": False,
        "is_published": True,
        "name": name,
        "page_count": 2,
        "price_coins": price,
        "reading_direction": 1,
        "series_code": series,
        "series_id": 41,
        "series_title": "毒姫は呪われた指先に春を乞う",
    }


def entry(code, number, name, price=0):
    return {
        "actual_price": price,
        "code": code,
        "episode_number": number,
        "id": 1036 + number,
        "is_purchased": False,
        "name": name,
        "price_coins": price,
        "publish_at": "2026-09-01T00:00:00+09:00",
        "thumbnail": {"cdn_url": f"{CDN}/{code}/thumbnails/x.webp"},
    }


ENTRIES = [
    entry("CD20013-001-001", 1, "第1話（1）"),
    entry("CD20013-001-002", 2, "第1話（2）"),
    entry("CD20013-001-003", 3, "第2話（1）", price=80),
]


def page_entry(number, data, iv, content_id=1037):
    return {
        "auth_tag": sign(data, iv, content_id, number),
        "content_type": "application/octet-stream",
        "image_url": f"{CDN}/CD20013-001-001/pages/{number:016x}_{number:04d}.webp.enc",
        "iv": iv,
        "page_number": number,
    }


# `POST /viewer/episodes/<code>/session` for a readable episode; the pages arrive out of order on purpose.
PAGES = [page_entry(2, PAGE2_ENC, IV2), page_entry(1, PAGE1_ENC, IV1)]
VIEWER = {
    "content_id": 1037,
    "content_type": "episode",
    "episode_code": "CD20013-001-001",
    "episode_name": "第1話（1）",
    "expires_at": "2026-09-18T13:45:29+09:00",
    "hmac_key": HMAC_KEY,
    "is_first_page_blank": True,
    "pages": PAGES,
    "reading_direction": 1,
    "session_key": SESSION_KEY,
    "session_token": "tok",
}
NOT_FOUND = {"error": "Episode not found", "code": "EPISODE_NOT_FOUND"}
AUTH_REQUIRED = {"error": "Authentication required for paid content", "code": "AUTHENTICATION_REQUIRED"}


def listing(items, page=1, per_page=LIST_LIMIT, total=None):
    total = len(items) if total is None else total
    return {
        "items": items,
        "pagination": {
            "currentPage": page,
            "itemsPerPage": per_page,
            "totalItems": total,
            "totalPages": -(-total // per_page),
        },
    }


@pytest.fixture
def client(fake_session, fake_response):
    """A `Drecomi` over a session answering the API, the viewer session and two encrypted pages.

    The listing is asked for by query, so the routes match on the URL with
    its query string appended.
    """

    class QuerySession(fake_session):
        def get(self, url, **kwargs):
            params = kwargs.get("params") or {}
            query = "&".join(f"{key}={value}" for key, value in params.items())
            self.calls.append(url)
            self.params_seen.append(kwargs.get("params"))
            self.headers_seen.append(kwargs.get("headers") or {})
            return self._route(f"{url}?{query}" if query else url)

        def post(self, url, data=None, json=None, **kwargs):
            self.posts.append((url, data if data is not None else json))
            self.headers_seen.append(kwargs.get("headers") or {})
            return self._route(url)

    def build(extra=None):
        routes = {
            f"{API_URL}/episodes/CD20013-001-001/next": fake_response(payload=ENTRIES[1]),
            f"{API_URL}/episodes/CD20013-001-002/next": fake_response(payload=ENTRIES[2]),
            f"{API_URL}/episodes/CD20013-001-003/next": fake_response(
                payload=NOT_FOUND, status_code=HTTPStatus.NOT_FOUND
            ),
            f"{API_URL}/episodes/CD20013-001-001": fake_response(payload=detail("CD20013-001-001", 1, "第1話（1）")),
            f"{API_URL}/episodes/CD20013-001-002": fake_response(payload=detail("CD20013-001-002", 2, "第1話（2）")),
            f"{API_URL}/episodes/CD20013-001-003": fake_response(
                payload=detail("CD20013-001-003", 3, "第2話（1）", 80)
            ),
            f"{API_URL}/episodes/": fake_response(payload=NOT_FOUND, status_code=HTTPStatus.NOT_FOUND),
            f"{API_URL}/episodes?series_code=CD20013&": fake_response(payload=listing(ENTRIES)),
            f"{API_URL}/episodes?series_code=": fake_response(
                payload={"error": "Series not found", "code": "SERIES_NOT_FOUND"}, status_code=HTTPStatus.NOT_FOUND
            ),
            f"{API_URL}/viewer/episodes/CD20013-001-001/session": fake_response(
                payload=VIEWER, status_code=HTTPStatus.CREATED
            ),
            f"{API_URL}/viewer/episodes/CD20013-001-002/session": fake_response(
                payload={**VIEWER, "content_id": 1038, "episode_code": "CD20013-001-002", "episode_name": "第1話（2）"},
                status_code=HTTPStatus.CREATED,
            ),
            f"{API_URL}/viewer/episodes/CD20013-001-003/session": fake_response(
                payload=AUTH_REQUIRED, status_code=HTTPStatus.UNAUTHORIZED
            ),
            "_0001.webp.enc": fake_response(PAGE1_ENC, content_type="application/octet-stream"),
            "_0002.webp.enc": fake_response(PAGE2_ENC, content_type="application/octet-stream"),
        }
        # A caller's routes come first, so they win over the defaults (first match wins).
        merged = dict(extra or {})
        for needle, response in routes.items():
            merged.setdefault(needle, response)
        session = QuerySession(merged)
        return Drecomi(session), session

    return build


# --- URLs ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        EPISODE_URL,
        f"{EPISODE_URL}/",
        f"{EPISODE_URL}?from=list",
        SERIES_URL,
        f"{SERIES_URL}/",
        "https://drecomi-plus.jp/series/CD00007/episodes/CD00007-010-081",
    ],
)
def test_suitable_accepts_episode_and_series_urls(url):
    assert Drecomi.suitable(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://drecomi-plus.jp/series/CD20013/episodes/CD20013-001-001",
        "https://drecomi-plus.jp/",
        "https://drecomi-plus.jp/series",
        "https://drecomi-plus.jp/series/",
        "https://drecomi-plus.jp/series/genre?id=1&name=x",
        "https://drecomi-plus.jp/series/author?id=105&name=x",
        "https://drecomi-plus.jp/series/label",
        "https://drecomi-plus.jp/series/CD20013/episodes/oldest",
        "https://drecomi-plus.jp/series/CD20013/volumes/CD20013-001",
        "https://drecomi-plus.jp/series/CD20013/episodes/CD20013-001-001/comments",
        "https://drecomi-plus.jp/login",
        "https://api.drecomi-plus.jp/api/v1/app/series/CD20013",
        "https://cdn.drecomi-plus.jp/contents/CD20013/thumbnails/x.webp",
        "https://piccoma.com/web/viewer/8195/1185884",
    ],
)
def test_suitable_rejects_other_urls(url):
    assert not Drecomi.suitable(url)


@pytest.mark.parametrize(
    ("url", "expected"),
    [(EPISODE_URL, False), (SERIES_URL, True), (f"{SERIES_URL}/", True)],
)
def test_is_series(url, expected):
    assert Drecomi.is_series(url) is expected


# --- decryption ------------------------------------------------------------------------


def test_decrypt_restores_the_image():
    plain = decrypt(PAGE1_ENC, SESSION_KEY, IV1)
    assert plain == webp_bytes((1, 2, 3))
    assert Image.open(BytesIO(plain)).convert("RGB").getpixel((0, 0)) == (1, 2, 3)


def test_decrypt_rejects_a_partial_block():
    with pytest.raises(GetjmangaError, match="whole number of AES blocks"):
        decrypt(PAGE1_ENC[:-1], SESSION_KEY, IV1)
    with pytest.raises(GetjmangaError, match="whole number of AES blocks"):
        decrypt(b"", SESSION_KEY, IV1)


def test_decrypt_rejects_the_wrong_key():
    with pytest.raises(GetjmangaError, match="PKCS#7"):
        decrypt(PAGE1_ENC, HMAC_KEY, IV1)


def test_verify_accepts_the_viewer_tag_and_rejects_a_changed_file():
    identity = {"content_id": "1037", "content_type": "episode", "page_number": 1}
    tag = PAGES[1]["auth_tag"]
    assert verify(PAGE1_ENC, HMAC_KEY, IV1, tag, identity)
    assert not verify(PAGE1_ENC[:-1] + bytes([PAGE1_ENC[-1] ^ 1]), HMAC_KEY, IV1, tag, identity)
    assert not verify(PAGE1_ENC, HMAC_KEY, IV1, tag, {**identity, "page_number": 2})
    assert not verify(PAGE1_ENC, HMAC_KEY, IV1, tag, {**identity, "content_id": 1038})
    assert not verify(PAGE1_ENC, SESSION_KEY, IV1, tag, identity)
    # The viewer signs the id as a string; an int in `extra` must give the same tag.
    assert verify(PAGE1_ENC, HMAC_KEY, IV1, tag, {**identity, "content_id": 1037, "page_number": "1"})


# --- episodes --------------------------------------------------------------------------


def test_episode_reads_the_titles_the_pages_and_the_next_episode(client):
    drecomi, session = client()
    episode = drecomi.episode(EPISODE_URL)

    assert episode.url == EPISODE_URL
    assert episode.series_title == "毒姫は呪われた指先に春を乞う"
    assert episode.episode_title == "第1話（1）"
    # Pages come back in `page_number` order, whatever order the session listed them in.
    assert [page.url for page in episode.pages] == [PAGES[1]["image_url"], PAGES[0]["image_url"]]
    assert episode.pages[0].extra == {
        "key": SESSION_KEY,
        "hmac_key": HMAC_KEY,
        "iv": IV1,
        "auth_tag": PAGES[1]["auth_tag"],
        "content_id": "1037",
        "content_type": "episode",
        "page_number": 1,
    }
    assert episode.next_url == SECOND_URL
    assert episode.metadata["episode"]["code"] == "CD20013-001-001"
    assert episode.metadata["viewer"] == VIEWER
    assert episode.metadata["next"] == ENTRIES[1]
    json.dumps(episode.metadata)
    json.dumps([dict(page.extra) for page in episode.pages])

    # The detail and the next episode are GETs, the viewer session a POST, all as JSON from the site's origin.
    assert session.calls == [f"{API_URL}/episodes/CD20013-001-001", f"{API_URL}/episodes/CD20013-001-001/next"]
    assert session.posts == [(f"{API_URL}/viewer/episodes/CD20013-001-001/session", None)]
    assert all(headers["Accept"] == "application/json" for headers in session.headers_seen)
    assert all(headers["Origin"] == BASE_URL for headers in session.headers_seen)
    assert all("Authorization" not in headers for headers in session.headers_seen)


def test_episode_accepts_a_trailing_slash_and_a_query(client):
    drecomi, _ = client()
    episode = drecomi.episode(f"{EPISODE_URL}/?from=list")
    assert episode.url == EPISODE_URL
    assert len(episode.pages) == 2


def test_locked_episode_has_no_pages_but_keeps_its_titles(client):
    drecomi, session = client()
    episode = drecomi.episode(LOCKED_URL)

    assert episode.pages == ()
    assert not episode.readable
    assert episode.series_title == "毒姫は呪われた指先に春を乞う"
    assert episode.episode_title == "第2話（1）"
    assert episode.next_url is None
    assert episode.metadata["viewer"] is None
    assert episode.metadata["next"] is None
    assert session.posts == [(f"{API_URL}/viewer/episodes/CD20013-001-003/session", None)]


def test_last_readable_episode_names_the_locked_one_next(client):
    drecomi, _ = client()
    episode = drecomi.episode(SECOND_URL)
    assert episode.episode_title == "第1話（2）"
    assert episode.next_url == LOCKED_URL


@pytest.mark.parametrize("status", [HTTPStatus.FORBIDDEN, HTTPStatus.NOT_FOUND])
def test_viewer_refusing_means_locked(client, fake_response, status):
    drecomi, _ = client(
        {
            f"{API_URL}/viewer/episodes/CD20013-001-001/session": fake_response(
                payload={"error": "no", "code": "PURCHASE_REQUIRED"}, status_code=status
            )
        }
    )
    episode = drecomi.episode(EPISODE_URL)
    assert not episode.readable
    assert episode.episode_title == "第1話（1）"
    assert episode.next_url == SECOND_URL


def test_viewer_failing_otherwise_raises(client, fake_response):
    drecomi, _ = client(
        {
            f"{API_URL}/viewer/episodes/CD20013-001-001/session": fake_response(
                text="<html>gateway</html>", status_code=HTTPStatus.BAD_GATEWAY
            )
        }
    )
    with pytest.raises(Exception, match="502"):
        drecomi.episode(EPISODE_URL)


def test_pages_without_a_file_are_skipped(client, fake_response):
    viewer = {**VIEWER, "pages": [*PAGES, {"page_number": 3, "image_url": None, "iv": IV1, "auth_tag": ""}]}
    drecomi, _ = client(
        {f"{API_URL}/viewer/episodes/CD20013-001-001/session": fake_response(payload=viewer, status_code=201)}
    )
    assert [page.extra["page_number"] for page in drecomi.episode(EPISODE_URL).pages] == [1, 2]


def test_unknown_episode_is_not_an_episode_page(client):
    drecomi, _ = client()
    with pytest.raises(NotAnEpisodePageError, match="no episode CD20013-001-099"):
        drecomi.episode(f"{BASE_URL}/series/CD20013/episodes/CD20013-001-099")


def test_detail_answering_html_is_not_an_episode_page(client, fake_response):
    drecomi, _ = client({f"{API_URL}/episodes/CD20013-001-001": fake_response(text="<html>maintenance</html>")})
    with pytest.raises(NotAnEpisodePageError, match="no episode"):
        drecomi.episode(EPISODE_URL)


def test_episode_rejects_a_series_url(client):
    drecomi, _ = client()
    with pytest.raises(UnsupportedUrlError):
        drecomi.episode(SERIES_URL)


# --- series ----------------------------------------------------------------------------


@pytest.mark.parametrize("url", [SERIES_URL, f"{SERIES_URL}/"])
def test_series_urls_lists_the_episodes_in_order(client, url):
    drecomi, session = client()
    assert drecomi.series_urls(url) == [EPISODE_URL, SECOND_URL, LOCKED_URL]
    assert all(Drecomi.suitable(candidate) for candidate in drecomi.series_urls(url))
    assert session.params_seen[0] == {
        "series_code": "CD20013",
        "page": 1,
        "limit": LIST_LIMIT,
        "sort": "episode_number",
        "order": "asc",
    }


def test_series_urls_walks_every_page_of_the_listing(client, fake_response):
    first = listing(ENTRIES[:2], page=1, per_page=2, total=3)
    second = listing(ENTRIES[2:], page=2, per_page=2, total=3)
    drecomi, session = client(
        {
            f"{API_URL}/episodes?series_code=CD20013&page=1&": fake_response(payload=first),
            f"{API_URL}/episodes?series_code=CD20013&page=2&": fake_response(payload=second),
        }
    )
    assert drecomi.series_urls(SERIES_URL) == [EPISODE_URL, SECOND_URL, LOCKED_URL]
    assert [params["page"] for params in session.params_seen] == [1, 2]


def test_series_urls_drops_a_repeated_entry(client, fake_response):
    twice = listing([ENTRIES[0], ENTRIES[0], ENTRIES[1]])
    drecomi, _ = client({f"{API_URL}/episodes?series_code=CD20013&": fake_response(payload=twice)})
    assert drecomi.series_urls(SERIES_URL) == [EPISODE_URL, SECOND_URL]


def test_series_urls_rejects_an_episode_url(client):
    drecomi, _ = client()
    with pytest.raises(UnsupportedUrlError):
        drecomi.series_urls(EPISODE_URL)


def test_series_urls_raises_on_an_empty_listing(client, fake_response):
    drecomi, _ = client({f"{API_URL}/episodes?series_code=CD20013&": fake_response(payload=listing([]))})
    with pytest.raises(NotAnEpisodePageError, match="lists no episode"):
        drecomi.series_urls(SERIES_URL)


def test_series_urls_raises_on_an_unknown_series(client):
    drecomi, _ = client()
    with pytest.raises(NotAnEpisodePageError, match="no series CD99999"):
        drecomi.series_urls(f"{BASE_URL}/series/CD99999")


# --- images ----------------------------------------------------------------------------


def test_image_fetches_verifies_and_decrypts(client):
    drecomi, session = client()
    episode = drecomi.episode(EPISODE_URL)
    first = drecomi.image(episode.pages[0], episode)
    second = drecomi.image(episode.pages[1], episode)

    assert first.convert("RGB").getpixel((0, 0)) == (1, 2, 3)
    assert second.convert("RGB").getpixel((0, 0)) == (4, 5, 6)
    assert session.calls[-2:] == [episode.pages[0].url, episode.pages[1].url]
    assert session.headers_seen[-1]["Referer"] == EPISODE_URL


def test_image_rejects_a_file_that_fails_its_tag(client, fake_response):
    drecomi, _ = client({"_0001.webp.enc": fake_response(PAGE2_ENC, content_type="application/octet-stream")})
    episode = drecomi.episode(EPISODE_URL)
    with pytest.raises(GetjmangaError, match="auth tag"):
        drecomi.image(episode.pages[0], episode)


# --- login -----------------------------------------------------------------------------


def test_login_posts_the_credentials_and_sends_the_token_afterwards(client, fake_response):
    answer = {"access_token": "abc.def.ghi", "refresh_token": "r", "expires_in": 3600, "user": {"id": 1}}
    drecomi, session = client({f"{API_URL}/auth/login": fake_response(payload=answer)})
    drecomi.login(EPISODE_URL, "someone@example.com", "hunter2")

    assert session.posts[0] == (f"{API_URL}/auth/login", {"email": "someone@example.com", "password": "hunter2"})
    assert session.headers_seen[0]["Content-Type"] == "application/json"

    drecomi.episode(EPISODE_URL)
    assert all(headers["Authorization"] == "Bearer abc.def.ghi" for headers in session.headers_seen[1:])


def test_login_raises_with_the_site_reason(client, fake_response):
    refused = {"error": "Invalid email or password", "code": "INVALID_CREDENTIALS"}
    drecomi, _ = client({f"{API_URL}/auth/login": fake_response(payload=refused, status_code=HTTPStatus.UNAUTHORIZED)})
    with pytest.raises(LoginError, match="Invalid email or password"):
        drecomi.login(EPISODE_URL, "someone@example.com", "wrong")


def test_login_raises_without_a_token(client, fake_response):
    drecomi, _ = client({f"{API_URL}/auth/login": fake_response(payload={"user": {"id": 1}})})
    with pytest.raises(LoginError, match="HTTP 200"):
        drecomi.login(EPISODE_URL, "someone@example.com", "hunter2")


# --- the real site ---------------------------------------------------------------------

# One free episode per known host: the first episode of an ongoing series.
TEST_URLS: dict[str, str] = {
    "drecomi-plus.jp": "https://drecomi-plus.jp/series/CD20013/episodes/CD20013-001-001",
}


@pytest.mark.network
@pytest.mark.geoblocked  # the API answers, cdn.drecomi-plus.jp does not
@pytest.mark.parametrize("host", TEST_URLS)
def test_site_download(tmp_path, host):
    result = Downloader(Drecomi(), tmp_path, only_first=True).download(TEST_URLS[host])
    assert result.status == "saved"
    assert (result.save_dir / "0.jpg").stat().st_size > 0


@pytest.mark.network
def test_site_paid_episode_has_no_pages():
    # Blade & Bastard, episode 4 (chapter 2 part 1) costs 80 coins; without an account the viewer answers 401.
    episode = Drecomi().episode("https://drecomi-plus.jp/series/CD00007/episodes/CD00007-001-004")
    assert not episode.readable
    assert episode.episode_title == "第2話（1）"
    assert episode.next_url == "https://drecomi-plus.jp/series/CD00007/episodes/CD00007-001-005"


@pytest.mark.network
def test_site_series_lists_episodes():
    urls = Drecomi().series_urls("https://drecomi-plus.jp/series/CD00007")
    assert urls[0] == "https://drecomi-plus.jp/series/CD00007/episodes/CD00007-001-001"
    assert len(urls) > LIST_LIMIT // 4
    assert all(Drecomi.suitable(url) for url in urls)
