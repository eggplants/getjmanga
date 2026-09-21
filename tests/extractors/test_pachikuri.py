from __future__ import annotations

from datetime import date
from http import HTTPStatus
from io import BytesIO

import pytest
from httpx2 import HTTPStatusError
from PIL import Image

from getjmanga.downloader import Downloader
from getjmanga.errors import NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractors.pachikuri import (
    Pachikuri,
)

WORK_URL = "https://pachikuri.jp/mofy/"
EPISODE_URL = "https://pachikuri.jp/mofy/%e5%a4%8f%e3%81%ae%e6%80%9d%e3%81%84%e5%87%ba-2/"
PREV_SHORT_URL = "https://pachikuri.jp/?p=32786"
NEXT_SHORT_URL = "https://pachikuri.jp/?p=32900"
FIRST_URL = "https://pachikuri.jp/toripeto/%e7%94%9f%e3%81%be%e3%82%8c%e3%81%a6%e3%81%af%e3%81%98%e3%82%81%e3%81%a6/"

SCALED_IMAGE = "https://pachikuri.jp/wp-content/uploads/2026/09/mofy_web710-scaled.jpg"
ORIGINAL_IMAGE = "https://pachikuri.jp/wp-content/uploads/2026/09/mofy_web710.jpg"
THUMBNAIL = "https://pachikuri.jp/wp-content/uploads/2026/09/4ec275197ad6ca350c7c05b830ad9a08.jpg"

# The parts of an episode page `episode()` reads, as the site writes them: the
# header with the category, the author, the number and the title, the
# `main#js-manga` with a hidden thumbnail and the pages, and the pager.
EPISODE_HTML = f"""
<html><head><title>夏の思い出 - 無料で読める漫画・４コマサイト | パチクリ！</title></head><body>
<section class="mangaHead"><div class="headline__txt--mangaHead">
<div class="headline__txt__name--mangaHead"><ul class="post-categories">
<li><a href="{WORK_URL}" rel="category tag">うさぎのモフィ</a></li></ul></div>
<div class="headline__txt__author--mangaHead">コンドウ アキ</div></div>
<div class="mangaHead__updated"> 第710話 │ \t\t\t2026.9.16 (Wed)</div>
<h1 class="mangaHead__title">夏の思い出</h1></section>
<main class="manga row sample" id="js-manga">
 <span class="hidden_image"><img width="350" height="350" class="size-full" src="{THUMBNAIL}" alt="" /></span>
 <img width="1813" height="2560" class="size-full wp-image-32817 aligncenter" src="{SCALED_IMAGE}" alt="" />
 <p>&nbsp;</p>
 <span class="not_resizing"><img class="size-full" src="/wp-content/uploads/2026/09/mofy_web710b.jpg" alt="" /></span>
</main>
<p class="mangaNextUpdate">毎週水曜更新</p>
<section class="mangaFuncs">
 <a class="mangaFuncs__btn mangaFuncs__btn--prev" href="{PREV_SHORT_URL}"> <i class="fa"></i>前の話へ </a>
 <a class="mangaFuncs__btn mangaFuncs__btn--next" href="{NEXT_SHORT_URL}"> 次の話へ<i class="fa"></i> </a>
</section>
<div class="footerBtnWrap footerBtnWrap--manga"> <a class="footerBtn" href="{WORK_URL}">作品紹介ページへもどる</a></div>
</body></html>
"""

# The latest episode: `次の話へ` is a disabled span, `前の話へ` a link.
LATEST_HTML = f"""
<html><body>
<section class="mangaHead">
<ul class="post-categories"><li><a href="{WORK_URL}">うさぎのモフィ</a></li></ul>
<div class="mangaHead__updated"> 第710話 │ 2026.9.16 (Wed)</div>
<h1 class="mangaHead__title">夏の思い出</h1></section>
<main class="manga row sample" id="js-manga">
 <img class="size-full aligncenter" src="{SCALED_IMAGE}" alt="" />
</main>
<section class="mangaFuncs">
 <a class="mangaFuncs__btn mangaFuncs__btn--prev" href="{PREV_SHORT_URL}">前の話へ</a>
 <span class="mangaFuncs__btn mangaFuncs__btn--next mangaFuncs__btn--disabled"> 次の話へ </span>
</section>
</body></html>
"""

