from __future__ import annotations

from datetime import date
from io import BytesIO

import pytest
from httpx import Client
from PIL import Image

from getjmanga.downloader import Downloader
from getjmanga.errors import LoginError, NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractor import Page
from getjmanga.extractors.cycomi import (
    API_URL,
    BASE_URL,
    LOGIN_URL,
    Cycomi,
    decrypt,
    episode_title,
    episode_url,
)

SERIES_URL = f"{BASE_URL}/title/257"
EPISODE_URL = f"{BASE_URL}/viewer/chapter/22179"
LOCKED_URL = f"{BASE_URL}/viewer/chapter/24442"
CDN = "https://contents.cycomi.com/image_binaries/jpeg/page"
KEY = "0c03cef2f8ca0cd65947db6db52a1224"
SIGNATURE = "Expires=1789698601&Signature=DEKf5I9xbJ0~&Key-Pair-Id=APKAJ3UXJUBPLEOGBMDQ"
END_CARD = f"https://contents.cycomi.com/images/jpeg/page/end_page/high/385_1.jpg?ver=1789524823&{SIGNATURE}"


def chapter(chapter_id, name, sub_name=None, title_id=257):
    return {
        "dataType": 1,
        "id": chapter_id,
        "titleId": title_id,
        "titleName": "BAD ASS BUDDIES",
        "image": "https://assets-web-prd.akamaized.net/images/jpeg/chapter/6ee8b04969a6b7651a7163f3d55044c9.jpg",
        "name": name,
        "subName": sub_name,
        "startAt": 1761015600000,
        "endAt": None,
        "commentCount": 188,
        "isPassCommentPage": True,
        "isUpdated": False,
        "contentType": 1,
        "rentalHour": 72,
        "isLatestChapter": False,
        "author": "すんしろう",
    }


def page(number, key=KEY):
    return {
        "image": f"{CDN}/{key}/high/{number}.jpg?ver=1772590196&{SIGNATURE}",
        "type": "image",
        "kind": "",
        "width": 960,
        "height": 1364,
        "pageNumber": number,
    }


# `/api/chapter/page/list` for a readable chapter: two pages and the end card past `lastPageNumber`.
PAGES_DATA = {
    "viewStyle": "both",
    "prev": None,
    "next": {"chapterId": 22180},
    "endAdPageCount": 1,
    "lastPageNumber": 2,
    "pages": [page(1), page(2), {**page(3), "image": END_CARD}],
}


def pages_answer(**overrides):
    return {"resultCode": 1, "data": {**PAGES_DATA, **overrides}}


PAGES = pages_answer()
# ...and for a chapter that wants a coin, a rental or a wait.
LOCKED_PAGES = pages_answer(prev={"chapterId": 24397}, next=None, endAdPageCount=0, lastPageNumber=17, pages=[])
# `/api/chapter/paginatedList` with `limit=100`, two pages of a cursor walk.
LISTING_FIRST = {
    "resultCode": 1,
    "data": [chapter(22179, "第１話"), chapter(22180, "第２話"), chapter(22179, "第１話")],
    "nextCursor": 22181,
}
LISTING_LAST = {"resultCode": 1, "data": [chapter(22181, "第３話"), chapter(24442, "第４８話")], "nextCursor": None}
ERROR = {"resultCode": 911000}


def jpeg_bytes(color=(1, 2, 3), size=(8, 8)):
    raw = BytesIO()
    Image.new("RGB", size, color).save(raw, "JPEG")
    return raw.getvalue()


@pytest.fixture
def client(fake_session, fake_response):
    """A `Cycomi` over a session answering the chapter API, the listing and an encrypted page.

    `chapters` maps a chapter id to its `page/list` answer; the URL of that
    route does not say which chapter is asked for, so the body is read.
    """

    def build(extra=None, chapters=None):
        routes = {
            "/chapter/page/list": fake_response(payload=PAGES),
            "/chapter/paginatedList": [fake_response(payload=LISTING_FIRST), fake_response(payload=LISTING_LAST)],
            f"{CDN}/{KEY}/": fake_response(decrypt(jpeg_bytes(), KEY), content_type="image/jpeg"),
        }
        details = {
            "22179": fake_response(payload={"resultCode": 1, "data": chapter(22179, "第１話")}),
            "24442": fake_response(payload={"resultCode": 1, "data": chapter(24442, "第４８話")}),
            "999": fake_response(payload=ERROR),
        }
        session = RoutedSession(fake_session({**routes, **(extra or {})}), details, chapters or {})
        return Cycomi(session), session

    return build


