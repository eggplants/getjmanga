from __future__ import annotations

from http import HTTPStatus
from io import BytesIO

import pytest
from PIL import Image

from getjmanga.downloader import Downloader
from getjmanga.errors import NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractors.leedcafe import (
    ALM_URL,
    LeedCafe,
    episode_slug,
    episode_url,
    parse_episode_page,
    parse_work_page,
    work_slug,
    work_url,
)

WORK_SLUG = "連続怪奇シリーズ　黄色い悪夢"
WORK_URL_ENCODED = (
    "https://leedcafe.com/webcomicinfo/"
    "%e9%80%a3%e7%b6%9a%e6%80%aa%e5%a5%87%e3%82%b7%e3%83%aa%e3%83%bc%e3%82%ba%e3%80%80%e9%bb%84%e8%89%b2%e3%81%84%e6%82%aa%e5%a4%a2/"
)
WORK_URL = work_url(WORK_SLUG)

FIRST_SLUG = "黄色い悪夢-第１話「断末夢」"
SECOND_SLUG = "黄色い悪夢　第９回『ライクアローリングストー"
THIRD_SLUG = "黄色い悪夢　第９回『ライクアローリングストー-2"
FIRST_URL = episode_url(FIRST_SLUG)
SECOND_URL = episode_url(SECOND_SLUG)
THIRD_URL = episode_url(THIRD_SLUG)
# The second episode, percent-encoded in lowercase the way the site links it.
SECOND_URL_SITE = (
    "https://leedcafe.com/webcomic/"
    "%e9%bb%84%e8%89%b2%e3%81%84%e6%82%aa%e5%a4%a2%e3%80%80%e7%ac%ac%ef%bc%99%e5%9b%9e"
    "%e3%80%8e%e3%83%a9%e3%82%a4%e3%82%af%e3%82%a2%e3%83%ad%e3%83%bc%e3%83%aa%e3%83%b3%e3%82%b0%e3%82%b9%e3%83%88%e3%83%bc/"
)

IMAGE_1 = "https://leedcafe.com/wp-content/uploads/2023/08/08def01618d8ed17082d598cfb6123e6.jpg"
IMAGE_2 = "https://leedcafe.com/wp-content/uploads/2023/08/d656b31082f8a3e4dbd364c5fae60007.jpg"


def _episode_html(*, heading, prev_url=None, next_url=None, images=((IMAGE_1, 705, 1000), (IMAGE_2, 1410, 1000))):
    """An episode page as the site writes it: heading, header link, gallery script, footer nav."""
    nav = ""
    if prev_url:
        nav += f'<div class="col-xs-4"><a href="{prev_url}"><i class="fa fa-arrow-circle-left fa-2x"></i></a></div>'
    else:
        nav += '<div class="col-xs-4"></div>'
    nav += f'<div class="col-xs-4"><a href="{WORK_URL_ENCODED}"><i class="fa fa-dot-circle-o fa-2x"></i></a></div>'
    if next_url:
        nav += f'<div class="col-xs-4"><a href="{next_url}"><i class="fa fa-arrow-circle-right fa-2x"></i></a></div>'
    else:
        nav += '<div class="col-xs-4"></div>'
    gallery = "".join(
        f"""jQuery('.gallery').append('<img width="{width}" height="{height}" src="{src}" class="attachment-full size-full" alt="" ids="81706,81707" orderby="post__in" srcset="{src} {width}w, {src[:-4]}-212x300.jpg 212w" sizes="(max-width: {width}px) 100vw, {width}px" />');"""  # noqa: E501
        for src, width, height in images
    )
    return f"""<!DOCTYPE html><html><head><title>黄色い悪夢 第19回 - LEED Cafe | リイドカフェ</title></head>
<body oncontextmenu="return false;">
<header class="webcomic-header row">
  <div class="col-xs-2 backto-list"><a href="{WORK_URL_ENCODED}"><i class="fa fa-arrow-left fa-2x"></i></a></div>
  <div class="col-xs-8 text-center"><h1 id="title-comic" itemscope itemtype="http://schema.org/Organization">{heading}</h1></div>
</header>
<main><article id="post-81705" class="cf post-81705 webcomic type-webcomic status-publish">
<section class="entry-content clearfix">
  <div id='gallery-1' class='gallery galleryid-81705'><noscript>JavaScriptを有効にしてください。</noscript></div>
  <div class="small">更新日 <time class="updated entry-time" datetime="2023-08-30">08月30日</time></div>
</section>
<footer class="article-footer"><nav class="footer-nav row">{nav}</nav></footer>
</article></main>
<script>{gallery}</script>
<script src="https://leedcafe.com/wp-content/plugins/a9-web-comic/public/js/a9-web-comic-public.js"></script>
</body></html>"""