# A post with the viewer but not a single page image.
EMPTY_HTML = f"""
<html><body>
<section class="mangaHead">
<ul class="post-categories"><li><a href="{WORK_URL}">うさぎのモフィ</a></li></ul>
<div class="mangaHead__updated"> 第1話 │ 2017.6.21 (Wed)</div>
<h1 class="mangaHead__title">はじまり</h1></section>
<main class="manga row sample" id="js-manga"><p>公開終了しました。</p></main>
<section class="mangaFuncs">
 <span class="mangaFuncs__btn mangaFuncs__btn--prev mangaFuncs__btn--disabled">前の話へ</span>
 <a class="mangaFuncs__btn mangaFuncs__btn--next" href="{NEXT_SHORT_URL}">次の話へ</a>
</section>
</body></html>
"""

# A news post: a WordPress page without the manga header or the viewer.
NEWS_HTML = """
<html><body><article class="news"><h1>お知らせ</h1><p>新連載スタート</p></article></body></html>
"""

# The first page of a work's listing: newest first, eight per page, `link[rel=next]` for the rest.
WORK_HTML = f"""
<html><head><title>うさぎのモフィ - 無料で読める漫画・４コマサイト | パチクリ！</title>
<link rel="next" href="https://pachikuri.jp/mofy/page/2/" /></head><body>
<section class="sakuhinFuncs"><div class="sakuhinFuncs__btnLatest"><a href="https://pachikuri.jp/?p=32815">最新話を読む</a></div></section>
<section class="sakuhinDtails row">
<h1 class="sakuhinDtails__name">うさぎのモフィ<span class="sakuhinDtails__author">
<span class="pcOnly">/</span> コンドウ アキ</span></h1>
</section>
<section class="mangaList row">
<article class="mangaList__item"><a class="mangaList__link" href="{EPISODE_URL}">
<p class="mangaList__title">Vol.710　夏の思い出</p></a></article>
<article class="mangaList__item"><a class="mangaList__link" href="https://pachikuri.jp/mofy/%e7%ab%b6-%e4%ba%89/">
<p class="mangaList__title">Vol.709　競 争</p></a></article>
<article class="mangaList__item"><a class="mangaList__link" href="https://pachikuri.jp/mofy/%e7%ab%b6-%e4%ba%89/#dup">
<p class="mangaList__title">Vol.709　競 争</p></a></article>
</section>
<section class="relationSakuhin row"><a class="sakuhinItem__link" href="https://pachikuri.jp/4kuma/">作品ページへ</a></section>
</body></html>
"""

# The last page of the listing: `link[rel=prev]` only.
WORK_PAGE_2_HTML = """
<html><head><link rel="prev" href="https://pachikuri.jp/mofy/" /></head><body>
<h1 class="sakuhinDtails__name">うさぎのモフィ<span class="sakuhinDtails__author">/ コンドウ アキ</span></h1>
<section class="mangaList row">
<article class="mangaList__item"><a class="mangaList__link" href="/mofy/%e7%99%ba-%e8%a6%8b-5/">
<p class="mangaList__title">Vol.708　発 見</p></a></article>
<article class="mangaList__item"><a class="mangaList__link" href="https://pachikuri.jp/mofy/%e5%8f%b0-%e9%a2%a8/">
<p class="mangaList__title">Vol.707　台 風</p></a></article>
</section>
</body></html>
"""

# A work with the frame but nothing listed.
EMPTY_WORK_HTML = """
<html><body><h1 class="sakuhinDtails__name">新作</h1><section class="mangaList row"></section></body></html>
"""


def png(size=(4, 4), colour=(200, 30, 30)):
    raw = BytesIO()
    Image.new("RGB", size, colour).save(raw, "PNG")
    return raw.getvalue()


