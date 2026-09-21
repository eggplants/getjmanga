from __future__ import annotations

from http import HTTPStatus
from io import BytesIO

import pytest
from httpx import HTTPStatusError
from PIL import Image

from getjmanga.downloader import Downloader
from getjmanga.errors import NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractors.torch import Torch

EPISODE_URL = "https://to-ti.in/story/fv_01"
NEXT_URL = "https://to-ti.in/story/fv_02"
WORK_URL = "https://to-ti.in/product/favorites"
IMAGE_BASE = "https://to-ti.in/wp-content/uploads/img/story/item146"

# A comic episode as the theme renders it: a blank leading page, the pages as
# lazy-loaded `span[img-url]`s, info pages after them, and the footer that
# names the work, the episode and the next one.
# The share button's tweet, URL-encoded: `『フェイバリッツ FAVORITES／mememe』-#01- @_to_ti`.
SHARE_TEXT = (
    "%E3%80%8E%E3%83%95%E3%82%A7%E3%82%A4%E3%83%90%E3%83%AA%E3%83%83%E3%83%84+FAVORITES"
    "%EF%BC%8Fmememe%E3%80%8F-%2301-+%40_to_ti"
)
EPISODE_HTML = f"""
<html><head><title>トーチweb フェイバリッツ FAVORITES 【#01】</title></head><body>
<div id="wrapper">
<div id="viewer" class="manga bind_right start_left">
<header class="viewer_ui"><h1><a href="https://to-ti.in/">トーチ</a></h1>
<ul class="share"><li><a href="http://twitter.com/share?url=https%3A%2F%2Fto-ti.in%2Fstory%2Ffv_01&text={SHARE_TEXT}"
 target="_blank">tw</a></li></ul>
</header>
<section id="viewer_container"><div id="viewer_main"><div class="scroll_bar"><div class="scroll">
<div class="page page_content">
<div class="manga_page blank"></div>
<div class="manga_page not_blank"><p class="manga_page_con">
<span class="manga_page_image" img-url="{IMAGE_BASE}/fv01_001.jpg"></span></p></div>
<div class="manga_page not_blank"><p class="manga_page_con">
<span class="manga_page_image" img-url="{IMAGE_BASE}/fv01_002.jpg"></span></p></div>
<div class="manga_page not_blank"><p class="manga_page_con">
<span class="manga_page_image" img-url="{IMAGE_BASE}/fv01_002.jpg"></span></p></div>
<div class="manga_page not_blank"><p class="manga_page_con">
<span class="manga_page_image" img-url="/wp-content/uploads/img/story/item146/fv01_003.jpg"></span></p></div>
<div class="manga_page not_blank info_page"><div class="content"><h4>STORE</h4>
<ul><li><a href="https://toti.official.ec/items/1" target="_blank">
<img src="https://to-ti.in/wp-content/uploads/2021/01/goods-200x200.jpg" /></a></li></ul>
</div></div>
<div class="manga_page not_blank info_page related_product"><div class="content"><h4>その他の作品</h4>
<ul><li><a href="https://to-ti.in/product/kanazawa"><p class="title">金沢職人ばなし</p></a></li></ul>
</div></div>
</div>
</div></div></div></section>
<div id="viewer_control" class="viewer_ui">
<a class="next"><span>next</span></a><a class="prev"><span>prev</span></a></div>
<footer class="viewer_ui"><div class="bg"><div class="content">
<div id="viewer_pagenation" class="viewer_ui"><span></span></div>
<a class="next" href="{NEXT_URL}">次のエピソード</a>
<h2><a href="{WORK_URL}">
フェイバリッツ FAVORITES                            <span class="name">#01</span>
</a></h2>
</div></div></footer>
</div>
<div id="viewer_loading"></div>
</div>
</body></html>
"""

# The last episode of a work: no `a.next` in the footer.
LAST_EPISODE_HTML = f"""
<html><head><title>トーチweb フェイバリッツ FAVORITES 【#13】</title></head><body>
<div id="viewer" class="manga bind_right start_left">
<div class="manga_page not_blank"><p class="manga_page_con">
<span class="manga_page_image" img-url="{IMAGE_BASE}/fv13_001.jpg"></span></p></div>
<footer class="viewer_ui">
<a class="prev" href="https://to-ti.in/story/fv_12">前のエピソード</a>
<h2><a href="{WORK_URL}">フェイバリッツ FAVORITES<span class="name">#13</span></a></h2></footer>
</div>
</body></html>
"""