EPISODE_HTML = _episode_html(
    heading="2. 黄色い悪夢　第９回『ライクアローリングストーン』前編", prev_url=FIRST_URL, next_url=THIRD_URL
)
# The first episode: the site shows no next arrow on it although the listing goes on.
FIRST_HTML = _episode_html(heading="1. 黄色い悪夢　第１回「断末夢」")
# An episode page whose gallery is empty.
EMPTY_HTML = _episode_html(heading="3. 黄色い悪夢　休載のお知らせ", images=())
# Not an episode at all.
NOT_EPISODE_HTML = "<html><body><h1 class='site-branding__heading'>LEED Cafe</h1><p>Nothing here.</p></body></html>"

WORK_HTML = f"""<!DOCTYPE html><html><head><title>連続怪奇シリーズ 黄色い悪夢 - LEED Cafe | リイドカフェ</title></head><body>
<div class="breadcrumbs"><a href="https://leedcafe.com/">ホーム</a> &gt; <a href="https://leedcafe.com/webcomicinfo/">作品情報</a> &gt; <strong>連続怪奇シリーズ　黄色い悪夢</strong></div>
<article class="article article--single post-1410 webcomicinfo">
<div class="webcomic-header"><a class="genre" href="/genre/x">サスペンス</a><img src="http://leedcafe.com/wp-content/uploads/2017/05/kiirolong.jpg" alt="連続怪奇シリーズ　黄色い悪夢" width="100%" /><div class="bottom">連続怪奇シリーズ　黄色い悪夢/黄島点心</div></div>
</article>
<div class="row list-episode clearfix">
<div id="ajax-load-more" class="ajax-load-more-wrap default" data-canonical-url="{WORK_URL_ENCODED}" data-post-id="1410">
<ul aria-live="polite" class="alm-listing alm-ajax" data-container-type="ul" data-repeater="default" data-post-type="webcomic" data-meta-key="a9_webcomic_info" data-meta-value="1410" data-order="DESC" data-orderby="date" data-offset="0" data-posts-per-page="10" data-scroll="false" data-button-label="もっと読む"></ul>
<div class="alm-btn-wrap"><button class="alm-load-more-btn" type="button">もっと読む</button></div></div></div>
</body></html>"""  # noqa: E501


def _item(url, title):
    return f"""
<div class="col-xs-12 col-sm-6 item-episode"><div class="row"><div class="inner">
  <div class="col-xs-3"><a href="{url}"><img src="/wp-content/uploads/x-150x150.jpg" /></a></div>
  <div class="col-xs-9"><p><span style="font-weight:bold;">{title}</span><br>
  奇才・黄島点心の最新作！<br>
  <a class="btn btn-default" href="{url}" target="_blank">この話を読む</a></p></div>
</div></div></div>"""


