from __future__ import annotations

from http import HTTPStatus
from io import BytesIO

import pytest
from PIL import Image

from getjmanga.downloader import Downloader
from getjmanga.extractors.comicwalker import unmask
from getjmanga.extractors.common import LoginError, NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractors.lezhin import (
    API_URL,
    BASE_URL,
    LOGIN_URL,
    XOR_KEY,
    Lezhin,
    episode_url,
    looks_like_image,
)

TITLE = "taming_zennenrei"
FIRST, SECOND, LOCKED = "01kwe89f3rer2593x517bc6q2a", "01kwe89f4jkxnbppetbkk4j1jf", "01kwe89f5vr5aezy0xyh5z7r52"
WORK_URL = f"{BASE_URL}/comic/{TITLE}"
EPISODE_URL = episode_url(TITLE, FIRST)
SECOND_URL = episode_url(TITLE, SECOND)
LOCKED_URL = episode_url(TITLE, LOCKED)
CDN = "https://private-image.lezhin.jp/chapter/content"
#: A second key, the one a "rotated" bundle carries in the discovery test.
OTHER_KEY = "0f1e2d3c4b5a69788796a5b4c3d2e1f0"


def ok(results):
    return {"status": "success", "message": "OK", "results": results}


def error(status, message):
    return {"status": int(status), "message": message, "errors": {"message": message}}


def item(hash_id, name, order, viewable="free"):
    return {
        "hash_id": hash_id,
        "name": name,
        "name_kana": None,
        "display_order": order,
        "viewable_type": viewable,
        "point_consumption": 0 if viewable == "free" else 56,
        "is_trial": False,
        "is_bought": False,
    }


def general_info(name, previous=None, following=None):
    return ok(
        {
            "previous_item": previous or {"hash_id": None, "name": None},
            "next_item": following or {"hash_id": None, "name": None},
            "is_vertical_reading": True,
            "item": {
                "gtm_id": 7021753691738760,
                "title": {"gtm_id": 214009, "name": "テイミング【改訂版】"},
                "title_id": 214009,
                "name": name,
                "content_type": "I",
            },
        }
    )


def image_path(chapter, page, order):
    return {
        "image_path": f"{CDN}/{chapter}/{page}.webp?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Signature=sig{order}",
        "display_order": order,
        "width": 1080,
        "height": 4218,
    }


# The pages arrive out of order on purpose.
VIEWER = ok({"read_id": 0, "image_paths": [image_path(FIRST, "p2", 2), image_path(FIRST, "p1", 1)]})


def chapter(hash_id, name, order, viewable="free"):
    return {**item(hash_id, name, order, viewable), "is_content": True, "free_chapter": "1,2,3"}


def all_chapters(chapters, *, page, last_page):
    return ok(
        {
            "title": {"gtm_id": 214009, "name": "テイミング【改訂版】"},
            "data": [{"volume_hash_id": None, "volume_name": None, "chapters": chapters}],
            "pagination": {"current_page": page, "last_page": last_page, "per_page": 100, "total": 3},
        }
    )


def masked_png(color, key=XOR_KEY):
    raw = BytesIO()
    Image.new("RGB", (8, 8), color).save(raw, "PNG")
    return unmask(raw.getvalue(), key)