# A text post (an interview, a news item) in the same viewer: no page image at all.
TEXT_HTML = """
<html><head><title>トーチweb 自転車屋さんの高橋くん 【ドラマ『自転車屋さんの高橋くん』メイキング】</title></head><body>
<div id="viewer" class="text bind_right start_left">
<section id="viewer_container"><div class="page page_content">
<p class="post_title no_author">ドラマ『自転車屋さんの高橋くん』メイキング</p>
<p><img src="https://to-ti.in/wp-content/uploads/2022/11/ota_1-300x185.png" alt="" width="300" height="185" /></p>
</div></section>
<footer class="viewer_ui"><div class="content">
<a class="next" href="https://to-ti.in/story/takahashi150_ranking">次のエピソード</a>
<h2><a href="https://to-ti.in/product/takahashikun">自転車屋さんの高橋くん
<span class="name">ドラマ『自転車屋さんの高橋くん』メイキング</span></a></h2>
</div></footer>
</div>
</body></html>
"""

# A viewer whose footer lost its heading: the titles come from `<title>`.
BARE_HTML = f"""
<html><head><title>トーチweb 言葉の獣 【第1話】</title></head><body>
<div id="viewer" class="manga bind_right start_left">
<span class="manga_page_image" img-url="{IMAGE_BASE}/kk01_001.jpg"></span>
<footer class="viewer_ui"></footer>
</div>
</body></html>
"""

# A work page: the cover links the latest episode, the pager the first and
# the latest, the episode block every published one oldest first, the "other
# works" block other works, the news block a store.
WORK_HTML = """
<html><head><title>トーチweb フェイバリッツ FAVORITES</title></head><body>
<div id="wrapper"><section>
<div class="work_detail manga">
<header>
<p class="cover"><a href="https://to-ti.in/story/fv02_ad">
<img src="https://to-ti.in/wp-content/uploads/2024/09/cover.webp" alt="フェイバリッツ FAVORITES" /></a></p>
<time>'26/09/02 UPDATE</time>
<h3>「フェイバリッツ FAVORITES」</h3>
<p>お互いがお気に入り？</p>
</header>
<div class="page_pager">
<p class="prev"><a href="https://to-ti.in/story/fv_01">第1話を読む<span>#01</span></a></p>
<p class="next"><a href="https://to-ti.in/story/fv02_ad">最新話を読む<span>2巻/特典情報</span></a></p>
</div>
<div class="episode">
<h4>公開中のエピソード</h4>
<ul>
<li><a href="https://to-ti.in/story/fv_01"><span>#01</span></a></li>
<li><a href="https://to-ti.in/story/fv_02"><span>#02</span></a></li>
<li><a href="https://to-ti.in/story/fv01_ad"><span>1巻/特典情報</span></a></li>
<li><a href="https://to-ti.in/story/fv_02"><span>#02 (again)</span></a></li>
<li><a href="https://to-ti.in/story/fv_13"><span>#13</span></a></li>
<li><a href="https://to-ti.in/story/fv02_ad"><span>2巻/特典情報</span></a></li>
</ul>
</div>
<aside class="news"><article><div class="detail">
<p><a href="https://amzn.asia/d/x" target="_blank">ご予約</a></p></div></article></aside>
<div class="other_items_list_circle"><h4>その他の作品</h4>
<ul><li><a href="https://to-ti.in/product/matsuiriku"><p class="title">ミーコ</p></a></li></ul>
</div>
</div>
</section></div>
</body></html>
"""

# A work page without the episode block: only the pager names episodes.
PAGER_ONLY_WORK_HTML = """
<html><body><div class="work_detail manga">
<header><h3>「ミーコ〈松井陸 読切シリーズ〉」</h3></header>
<div class="page_pager">
<p class="prev"><a href="https://to-ti.in/story/rikonsui">第1話を読む<span>離婚水</span></a></p>
<p class="next"><a href="https://to-ti.in/story/mi-ko">最新話を読む<span>ミーコ</span></a></p>
</div>
</div></body></html>
"""

EMPTY_WORK_HTML = """
<html><body><div class="work_detail manga"><header><h3>「まだ何も」</h3></header></div></body></html>
"""

NOT_A_PAGE_HTML = "<html><head><title>トーチweb</title></head><body><p>404 not found.</p></body></html>"


def jpeg(colour=(200, 40, 40), size=(40, 60)) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", size, colour).save(buffer, format="JPEG")
    return buffer.getvalue()