# The listing as the Ajax Load More endpoint answers it, oldest first, the
# second episode listed twice (once under its lowercase site link).
LISTING_HTML = (
    _item(FIRST_URL, "黄色い悪夢　第１回「断末夢」")
    + _item(SECOND_URL_SITE, "黄色い悪夢　第９回『ライクアローリングストーン』前編")
    + _item(SECOND_URL, "黄色い悪夢　第９回『ライクアローリングストーン』前編")
    + _item(THIRD_URL, "黄色い悪夢　第９回『ライクアローリングストーン』後編")
)
LISTING_JSON = {"html": LISTING_HTML, "meta": {"postcount": 3, "totalposts": 3, "debug": False}}
EMPTY_LISTING_JSON = {"html": "", "meta": {"postcount": 0, "totalposts": 0, "debug": False}}


@pytest.fixture
def client(fake_session):
    def make(routes):
        session = fake_session(routes)
        return LeedCafe(session), session

    return make


# --- URLs ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        WORK_URL_ENCODED,
        WORK_URL,
        "https://leedcafe.com/webcomicinfo/hondakanoko/",
        "https://leedcafe.com/webcomicinfo/hondakanoko",
        "https://leedcafe.com/webcomic/黄色い悪夢　第19回「考える手」その１/",
        SECOND_URL_SITE,
        SECOND_URL,
        "https://leedcafe.com/webcomic/carrera-gt3-1/",
    ],
)
def test_suitable_accepts_episode_and_work_pages(url):
    assert LeedCafe.suitable(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://leedcafe.com/webcomic/carrera-gt3-1/",
        "https://www.leedcafe.com/webcomic/carrera-gt3-1/",
        "https://leedcafe.com/",
        "https://leedcafe.com/webcomicinfo/",
        "https://leedcafe.com/webcomic/",
        "https://leedcafe.com/back-numbers",
        "https://leedcafe.com/creator/%e9%bb%84%e5%b3%b6%e7%82%b9%e5%bf%83/",
        "https://leedcafe.com/webcomic/a/b/",
        "https://to-ti.in/story/xxx",
    ],
)
def test_suitable_rejects_other_urls(url):
    assert not LeedCafe.suitable(url)


def test_is_series_only_for_work_pages():
    assert LeedCafe.is_series(WORK_URL_ENCODED)
    assert LeedCafe.is_series("https://leedcafe.com/webcomicinfo/hondakanoko")
    assert not LeedCafe.is_series(SECOND_URL_SITE)
    assert not LeedCafe.is_series("https://leedcafe.com/")


def test_slugs_are_decoded_and_urls_reencoded():
    assert episode_slug(SECOND_URL_SITE) == SECOND_SLUG
    assert episode_slug(SECOND_URL) == SECOND_SLUG
    assert episode_slug("https://leedcafe.com/webcomic/黄色い悪夢　第１回/") == "黄色い悪夢　第１回"
    assert episode_slug(WORK_URL) is None
    assert work_slug(WORK_URL_ENCODED) == WORK_SLUG
    assert work_slug(SECOND_URL) is None
    assert episode_url("carrera-gt3-1") == "https://leedcafe.com/webcomic/carrera-gt3-1/"
    assert work_url("hondakanoko") == "https://leedcafe.com/webcomicinfo/hondakanoko/"
    assert episode_url(SECOND_SLUG) == SECOND_URL
    assert "%E9%BB%84" in SECOND_URL


# --- parsing ------------------------------------------------------------------------


def test_parse_episode_page_without_counter_or_nav():
    page = parse_episode_page(_episode_html(heading="黄色い悪夢　第１回「断末夢」").encode(), FIRST_URL)
    assert page.number == 0
    assert page.title == "黄色い悪夢　第１回「断末夢」"
    assert page.prev_url is None
    assert page.next_url is None


def test_parse_work_page_falls_back_to_the_cover_and_the_title_tag():
    no_crumb = WORK_HTML.replace("<strong>連続怪奇シリーズ　黄色い悪夢</strong>", "")
    assert parse_work_page(no_crumb, WORK_URL).title == "連続怪奇シリーズ　黄色い悪夢"
    no_cover = no_crumb.replace('alt="連続怪奇シリーズ　黄色い悪夢"', 'alt=""')
    assert parse_work_page(no_cover, WORK_URL).title == "連続怪奇シリーズ 黄色い悪夢"