@pytest.fixture
def client(fake_session, fake_response):
    def make(extra=None):
        routes = {
            f"{API_URL}/comic/{TITLE}/chapter/{FIRST}/general-info": fake_response(
                payload=general_info("第 1 話", following=item(SECOND, "第 2 話", 2)),
                content_type="application/json",
            ),
            f"{API_URL}/comic/{TITLE}/chapter/{FIRST}/viewer": fake_response(
                payload=VIEWER, content_type="application/json"
            ),
            f"{API_URL}/comic/{TITLE}/chapter/{LOCKED}/general-info": fake_response(
                payload=general_info(
                    "第 4 話", following=item("01kwe89f6dhvx56wdvxxahwdp6", "第 5 話", 5, "bonus_point")
                ),
                content_type="application/json",
            ),
            f"{API_URL}/comic/{TITLE}/chapter/{LOCKED}/viewer": fake_response(
                payload=error(HTTPStatus.BAD_REQUEST, "not_purchased"),
                status_code=HTTPStatus.BAD_REQUEST,
                content_type="application/json",
            ),
            f"{API_URL}/comic/{TITLE}/chapter/nonexistent/": fake_response(
                payload=error(HTTPStatus.NOT_FOUND, "入力したハッシュIDは存在しません。"),
                status_code=HTTPStatus.NOT_FOUND,
                content_type="application/json",
            ),
            f"{API_URL}/comic/adult_title/": fake_response(
                payload=error(HTTPStatus.BAD_REQUEST, "title_is_safe_mode"),
                status_code=HTTPStatus.BAD_REQUEST,
                content_type="application/json",
            ),
            f"{API_URL}/comic/{TITLE}/all-chapters": [
                fake_response(
                    payload=all_chapters(
                        [chapter(FIRST, "第 1 話", 1), chapter(SECOND, "第 2 話", 2), chapter(FIRST, "第 1 話", 1)],
                        page=1,
                        last_page=2,
                    ),
                    content_type="application/json",
                ),
                fake_response(
                    payload=all_chapters([chapter(LOCKED, "第 4 話", 4, "bonus_point")], page=2, last_page=2),
                    content_type="application/json",
                ),
            ],
            f"{API_URL}/comic/nonexist/all-chapters": fake_response(
                payload=error(HTTPStatus.NOT_FOUND, "入力したタイトルIDは存在しません。"),
                status_code=HTTPStatus.NOT_FOUND,
                content_type="application/json",
            ),
            f"{API_URL}/comic/empty/all-chapters": fake_response(
                payload=all_chapters([], page=1, last_page=1), content_type="application/json"
            ),
            f"{CDN}/{FIRST}/p1.webp": fake_response(masked_png((10, 20, 30)), content_type="image/webp"),
            f"{CDN}/{FIRST}/p2.webp": fake_response(masked_png((40, 50, 60)), content_type="image/webp"),
        }
        # `extra` comes first and overrides a default route of the same key.
        overrides = extra or {}
        session = fake_session({**overrides, **{key: value for key, value in routes.items() if key not in overrides}})
        return Lezhin(session), session

    return make


# --- URLs -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        EPISODE_URL,
        f"{BASE_URL}/comic/{TITLE}/chapter/{FIRST}",
        f"{BASE_URL}/comic/{TITLE}/chapter/{FIRST}/",
        WORK_URL,
        f"{WORK_URL}/",
        f"{WORK_URL}?tab=volume",
    ],
)
def test_suitable_accepts_chapter_and_work_urls(url):
    assert Lezhin.suitable(url)


@pytest.mark.parametrize(
    "url",
    [
        f"http://lezhin.jp/comic/{TITLE}",
        f"https://www.lezhin.jp/comic/{TITLE}",
        f"https://www.lezhin.com/ja/comic/{TITLE}",
        "https://www.beltoon.jp/detail/12s1",
        f"{BASE_URL}/",
        f"{BASE_URL}/login",
        f"{BASE_URL}/comic/{TITLE}/volume/{FIRST}/viewer",
        f"{BASE_URL}/comic/{TITLE}/chapter/{FIRST}/general-info",
    ],
)
def test_suitable_rejects_other_pages(url):
    assert not Lezhin.suitable(url)


def test_is_series_tells_a_work_from_a_chapter():
    assert Lezhin.is_series(WORK_URL)
    assert Lezhin.is_series(f"{WORK_URL}?tab=volume")
    assert not Lezhin.is_series(EPISODE_URL)


# --- episodes ---------------------------------------------------------------------------


def test_episode_reads_titles_pages_and_the_next_chapter(client):
    lezhin, session = client()
    episode = lezhin.episode(f"{BASE_URL}/comic/{TITLE}/chapter/{FIRST}")

    assert episode.url == EPISODE_URL
    assert episode.series_title == "テイミング【改訂版】"
    assert episode.episode_title == "第 1 話"
    assert [page.url.split("?")[0] for page in episode.pages] == [f"{CDN}/{FIRST}/p1.webp", f"{CDN}/{FIRST}/p2.webp"]
    assert episode.pages[0].extra == {"key": XOR_KEY}
    assert (episode.pages[0].width, episode.pages[0].height) == (1080, 4218)
    assert episode.next_url == SECOND_URL
    assert episode.metadata["error"] is None
    assert session.calls == [
        f"{API_URL}/comic/{TITLE}/chapter/{FIRST}/general-info",
        f"{API_URL}/comic/{TITLE}/chapter/{FIRST}/viewer",
    ]
    assert session.headers_seen[-1]["Referer"] == EPISODE_URL
    assert session.headers_seen[-1]["Accept"] == "application/json"
    assert "Authorization" not in session.headers_seen[-1]