# --- URLs ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        EPISODE_URL,
        "https://to-ti.in/story/fv_01/",
        "https://to-ti.in/story/%e3%81%9d%e3%81%ae4%e3%80%80%e3%83%9e%e3%83%ab",
        "https://to-ti.in/story/takahashi-news251003?utm=x",
        WORK_URL,
        "https://to-ti.in/product/mantra-arya/",
    ],
)
def test_suitable_accepts_episode_and_work_urls(url):
    assert Torch.suitable(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://to-ti.in/story/fv_01",
        "https://www.to-ti.in/story/fv_01",
        "https://leedcafe.com/story/fv_01",
        "https://to-ti.in/",
        "https://to-ti.in/product",
        "https://to-ti.in/product/",
        "https://to-ti.in/items",
        "https://to-ti.in/blog/2024/09/01/hello",
        "https://to-ti.in/story/",
        "https://to-ti.in/story/fv_01/extra",
        "https://to-ti.in/wp-content/uploads/img/story/item146/fv01_001.jpg",
    ],
)
def test_suitable_rejects_other_urls(url):
    assert not Torch.suitable(url)


def test_is_series_tells_a_work_page_from_an_episode():
    assert Torch.is_series(WORK_URL)
    assert not Torch.is_series(EPISODE_URL)


# --- episodes --------------------------------------------------------------------------


def test_episode_reads_the_titles_the_pages_and_the_next(fake_session, fake_response):
    session = fake_session({"/story/fv_01": fake_response(text=EPISODE_HTML)})
    episode = Torch(session).episode(EPISODE_URL)

    assert episode.series_title == "フェイバリッツ FAVORITES"
    assert (episode.writer, episode.publisher) == ("mememe", "リイド社")
    assert episode.episode_title == "#01"
    # In DOM order, deduplicated, relative paths resolved; the store and the
    # "other works" thumbnails on the info pages are not pages.
    assert [page.url for page in episode.pages] == [
        f"{IMAGE_BASE}/fv01_001.jpg",
        f"{IMAGE_BASE}/fv01_002.jpg",
        f"{IMAGE_BASE}/fv01_003.jpg",
    ]
    assert all(page.extra == {} for page in episode.pages)
    assert episode.next_url == NEXT_URL
    assert episode.metadata["kind"] == "manga"
    assert episode.metadata["series_url"] == WORK_URL
    assert episode.metadata["slug"] == "fv_01"
    assert session.calls == [EPISODE_URL]
    assert session.headers_seen[0]["User-Agent"].startswith("Mozilla/5.0")


def test_episode_at_the_end_of_a_work_has_no_next(fake_session, fake_response):
    session = fake_session({"/story/fv_13": fake_response(text=LAST_EPISODE_HTML)})
    episode = Torch(session).episode("https://to-ti.in/story/fv_13")

    assert episode.episode_title == "#13"
    assert [page.url for page in episode.pages] == [f"{IMAGE_BASE}/fv13_001.jpg"]
    assert (episode.prev_url, episode.next_url) == ("https://to-ti.in/story/fv_12", None)


def test_text_post_has_no_pages_but_still_a_next(fake_session, fake_response):
    session = fake_session({"/story/making": fake_response(text=TEXT_HTML)})
    episode = Torch(session).episode("https://to-ti.in/story/making_takahashi_drama")

    assert not episode.readable
    assert episode.pages == ()
    assert episode.series_title == "自転車屋さんの高橋くん"
    assert episode.episode_title == "ドラマ『自転車屋さんの高橋くん』メイキング"
    assert episode.next_url == "https://to-ti.in/story/takahashi150_ranking"
    assert episode.metadata["kind"] == "text"


def test_titles_fall_back_to_the_page_title(fake_session, fake_response):
    session = fake_session({"/story/kk01": fake_response(text=BARE_HTML)})
    episode = Torch(session).episode("https://to-ti.in/story/kk01")

    assert episode.series_title == "言葉の獣"
    assert episode.episode_title == "第1話"
    assert episode.metadata["series_url"] is None
    assert episode.next_url is None


def test_page_without_a_viewer_is_not_an_episode(fake_session, fake_response):
    session = fake_session({"/story/": fake_response(text=NOT_A_PAGE_HTML)})
    with pytest.raises(NotAnEpisodePageError, match="no viewer"):
        Torch(session).episode("https://to-ti.in/story/whatever")


def test_taken_down_episode_answers_404_and_is_not_an_episode(fake_session, fake_response):
    session = fake_session({"/story/fv_03": fake_response(text=NOT_A_PAGE_HTML, status_code=HTTPStatus.NOT_FOUND)})
    with pytest.raises(NotAnEpisodePageError, match="404"):
        Torch(session).episode("https://to-ti.in/story/fv_03")


def test_other_http_errors_come_through(fake_session, fake_response):
    session = fake_session({"/story/fv_01": fake_response(text="", status_code=HTTPStatus.SERVICE_UNAVAILABLE)})
    with pytest.raises(HTTPStatusError):
        Torch(session).episode(EPISODE_URL)