@pytest.fixture
def client(fake_session, fake_response):
    def build(routes=None):
        merged = dict(routes or {})
        # The short links 301 to the slug URL; the fake stands the redirect in with `url`.
        merged.setdefault(PREV_SHORT_URL, fake_response(text=EPISODE_HTML, url=EPISODE_URL))
        merged.setdefault(EPISODE_URL, fake_response(text=EPISODE_HTML))
        merged.setdefault("/mofy/page/2/", fake_response(text=WORK_PAGE_2_HTML))
        merged.setdefault(WORK_URL, fake_response(text=WORK_HTML))
        merged.setdefault(ORIGINAL_IMAGE, fake_response(png(colour=(10, 200, 10)), content_type="image/png"))
        merged.setdefault("/wp-content/uploads/", fake_response(png(), content_type="image/png"))
        session = fake_session(merged)
        return Pachikuri(session), session

    return build


# --- URLs -----------------------------------------------------------------------------
@pytest.mark.parametrize(
    "url",
    [
        EPISODE_URL,
        "https://pachikuri.jp/kogepan/30895/",
        "https://pachikuri.jp/bon_yo-soro/%e9%80%a3%e8%bc%89",
        PREV_SHORT_URL,
        "https://pachikuri.jp/?p=1705",
        WORK_URL,
        "https://pachikuri.jp/zubizuba",
        "https://pachikuri.jp/ka_chanhonpo/",
    ],
)
def test_suitable_accepts_episode_short_link_and_work_urls(url):
    assert Pachikuri.suitable(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://pachikuri.jp/mofy/",
        "https://www.pachikuri.jp/mofy/",
        "https://example.com/mofy/",
        "https://pachikuri.jp/",
        "https://pachikuri.jp/?cat=9",
        "https://pachikuri.jp/?p=abc",
        "https://pachikuri.jp/news/",
        "https://pachikuri.jp/list_manga/",
        "https://pachikuri.jp/sitepolicy/",
        "https://pachikuri.jp/mofy/page/2/",
        "https://pachikuri.jp/news/2023/04/14/%e3%82%ad/",
        "https://pachikuri.jp/wp-content/uploads/2026/09/mofy_web710.jpg",
    ],
)
def test_suitable_rejects_other_urls(url):
    assert not Pachikuri.suitable(url)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (WORK_URL, True),
        ("https://pachikuri.jp/zubizuba", True),
        (EPISODE_URL, False),
        (PREV_SHORT_URL, False),
        ("https://pachikuri.jp/news/", False),
    ],
)
def test_is_series_by_url_shape(url, expected):
    assert Pachikuri.is_series(url) is expected


# --- the parsers -----------------------------------------------------------------------


# --- episode() -------------------------------------------------------------------------
def test_episode_reads_the_titles_the_pages_and_the_next_episode(client):
    pachikuri, session = client()
    episode = pachikuri.episode(EPISODE_URL)

    assert episode.url == EPISODE_URL
    assert episode.series_title == "うさぎのモフィ"
    assert (episode.writer, episode.publisher) == ("コンドウ アキ", "主婦と生活社")
    assert (episode.published, episode.number) == (date(2026, 9, 16), 4)
    assert episode.episode_title == "第710話 夏の思い出"
    assert [page.url for page in episode.pages] == [
        SCALED_IMAGE,
        "https://pachikuri.jp/wp-content/uploads/2026/09/mofy_web710b.jpg",
    ]
    assert episode.pages[0].extra == {"original": ORIGINAL_IMAGE}
    assert episode.pages[1].extra == {}
    assert episode.next_url == NEXT_SHORT_URL
    assert episode.metadata == {
        "title": "夏の思い出",
        "number": "第710話",
        "date": "2026.9.16 (Wed)",
        "author": "コンドウ アキ",
        "work_url": WORK_URL,
        "prev_url": PREV_SHORT_URL,
    }
    # The episode, then the work's listing (two pages) for where the episode stands in it.
    assert session.calls == [EPISODE_URL, WORK_URL, "https://pachikuri.jp/mofy/page/2/"]
    assert session.headers_seen[0]["User-Agent"]