class RoutedSession(Client):
    """Answers `chapter/detail` by its `chapterId` parameter and `page/list` by its JSON body."""

    def __init__(self, session, details, pages):
        super().__init__()
        self._session = session
        self._details = details
        self._pages = pages

    def __getattr__(self, name):
        return getattr(self._session, name)

    def get(self, url, **kwargs):
        params = kwargs.get("params") or {}
        if url.endswith("/chapter/detail") and str(params.get("chapterId")) in self._details:
            self._session.calls.append(url)
            self._session.params_seen.append(params)
            self._session.headers_seen.append(kwargs.get("headers") or {})
            return self._details[str(params["chapterId"])]
        return self._session.get(url, **kwargs)

    def post(self, url, data=None, json=None, **kwargs):
        body = json or {}
        if url.endswith("/chapter/page/list") and body.get("chapterId") in self._pages:
            self._session.posts.append((url, body))
            return self._pages[body["chapterId"]]
        return self._session.post(url, data=data, json=json, **kwargs)


# --- URLs ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        EPISODE_URL,
        f"{EPISODE_URL}/",
        SERIES_URL,
        f"{SERIES_URL}/",
    ],
)
def test_suitable_accepts_chapter_and_title_urls(url):
    assert Cycomi.suitable(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://cycomi.com/viewer/chapter/22179",
        "https://cycomi.com/",
        "https://cycomi.com/viewer/single/1073",
        "https://cycomi.com/viewer/single/1073/22179",
        "https://cycomi.com/viewer/item/image/1",
        "https://cycomi.com/book/1073",
        "https://cycomi.com/title/",
        "https://www.cycomi.com/title/257",
        "https://web.cycomi.com/api/chapter/detail?chapterId=22179",
        "https://piccoma.com/web/viewer/8195/1185884",
    ],
)
def test_suitable_rejects_other_urls(url):
    assert not Cycomi.suitable(url)


@pytest.mark.parametrize(("url", "expected"), [(EPISODE_URL, False), (SERIES_URL, True), (f"{SERIES_URL}/", True)])
def test_is_series(url, expected):
    assert Cycomi.is_series(url) is expected


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        (chapter(1, "第１話"), "第１話"),
        (chapter(1, "第１話", "序章①"), "第１話 序章①"),
        (chapter(1, "第１話", ""), "第１話"),
        ({"id": 7}, "7"),
    ],
)
def test_episode_title(entry, expected):
    assert episode_title(entry) == expected


# --- pages -----------------------------------------------------------------------------


def test_decrypt_is_rc4():
    # RC4 test vector: key "Key", plaintext "Plaintext".
    assert decrypt(b"Plaintext", "Key") == bytes.fromhex("bbf316e8d940af0ad3")
    assert decrypt(bytes.fromhex("bbf316e8d940af0ad3"), "Key") == b"Plaintext"


# --- episodes --------------------------------------------------------------------------