def test_episode_is_locked_when_the_viewer_wants_a_purchase(client):
    lezhin, _ = client()
    episode = lezhin.episode(LOCKED_URL)

    assert episode.pages == ()
    assert not episode.readable
    assert episode.episode_title == "第 4 話"
    assert episode.next_url == episode_url(TITLE, "01kwe89f6dhvx56wdvxxahwdp6")
    assert episode.metadata["error"] == "not_purchased"


def test_episode_is_locked_when_the_work_needs_adult_mode(client):
    lezhin, _ = client()
    episode = lezhin.episode(f"{BASE_URL}/comic/adult_title/chapter/{FIRST}/viewer")

    assert episode.pages == ()
    assert (episode.series_title, episode.episode_title) == ("adult_title", FIRST)
    assert episode.next_url is None
    assert episode.metadata["error"] == "title_is_safe_mode"


def test_episode_raises_for_an_unknown_chapter(client):
    lezhin, _ = client()
    with pytest.raises(NotAnEpisodePageError, match="nonexistent"):
        lezhin.episode(f"{BASE_URL}/comic/{TITLE}/chapter/nonexistent/viewer")


def test_episode_rejects_a_work_url(client):
    lezhin, _ = client()
    with pytest.raises(UnsupportedUrlError, match="not a chapter page"):
        lezhin.episode(WORK_URL)


def test_episode_raises_on_a_server_error(client, fake_response):
    lezhin, _ = client({f"{API_URL}/comic/broken/": fake_response(status_code=HTTPStatus.BAD_GATEWAY)})
    with pytest.raises(Exception, match="502"):
        lezhin.episode(f"{BASE_URL}/comic/broken/chapter/{FIRST}/viewer")


# --- series -----------------------------------------------------------------------------


def test_series_urls_walks_every_page_in_order_without_duplicates(client):
    lezhin, session = client()
    assert lezhin.series_urls(WORK_URL) == [EPISODE_URL, SECOND_URL, LOCKED_URL]
    assert session.params_seen == [{"page": 1, "page_size": 100}, {"page": 2, "page_size": 100}]
    assert all(Lezhin.suitable(url) for url in lezhin.series_urls(WORK_URL))


def test_series_urls_raises_for_an_unknown_work(client):
    lezhin, _ = client()
    with pytest.raises(NotAnEpisodePageError, match="nonexist"):
        lezhin.series_urls(f"{BASE_URL}/comic/nonexist")


def test_series_urls_raises_for_an_empty_listing(client):
    lezhin, _ = client()
    with pytest.raises(NotAnEpisodePageError, match="lists no chapter"):
        lezhin.series_urls(f"{BASE_URL}/comic/empty")


def test_series_urls_rejects_a_chapter_url(client):
    lezhin, _ = client()
    with pytest.raises(UnsupportedUrlError, match="not a work page"):
        lezhin.series_urls(EPISODE_URL)


# --- unmasking --------------------------------------------------------------------------


def test_looks_like_image_knows_the_served_formats():
    assert looks_like_image(b"RIFF\x00\x00\x00\x00WEBPVP8 ")
    assert looks_like_image(b"\xff\xd8\xff\xe0")
    assert looks_like_image(b"\x89PNG\r\n")
    assert not looks_like_image(b"\x05\xa1\x3a\xcc")
    assert not looks_like_image(b"")


def test_image_unmasks_a_page_with_the_key_of_the_page(client):
    lezhin, session = client()
    episode = lezhin.episode(EPISODE_URL)
    image = lezhin.image(episode.pages[0], episode)

    assert image.getpixel((0, 0)) == (10, 20, 30)
    assert session.calls[-1].startswith(f"{CDN}/{FIRST}/p1.webp?")
    assert session.headers_seen[-1]["Referer"] == EPISODE_URL