def test_work_url_is_not_an_episode(fake_session):
    with pytest.raises(NotAnEpisodePageError, match="not an episode page"):
        Torch(fake_session({})).episode(WORK_URL)


# --- series ----------------------------------------------------------------------------


def test_series_urls_lists_the_episode_block_oldest_first_deduplicated(fake_session, fake_response):
    session = fake_session({"/product/favorites": fake_response(text=WORK_HTML)})
    urls = Torch(session).series_urls(WORK_URL)

    assert urls == [
        "https://to-ti.in/story/fv_01",
        "https://to-ti.in/story/fv_02",
        "https://to-ti.in/story/fv01_ad",
        "https://to-ti.in/story/fv_13",
        "https://to-ti.in/story/fv02_ad",
    ]
    assert all(Torch.suitable(url) for url in urls)


def test_series_urls_falls_back_to_the_pager(fake_session, fake_response):
    session = fake_session({"/product/matsuiriku": fake_response(text=PAGER_ONLY_WORK_HTML)})
    assert Torch(session).series_urls("https://to-ti.in/product/matsuiriku") == [
        "https://to-ti.in/story/rikonsui",
        "https://to-ti.in/story/mi-ko",
    ]


def test_series_urls_raises_on_an_empty_listing(fake_session, fake_response):
    session = fake_session({"/product/empty": fake_response(text=EMPTY_WORK_HTML)})
    with pytest.raises(NotAnEpisodePageError, match="lists no episode"):
        Torch(session).series_urls("https://to-ti.in/product/empty")


def test_series_urls_raises_on_a_missing_work(fake_session, fake_response):
    session = fake_session({"/product/gone": fake_response(text=NOT_A_PAGE_HTML, status_code=HTTPStatus.NOT_FOUND)})
    with pytest.raises(NotAnEpisodePageError, match="404"):
        Torch(session).series_urls("https://to-ti.in/product/gone")


def test_series_urls_raises_on_a_page_that_is_no_work(fake_session, fake_response):
    session = fake_session({"/product/odd": fake_response(text=NOT_A_PAGE_HTML)})
    with pytest.raises(NotAnEpisodePageError, match="no work page"):
        Torch(session).series_urls("https://to-ti.in/product/odd")


def test_series_urls_rejects_an_episode_url(fake_session):
    with pytest.raises(UnsupportedUrlError):
        Torch(fake_session({})).series_urls(EPISODE_URL)


# --- download --------------------------------------------------------------------------


def test_download_writes_the_pages(tmp_path, fake_session, fake_response):
    session = fake_session(
        {
            "/story/fv_01": fake_response(text=EPISODE_HTML),
            "fv01_001.jpg": fake_response(jpeg((200, 40, 40)), content_type="image/jpeg"),
            "fv01_002.jpg": fake_response(jpeg((40, 200, 40)), content_type="image/jpeg"),
            "fv01_003.jpg": fake_response(jpeg((40, 40, 200)), content_type="image/jpeg"),
        }
    )
    result = Downloader(Torch(session), tmp_path).download(EPISODE_URL)

    assert result.status == "saved"
    assert result.save_dir == tmp_path / "to-ti.in" / "フェイバリッツ FAVORITES" / "#01"
    assert sorted(path.name for path in result.save_dir.iterdir()) == ["0.jpg", "1.jpg", "2.jpg"]
    with Image.open(result.save_dir / "2.jpg") as image:
        assert image.size == (40, 60)
        assert image.convert("RGB").getpixel((0, 0)) == pytest.approx((40, 40, 200), abs=8)
    # The images are asked for with the episode as Referer, the way a browser would.
    image_headers = [
        headers for call, headers in zip(session.calls, session.headers_seen, strict=True) if call.endswith(".jpg")
    ]
    assert len(image_headers) == 3
    assert all(headers["Referer"] == EPISODE_URL for headers in image_headers)


# --- the real site ---------------------------------------------------------------------

# One episode per known host, free to read without an account.
TEST_URLS: dict[str, str] = {
    "to-ti.in": "https://to-ti.in/story/fv_01",
}


@pytest.mark.network
@pytest.mark.parametrize("host", TEST_URLS)
def test_site_download(tmp_path, host):
    result = Downloader(Torch(), tmp_path, only_first=True).download(TEST_URLS[host])
    assert result.status == "saved"
    assert (result.save_dir / "0.jpg").stat().st_size > 0


@pytest.mark.network
def test_work_page_lists_episodes():
    urls = Torch().series_urls("https://to-ti.in/product/favorites")
    assert "https://to-ti.in/story/fv_01" in urls
    assert all(Torch.suitable(url) for url in urls)