def test_episode_reads_the_titles_the_pages_and_the_next_chapter(client):
    cycomi, session = client()
    episode = cycomi.episode(EPISODE_URL)

    assert episode.url == EPISODE_URL
    assert episode.series_title == "BAD ASS BUDDIES"
    assert episode.episode_title == "第１話"
    assert (episode.writer, episode.publisher) == ("すんしろう", "Cygames")
    assert episode.published == date(2025, 10, 21)
    assert [page.url for page in episode.pages] == [page(1)["image"], page(2)["image"]]
    assert [page.extra for page in episode.pages] == [{"key": KEY, "page_number": 1}, {"key": KEY, "page_number": 2}]
    assert episode.pages[0].width == 960
    assert episode.pages[0].height == 1364
    assert episode.next_url == f"{BASE_URL}/viewer/chapter/22180"
    assert episode.metadata["chapter"]["id"] == 22179
    assert episode.metadata["pages"]["lastPageNumber"] == 2

    assert session.calls == [f"{API_URL}/chapter/detail"]
    assert session.params_seen == [{"chapterId": "22179"}]
    assert session.headers_seen[0]["Accept"] == "application/json"
    assert session.headers_seen[0]["Referer"] == EPISODE_URL
    assert session.posts == [(f"{API_URL}/chapter/page/list", {"titleId": 257, "chapterId": 22179})]


def test_episode_accepts_a_trailing_slash(client):
    cycomi, _ = client()
    assert cycomi.episode(f"{EPISODE_URL}/").url == EPISODE_URL


def test_episode_keeps_every_page_when_the_site_names_no_last_page(client, fake_response):
    cycomi, _ = client({"/chapter/page/list": fake_response(payload=pages_answer(lastPageNumber=0, endAdPageCount=0))})
    episode = cycomi.episode(EPISODE_URL)
    assert len(episode.pages) == 3
    assert episode.pages[-1].extra == {"key": None, "page_number": 3}


def test_episode_skips_entries_that_are_not_images(client, fake_response):
    pages = pages_answer(pages=[page(1), {**page(2), "type": "ad"}, {**page(2), "image": ""}])
    cycomi, _ = client({"/chapter/page/list": fake_response(payload=pages)})
    assert [page.extra["page_number"] for page in cycomi.episode(EPISODE_URL).pages] == [1]


def test_locked_episode_has_no_pages_but_keeps_its_titles(client, fake_response):
    cycomi, _ = client(chapters={24442: fake_response(payload=LOCKED_PAGES)})
    episode = cycomi.episode(LOCKED_URL)

    assert not episode.readable
    assert episode.series_title == "BAD ASS BUDDIES"
    assert episode.episode_title == "第４８話"
    assert (episode.prev_url, episode.next_url) == (f"{BASE_URL}/viewer/chapter/24397", None)
    assert episode.metadata["pages"]["prev"] == {"chapterId": 24397}


def test_locked_episode_still_names_the_next_one(client, fake_response):
    locked = pages_answer(next={"chapterId": 24500}, pages=[])
    cycomi, _ = client(chapters={24442: fake_response(payload=locked)})
    episode = cycomi.episode(LOCKED_URL)
    assert not episode.readable
    assert episode.next_url == f"{BASE_URL}/viewer/chapter/24500"


def test_unknown_chapter_is_not_an_episode_page(client):
    cycomi, _ = client()
    with pytest.raises(NotAnEpisodePageError, match="no chapter 999"):
        cycomi.episode(f"{BASE_URL}/viewer/chapter/999")


def test_chapter_without_pages_answer_is_not_an_episode_page(client, fake_response):
    cycomi, _ = client({"/chapter/page/list": fake_response(payload={"resultCode": 909000})})
    with pytest.raises(NotAnEpisodePageError, match="no pages for chapter 22179"):
        cycomi.episode(EPISODE_URL)


def test_episode_rejects_a_series_url(client):
    cycomi, _ = client()
    with pytest.raises(UnsupportedUrlError):
        cycomi.episode(SERIES_URL)


# --- series ----------------------------------------------------------------------------


@pytest.mark.parametrize("url", [SERIES_URL, f"{SERIES_URL}/"])
def test_series_urls_walks_the_cursor_in_order_without_repeats(client, url):
    cycomi, session = client()
    urls = cycomi.series_urls(url)

    assert urls == [episode_url(22179), episode_url(22180), episode_url(22181), episode_url(24442)]
    assert all(Cycomi.suitable(candidate) for candidate in urls)
    assert session.params_seen == [
        {"titleId": "257", "sort": 1, "limit": 100},
        {"titleId": "257", "sort": 1, "limit": 100, "cursor": 22181},
    ]
    assert session.headers_seen[0]["Referer"] == url