def test_image_reads_a_rotated_key_out_of_the_viewer_bundle(client, fake_response):
    rotated = {
        f"{CDN}/{FIRST}/p1.webp": fake_response(masked_png((70, 80, 90), OTHER_KEY), content_type="image/webp"),
        EPISODE_URL: fake_response(
            text='<html><script src="/_next/static/chunks/main-1.js" async=""></script>'
            '<script src="/_next/static/chunks/5360-2.js" async=""></script></html>'
        ),
        "/_next/static/chunks/main-1.js": fake_response(text="(self.webpackChunk=[])"),
        "/_next/static/chunks/5360-2.js": fake_response(
            text=f'src:e.image_path,crypto:{{method:"xor",key:"{OTHER_KEY.upper()}"}},key:""'
        ),
    }
    lezhin, session = client(rotated)
    episode = lezhin.episode(EPISODE_URL)
    assert episode.pages[0].extra == {"key": XOR_KEY}

    assert lezhin.image(episode.pages[0], episode).getpixel((0, 0)) == (70, 80, 90)
    assert session.calls[-2:] == [
        f"{BASE_URL}/_next/static/chunks/main-1.js",
        f"{BASE_URL}/_next/static/chunks/5360-2.js",
    ]
    # The key found once serves the chapters that follow.
    assert lezhin.episode(EPISODE_URL).pages[0].extra == {"key": OTHER_KEY}


def test_image_raises_when_no_bundle_carries_a_key(client, fake_response):
    lezhin, _ = client(
        {
            f"{CDN}/{FIRST}/p1.webp": fake_response(masked_png((1, 2, 3), OTHER_KEY), content_type="image/webp"),
            EPISODE_URL: fake_response(text='<script src="/_next/static/chunks/main-1.js"></script>'),
            "/_next/static/chunks/main-1.js": fake_response(text="nothing here"),
        }
    )
    episode = lezhin.episode(EPISODE_URL)
    with pytest.raises(NotAnEpisodePageError, match="no XOR key"):
        lezhin.image(episode.pages[0], episode)


# --- downloading ------------------------------------------------------------------------


# --- logging in -------------------------------------------------------------------------


def test_login_posts_the_credentials_and_sends_the_token_afterwards(client, fake_response):
    lezhin, session = client(
        {
            LOGIN_URL: fake_response(
                payload=ok({"access_token": "eyJ.token", "refresh_token": "r", "expires_in": 3600}),
                content_type="application/json",
            )
        }
    )
    lezhin.login(WORK_URL, "someone@example.com", "hunter2")

    assert session.posts == [(LOGIN_URL, {"email": "someone@example.com", "password": "hunter2"})]
    lezhin.episode(EPISODE_URL)
    assert session.headers_seen[-1]["Authorization"] == "Bearer eyJ.token"


def test_login_raises_with_the_site_reason(client, fake_response):
    lezhin, _ = client(
        {
            LOGIN_URL: fake_response(
                payload=error(HTTPStatus.BAD_REQUEST, "USER_ID_OR_PASSWORD_MISMATCH"),
                status_code=HTTPStatus.BAD_REQUEST,
                content_type="application/json",
            )
        }
    )
    with pytest.raises(LoginError, match="USER_ID_OR_PASSWORD_MISMATCH"):
        lezhin.login(WORK_URL, "someone@example.com", "wrong")


def test_login_raises_when_no_token_comes_back(client, fake_response):
    lezhin, _ = client({LOGIN_URL: fake_response(text="<html>maintenance</html>")})
    with pytest.raises(LoginError, match="HTTP 200"):
        lezhin.login(WORK_URL, "someone@example.com", "hunter2")


# --- the real site ----------------------------------------------------------------------

# One chapter per known host, free to read without an account.
TEST_URLS: dict[str, str] = {
    "lezhin.jp": "https://lezhin.jp/comic/taming_zennenrei/chapter/01kwe89f3rer2593x517bc6q2a/viewer",
}


@pytest.mark.network
@pytest.mark.parametrize("host", TEST_URLS)
def test_site_download(tmp_path, host):
    result = Downloader(Lezhin(), tmp_path, only_first=True).download(TEST_URLS[host])
    assert result.status == "saved"
    assert (result.save_dir / "0.jpg").stat().st_size > 0


@pytest.mark.network
def test_work_page_lists_chapters():
    urls = Lezhin().series_urls("https://lezhin.jp/comic/taming_zennenrei")
    assert urls[0] == TEST_URLS["lezhin.jp"]
    assert all(Lezhin.suitable(url) for url in urls)


@pytest.mark.network
def test_paid_chapter_is_locked():
    episode = Lezhin().episode("https://lezhin.jp/comic/taming_zennenrei/chapter/01kwe89f5vr5aezy0xyh5z7r52/viewer")
    assert episode.pages == ()
    assert episode.next_url is not None