# --- the series -----------------------------------------------------------------------


@pytest.mark.parametrize("url", [WORK_URL_ENCODED, WORK_URL])
def test_series_urls_lists_oldest_first_and_deduplicates(client, fake_response, url):
    leedcafe, session = client(
        {
            "/webcomicinfo/": fake_response(text=WORK_HTML),
            "admin-ajax.php": fake_response(payload=LISTING_JSON),
        },
    )
    urls = leedcafe.series_urls(url)

    assert urls == [FIRST_URL, SECOND_URL, THIRD_URL]
    assert all(LeedCafe.suitable(url) for url in urls)
    assert session.calls[0] == WORK_URL
    assert session.calls[1] == ALM_URL
    assert session.params_seen[1] == {
        "action": "alm_get_posts",
        "post_type": "webcomic",
        "meta_key": "a9_webcomic_info",
        "meta_value": "1410",
        "order": "ASC",
        "orderby": "date",
        "posts_per_page": 100,
        "page": 0,
    }
    assert session.headers_seen[1]["Referer"] == WORK_URL
    assert session.headers_seen[1]["X-Requested-With"] == "XMLHttpRequest"


def test_series_urls_pages_through_a_long_listing(client, fake_response):
    pages = [
        {"html": "".join(_item(episode_url(f"ep-{i}"), f"ep {i}") for i in range(100)), "meta": {"totalposts": 105}},
        {
            "html": "".join(_item(episode_url(f"ep-{i}"), f"ep {i}") for i in range(100, 105)),
            "meta": {"totalposts": 105},
        },
    ]
    leedcafe, session = client(
        {
            "/webcomicinfo/": fake_response(text=WORK_HTML),
            "admin-ajax.php": [fake_response(payload=page) for page in pages],
        },
    )
    urls = leedcafe.series_urls(WORK_URL)

    assert len(urls) == 105
    assert urls[0] == episode_url("ep-0")
    assert urls[-1] == episode_url("ep-104")
    assert [params["page"] for params in session.params_seen[1:]] == [0, 1]


def test_series_urls_raises_on_an_empty_listing(client, fake_response):
    leedcafe, _ = client(
        {
            "/webcomicinfo/": fake_response(text=WORK_HTML),
            "admin-ajax.php": fake_response(payload=EMPTY_LISTING_JSON),
        },
    )
    with pytest.raises(NotAnEpisodePageError, match="lists no episode"):
        leedcafe.series_urls(WORK_URL)


def test_series_urls_raises_on_a_missing_work(client, fake_response):
    leedcafe, _ = client({"/webcomicinfo/": fake_response(text="gone", status_code=HTTPStatus.NOT_FOUND)})
    with pytest.raises(NotAnEpisodePageError, match="404"):
        leedcafe.series_urls("https://leedcafe.com/webcomicinfo/nope/")


def test_series_urls_rejects_an_episode_url(client):
    leedcafe, _ = client({})
    with pytest.raises(UnsupportedUrlError):
        leedcafe.series_urls(SECOND_URL)


# --- the episode --------------------------------------------------------------------


def test_episode_reads_the_titles_the_pages_and_the_next_episode(client, fake_response):
    leedcafe, session = client(
        {
            "/webcomic/": fake_response(text=EPISODE_HTML),
            "/webcomicinfo/": fake_response(text=WORK_HTML),
            "admin-ajax.php": fake_response(payload=LISTING_JSON),
        },
    )
    episode = leedcafe.episode(SECOND_URL_SITE)

    assert episode.url == SECOND_URL
    assert episode.series_title == "連続怪奇シリーズ　黄色い悪夢"
    assert episode.episode_title == "黄色い悪夢　第９回『ライクアローリングストーン』前編"
    assert [page.url for page in episode.pages] == [IMAGE_1, IMAGE_2]
    assert (episode.pages[1].width, episode.pages[1].height) == (1410, 1000)
    assert episode.next_url == THIRD_URL
    assert episode.metadata["post_id"] == "81705"
    assert episode.metadata["number"] == 2
    assert episode.metadata["work_url"] == WORK_URL
    assert episode.metadata["updated"] == "2023-08-30"
    assert episode.metadata["prev_url"] == FIRST_URL
    assert session.calls[0] == SECOND_URL