def test_series_urls_rejects_an_episode_url(client):
    cycomi, _ = client()
    with pytest.raises(UnsupportedUrlError):
        cycomi.series_urls(EPISODE_URL)


def test_series_urls_raises_on_an_unknown_title(client, fake_response):
    cycomi, _ = client({"/chapter/paginatedList": fake_response(payload=ERROR)})
    with pytest.raises(NotAnEpisodePageError, match="no series 257"):
        cycomi.series_urls(SERIES_URL)


def test_series_urls_raises_on_an_empty_listing(client, fake_response):
    cycomi, _ = client(
        {"/chapter/paginatedList": fake_response(payload={"resultCode": 1, "data": [], "nextCursor": None})}
    )
    with pytest.raises(NotAnEpisodePageError, match="lists no episode"):
        cycomi.series_urls(SERIES_URL)


# --- images ----------------------------------------------------------------------------


def test_image_is_fetched_with_the_chapter_as_referer_and_decrypted(client):
    cycomi, session = client()
    episode = cycomi.episode(EPISODE_URL)
    image = cycomi.image(episode.pages[0], episode)

    assert image.size == (8, 8)
    assert session.calls[-1] == episode.pages[0].url
    assert session.headers_seen[-1]["Referer"] == EPISODE_URL


def test_image_without_a_key_is_used_as_served(client, fake_response):
    cycomi, _ = client({END_CARD: fake_response(jpeg_bytes(size=(4, 4)), content_type="image/jpeg")})
    episode = cycomi.episode(EPISODE_URL)
    assert cycomi.image(Page(url=END_CARD, extra={"key": None}), episode).size == (4, 4)


# --- logging in ------------------------------------------------------------------------


def test_login_posts_the_credentials_as_json(fake_session, fake_response):
    session = fake_session({"/api/auth/login/email": fake_response(payload={"resultCode": 1, "data": {}})})
    Cycomi(session).login(SERIES_URL, "someone@example.com", "hunter2")

    assert session.posts == [(LOGIN_URL, {"email": "someone@example.com", "password": "hunter2", "keepFlag": True})]


def test_login_raises_when_the_site_refuses(fake_session, fake_response):
    session = fake_session({"/api/auth/login/email": fake_response(payload={"resultCode": 903000})})
    with pytest.raises(LoginError, match=r"refused the credentials for 'someone@example.com' \(resultCode 903000\)"):
        Cycomi(session).login(SERIES_URL, "someone@example.com", "wrong")


def test_login_raises_on_a_nonsense_answer(fake_session, fake_response):
    session = fake_session({"/api/auth/login/email": fake_response(text="<html>maintenance</html>", payload=None)})
    with pytest.raises(LoginError, match="resultCode None"):
        Cycomi(session).login(SERIES_URL, "someone@example.com", "hunter2")


# --- the real site --------------------------------------------------------------------

# One free chapter per known host: the first chapter of a long-running series.
TEST_URLS: dict[str, str] = {
    "cycomi.com": "https://cycomi.com/viewer/chapter/22179",
}


@pytest.mark.network
@pytest.mark.parametrize("host", TEST_URLS)
def test_site_download(tmp_path, host):
    result = Downloader(Cycomi(), tmp_path, only_first=True).download(TEST_URLS[host])
    assert result.status == "saved"
    assert (result.save_dir / "0.jpg").stat().st_size > 0


@pytest.mark.network
def test_site_locked_chapter_has_no_pages():
    # The newest chapter of a running series wants a coin or a rental.
    urls = Cycomi().series_urls(SERIES_URL)
    episode = Cycomi().episode(urls[-1])
    assert not episode.readable
    assert episode.series_title == "BAD ASS BUDDIES"


@pytest.mark.network
def test_site_title_lists_chapters():
    urls = Cycomi().series_urls(SERIES_URL)
    assert urls[0] == EPISODE_URL
    assert all(Cycomi.suitable(url) for url in urls)