def test_episode_follows_a_short_link_to_the_slug_url(client):
    pachikuri, session = client()
    episode = pachikuri.episode(PREV_SHORT_URL)

    assert session.calls[0] == PREV_SHORT_URL
    assert episode.url == EPISODE_URL
    assert episode.readable


def test_latest_episode_has_no_next(client, fake_response):
    pachikuri, _ = client({EPISODE_URL: fake_response(text=LATEST_HTML)})
    episode = pachikuri.episode(EPISODE_URL)

    assert (episode.prev_url, episode.next_url) == (PREV_SHORT_URL, None)
    assert episode.metadata["prev_url"] == PREV_SHORT_URL


def test_episode_without_page_images_is_locked_and_keeps_its_next(client, fake_response):
    pachikuri, _ = client({EPISODE_URL: fake_response(text=EMPTY_HTML)})
    episode = pachikuri.episode(EPISODE_URL)

    assert not episode.readable
    assert episode.pages == ()
    assert episode.episode_title == "第1話 はじまり"
    assert episode.next_url == NEXT_SHORT_URL


def test_episode_title_without_a_number_is_the_title_alone(client, fake_response):
    html = EPISODE_HTML.replace("第710話 │ ", "")
    pachikuri, _ = client({EPISODE_URL: fake_response(text=html)})
    episode = pachikuri.episode(EPISODE_URL)

    assert episode.episode_title == "夏の思い出"
    assert episode.metadata["number"] == ""
    assert episode.metadata["date"] == "2026.9.16 (Wed)"


def test_episode_refuses_a_work_url_and_a_news_url(client):
    pachikuri, session = client()
    with pytest.raises(UnsupportedUrlError, match="not an episode page"):
        pachikuri.episode(WORK_URL)
    with pytest.raises(UnsupportedUrlError, match="not an episode page"):
        pachikuri.episode("https://pachikuri.jp/news/2023/04/14/x/")
    assert session.calls == []


def test_page_without_the_viewer_is_not_an_episode(client, fake_response):
    pachikuri, _ = client({EPISODE_URL: fake_response(text=NEWS_HTML)})
    with pytest.raises(NotAnEpisodePageError, match="no episode"):
        pachikuri.episode(EPISODE_URL)


def test_missing_post_is_not_an_episode(client, fake_response):
    pachikuri, _ = client({EPISODE_URL: fake_response(text="<html>404</html>", status_code=HTTPStatus.NOT_FOUND)})
    with pytest.raises(NotAnEpisodePageError, match="404"):
        pachikuri.episode(EPISODE_URL)


def test_other_http_errors_propagate(client, fake_response):
    pachikuri, _ = client({EPISODE_URL: fake_response(text="", status_code=HTTPStatus.SERVICE_UNAVAILABLE)})
    with pytest.raises(HTTPStatusError):
        pachikuri.episode(EPISODE_URL)


# --- series_urls() ---------------------------------------------------------------------
def test_series_urls_walks_the_listing_and_lists_oldest_first(client):
    pachikuri, session = client()
    urls = pachikuri.series_urls(WORK_URL)

    assert urls == [
        "https://pachikuri.jp/mofy/%e5%8f%b0-%e9%a2%a8/",
        "https://pachikuri.jp/mofy/%e7%99%ba-%e8%a6%8b-5/",
        "https://pachikuri.jp/mofy/%e7%ab%b6-%e4%ba%89/",
        EPISODE_URL,
    ]
    assert session.calls == [WORK_URL, "https://pachikuri.jp/mofy/page/2/"]
    assert all(Pachikuri.suitable(url) and not Pachikuri.is_series(url) for url in urls)


def test_series_urls_stops_at_a_listing_page_seen_before(client, fake_response):
    looping = WORK_HTML.replace('href="https://pachikuri.jp/mofy/page/2/"', f'href="{WORK_URL}"')
    pachikuri, session = client({WORK_URL: fake_response(text=looping)})

    assert len(pachikuri.series_urls(WORK_URL)) == 2
    assert session.calls == [WORK_URL]