def test_episode_next_comes_from_the_listing_when_the_page_has_no_arrow(client, fake_response):
    leedcafe, _ = client(
        {
            "/webcomic/": fake_response(text=FIRST_HTML),
            "/webcomicinfo/": fake_response(text=WORK_HTML),
            "admin-ajax.php": fake_response(payload=LISTING_JSON),
        },
    )
    episode = leedcafe.episode(FIRST_URL)

    assert episode.episode_title == "黄色い悪夢　第１回「断末夢」"
    assert episode.next_url == SECOND_URL
    assert episode.metadata["nav_next_url"] is None


def test_episode_falls_back_to_the_arrow_when_unlisted(client, fake_response):
    html = _episode_html(heading="9. 黄色い悪夢　第５回", next_url=THIRD_URL)
    leedcafe, _ = client(
        {
            "/webcomic/": fake_response(text=html),
            "/webcomicinfo/": fake_response(text=WORK_HTML),
            "admin-ajax.php": fake_response(payload=LISTING_JSON),
        },
    )
    episode = leedcafe.episode("https://leedcafe.com/webcomic/unlisted/")

    assert episode.episode_title == "黄色い悪夢　第５回"
    assert episode.next_url == THIRD_URL


def test_episode_is_the_last_one_when_the_listing_ends(client, fake_response):
    html = _episode_html(heading="3. 黄色い悪夢　第９回『ライクアローリングストーン』後編", prev_url=SECOND_URL)
    leedcafe, _ = client(
        {
            "/webcomic/": fake_response(text=html),
            "/webcomicinfo/": fake_response(text=WORK_HTML),
            "admin-ajax.php": fake_response(payload=LISTING_JSON),
        },
    )
    assert leedcafe.episode(THIRD_URL).next_url is None


def test_episode_follows_a_redirect_to_the_real_slug(client, fake_response):
    leedcafe, _ = client(
        {
            "/webcomic/": fake_response(text=EPISODE_HTML, url=SECOND_URL_SITE),
            "/webcomicinfo/": fake_response(text=WORK_HTML),
            "admin-ajax.php": fake_response(payload=LISTING_JSON),
        },
    )
    episode = leedcafe.episode("https://leedcafe.com/webcomic/黄色い悪夢　第９回『ライクアローリングストーン』前編/")

    assert episode.url == SECOND_URL
    assert episode.next_url == THIRD_URL


def test_episode_without_a_work_link_uses_its_own_title(client, fake_response):
    html = EPISODE_HTML.replace('<div class="col-xs-2 backto-list">', '<div class="col-xs-2">')
    leedcafe, session = client({"/webcomic/": fake_response(text=html)})
    episode = leedcafe.episode(SECOND_URL)

    assert episode.series_title == "黄色い悪夢　第９回『ライクアローリングストーン』前編"
    assert episode.next_url == THIRD_URL
    assert session.calls == [SECOND_URL]


def test_episode_with_an_empty_gallery_has_no_pages(client, fake_response):
    leedcafe, _ = client(
        {
            "/webcomic/": fake_response(text=EMPTY_HTML),
            "/webcomicinfo/": fake_response(text=WORK_HTML),
            "admin-ajax.php": fake_response(payload=LISTING_JSON),
        },
    )
    episode = leedcafe.episode("https://leedcafe.com/webcomic/notice/")

    assert not episode.readable
    assert episode.pages == ()
    assert episode.episode_title == "黄色い悪夢　休載のお知らせ"


def test_episode_raises_on_a_deleted_episode(client, fake_response):
    leedcafe, _ = client({"/webcomic/": fake_response(text="gone", status_code=HTTPStatus.NOT_FOUND)})
    with pytest.raises(NotAnEpisodePageError, match="404"):
        leedcafe.episode("https://leedcafe.com/webcomic/expired/")


def test_episode_raises_on_a_page_without_an_episode(client, fake_response):
    leedcafe, _ = client({"/webcomic/": fake_response(text=NOT_EPISODE_HTML)})
    with pytest.raises(NotAnEpisodePageError, match="no episode"):
        leedcafe.episode("https://leedcafe.com/webcomic/odd/")


def test_episode_rejects_a_work_url(client):
    leedcafe, _ = client({})
    with pytest.raises(UnsupportedUrlError):
        leedcafe.episode(WORK_URL)


# --- downloading ----------------------------------------------------------------------


def _jpeg(color):
    raw = BytesIO()
    Image.new("RGB", (6, 8), color).save(raw, "JPEG", quality=100)
    return raw.getvalue()


def test_download_writes_the_first_page_as_served(client, fake_response, tmp_path):
    leedcafe, session = client(
        {
            "/webcomic/": fake_response(text=EPISODE_HTML),
            "/webcomicinfo/": fake_response(text=WORK_HTML),
            "admin-ajax.php": fake_response(payload=LISTING_JSON),
            # Flat greys survive the JPEG round trip through the downloader exactly.
            "08def01618d8ed17082d598cfb6123e6.jpg": fake_response(_jpeg((128, 128, 128)), content_type="image/jpeg"),
            "d656b31082f8a3e4dbd364c5fae60007.jpg": fake_response(_jpeg((64, 64, 64)), content_type="image/jpeg"),
        },
    )
    result = Downloader(leedcafe, tmp_path, only_first=True).download(SECOND_URL_SITE)

    assert result.status == "saved"
    assert (
        result.save_dir
        == tmp_path
        / "leedcafe.com"
        / "連続怪奇シリーズ　黄色い悪夢"
        / "黄色い悪夢　第９回『ライクアローリングストーン』前編"
    )
    written = Image.open(result.save_dir / "0.jpg")
    assert written.size == (6, 8)
    assert written.getpixel((3, 4)) == (128, 128, 128)
    assert not (result.save_dir / "1.jpg").exists()
    assert session.calls[-1] == IMAGE_1
    assert session.headers_seen[-1]["Referer"] == SECOND_URL


# --- logging in -----------------------------------------------------------------------


# --- the real site --------------------------------------------------------------------

# One episode per known host, free to read without an account: the first
# episode of a long-running work.
TEST_URLS: dict[str, str] = {
    "leedcafe.com": "https://leedcafe.com/webcomic/%e9%bb%84%e8%89%b2%e3%81%84%e6%82%aa%e5%a4%a2-%e7%ac%ac%ef%bc%91%e8%a9%b1%e3%80%8c%e6%96%ad%e6%9c%ab%e5%a4%a2%e3%80%8d/",
}


@pytest.mark.network
@pytest.mark.parametrize("host", TEST_URLS)
def test_site_download(tmp_path, host):
    result = Downloader(LeedCafe(), tmp_path, only_first=True).download(TEST_URLS[host])
    assert result.status == "saved"
    assert result.episode.url == FIRST_URL
    assert result.episode.series_title == "連続怪奇シリーズ　黄色い悪夢"
    assert result.episode.episode_title == "黄色い悪夢　第１回「断末夢」"
    assert result.episode.next_url is not None
    assert (result.save_dir / "0.jpg").stat().st_size > 0


@pytest.mark.network
def test_work_page_lists_episodes_oldest_first():
    leedcafe = LeedCafe()
    assert leedcafe.is_series(WORK_URL_ENCODED)
    urls = leedcafe.series_urls(WORK_URL_ENCODED)
    assert urls[0] == FIRST_URL
    assert len(urls) > 1
    assert all(LeedCafe.suitable(url) for url in urls)
    assert len(set(urls)) == len(urls)