def test_series_urls_raises_on_an_empty_listing(client, fake_response):
    pachikuri, _ = client({WORK_URL: fake_response(text=EMPTY_WORK_HTML)})
    with pytest.raises(NotAnEpisodePageError, match="lists no episode"):
        pachikuri.series_urls(WORK_URL)


def test_series_urls_raises_on_a_missing_work(client, fake_response):
    pachikuri, _ = client({WORK_URL: fake_response(text="404", status_code=HTTPStatus.NOT_FOUND)})
    with pytest.raises(NotAnEpisodePageError, match="404"):
        pachikuri.series_urls(WORK_URL)


def test_series_urls_refuses_an_episode_url(client):
    pachikuri, session = client()
    with pytest.raises(UnsupportedUrlError, match="not a work page"):
        pachikuri.series_urls(EPISODE_URL)
    assert session.calls == []


# --- image() and the downloader --------------------------------------------------------
def test_image_prefers_the_unscaled_original(client):
    pachikuri, session = client()
    episode = pachikuri.episode(EPISODE_URL)
    image = pachikuri.image(episode.pages[0], episode)

    assert image.getpixel((0, 0)) == (10, 200, 10)
    assert session.calls[-1] == ORIGINAL_IMAGE
    assert session.headers_seen[-1]["Referer"] == EPISODE_URL


def test_image_falls_back_to_the_scaled_copy(client, fake_response):
    pachikuri, session = client({ORIGINAL_IMAGE: fake_response(text="gone", status_code=HTTPStatus.NOT_FOUND)})
    episode = pachikuri.episode(EPISODE_URL)
    image = pachikuri.image(episode.pages[0], episode)

    assert image.getpixel((0, 0)) == (200, 30, 30)
    assert session.calls[-2:] == [ORIGINAL_IMAGE, SCALED_IMAGE]


def test_image_falls_back_when_the_original_is_not_an_image(client, fake_response):
    pachikuri, session = client({ORIGINAL_IMAGE: fake_response(text="<html>not found</html>")})
    episode = pachikuri.episode(EPISODE_URL)
    image = pachikuri.image(episode.pages[0], episode)

    assert image.getpixel((0, 0)) == (200, 30, 30)
    assert session.calls[-1] == SCALED_IMAGE


def test_image_without_an_original_fetches_the_page_url_only(client):
    pachikuri, session = client()
    episode = pachikuri.episode(EPISODE_URL)
    pachikuri.image(episode.pages[1], episode)

    assert session.calls[-1] == "https://pachikuri.jp/wp-content/uploads/2026/09/mofy_web710b.jpg"


# --- the real site --------------------------------------------------------------------

# One free episode per known host: the first episode of a finished work.
TEST_URLS: dict[str, str] = {
    "pachikuri.jp": FIRST_URL,
}


@pytest.mark.network
@pytest.mark.parametrize("host", TEST_URLS)
def test_site_download(tmp_path, host):
    result = Downloader(Pachikuri(), tmp_path, only_first=True).download(TEST_URLS[host])
    assert result.status == "saved"
    assert result.episode.series_title == "トリペと7　でこぼこステップ"
    assert result.episode.episode_title == "第001話 生まれてはじめて"
    assert result.episode.next_url == "https://pachikuri.jp/?p=2053"
    assert (result.save_dir / "0.jpg").stat().st_size > 0


@pytest.mark.network
def test_short_link_resolves_to_the_same_episode():
    episode = Pachikuri().episode("https://pachikuri.jp/?p=1705")
    assert episode.url == FIRST_URL
    assert episode.readable


@pytest.mark.network
def test_work_page_lists_episodes_oldest_first():
    urls = Pachikuri().series_urls("https://pachikuri.jp/kogepan/")
    assert len(urls) >= 10
    assert urls[0] == "https://pachikuri.jp/kogepan/%e3%81%93%e3%81%ae%e9%9f%b3%e3%81%af/"
    assert all(Pachikuri.suitable(url) and not Pachikuri.is_series(url) for url in urls)
