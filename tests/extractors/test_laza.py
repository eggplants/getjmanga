from __future__ import annotations

from http import HTTPStatus
from io import BytesIO

import pytest
from httpx import ReadTimeout
from PIL import Image

from getjmanga.downloader import Downloader
from getjmanga.errors import NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractors.laza import (
    Laza,
    episode_title,
    read_body,
)

MT_EPISODE_URL = "http://laza.mandarake.co.jp/kimono-lolita/manga/001.html"
MT_LIST_URL = "http://laza.mandarake.co.jp/kimono-lolita/list.html"
OLD_EPISODE_URL = "http://laza.mandarake.co.jp/comic001/p36.html"
OLD_INDEX_URL = "http://laza.mandarake.co.jp/comic001/"

HOST = "https://laza.mandarake.co.jp"


def mt_page(name: str, title: str, *, next_name: str | None, image: str = "") -> str:
    """One Movable Type page, as the site writes it (links as http, one image, a rel=next)."""
    next_link = (
        f'<link rel="next" href="http://laza.mandarake.co.jp/kimono-lolita/manga/{next_name}.html">'
        if next_name
        else ""
    )
    next_button = (
        f'<a href="http://laza.mandarake.co.jp/kimono-lolita/manga/{next_name}.html">'
        '<img src="/kimono-lolita/img/button/next.png"></a>'
        if next_name
        else ""
    )
    image = image or f"http://laza.mandarake.co.jp/kimono-lolita/up/2021/01/29/{name}.png"
    return f"""
<!DOCTYPE html>
<html lang="ja"><head><meta charset="UTF-8">
<title>まんだらけ | 着物ちゃんとロリータちゃん - {title}</title>
<link rel="canonical" href="http://laza.mandarake.co.jp/kimono-lolita/manga/{name}.html" />
{next_link}
<meta property="og:site_name" content="着物ちゃんとロリータちゃん">
</head><body class="single">
<div class="local_head"><div class="logo"><img src="/kimono-lolita/img/home/logo-laza.png"></div></div>
<h1 itemprop="name">
			{title}
		</h1>
<div class="entry"><article id="53207"><div itemprop="articleBody"><div class="ohanashi">
<a href="http://laza.mandarake.co.jp/kimono-lolita/manga/{next_name or name}.html">
<img alt="{title}.png" src="{image}" width="600" height="900" class="mt-image-none" style="" />
</a></div></div></article>
<div class="button">
<div class="next">{next_button}</div>
<div class="top"><a href="/kimono-lolita/list.html"><img src="/kimono-lolita/img/button/top.png"></a></div>
<div class="hajime"><a href="/kimono-lolita/manga/001.html"><img src="/kimono-lolita/img/button/hajime.png"></a></div>
</div></div>
</body></html>
"""


# The episode list: newest first, an episode taken down (its entry links the
# product page), a mixed bag of link forms, one episode listed twice and a
# link into another work.
MT_LIST_HTML = """
<html><head><title>まんだらけ | 着物ちゃんとロリータちゃん - 漫画リスト</title></head><body>
<ul class="shortcut">
  <li><a href="http://laza.mandarake.co.jp/kimono-lolita/manga/005.html"><img src="img/list/saishin.png"></a></li>
  <li><a href="manga/001.html"><img src="img/list/hajime.png"></a></li>
</ul>
<ul class="thum">
<li>
 <a href="https://laza.mandarake.co.jp/kimono-lolita/item-info.html">
<img alt="3話サムネ.png" src="http://laza.mandarake.co.jp/kimono-lolita/up/2021/03/01/c.png" width="120" /> </a>
        <p>第3話</p>
</li>
<li>
 <a href="http://laza.mandarake.co.jp/kimono-lolita/manga/005.html">
<img alt="2話サムネ.png" src="http://laza.mandarake.co.jp/kimono-lolita/up/2021/02/01/b.png" width="120" /> </a>
        <p>第2話</p>
</li>
<li>
 <a href="http://laza.mandarake.co.jp/BTP_plus/manga/1.html"><img src="http://laza.mandarake.co.jp/BTP_plus/up/x.png"></a>
        <p>別の作品</p>
</li>
      <li>
        <a href="https://laza.mandarake.co.jp/kimono-lolita/manga/001.html">
          <img alt="001：第一話着物ちゃん のコピー.jpg"
               src="http://laza.mandarake.co.jp/kimono-lolita/up/2021/02/06/a.jpg" width="120" height="120" />
        </a>
        <p>第1話</p>
      </li>
      <li>
        <a href="manga/001.html"><img src="http://laza.mandarake.co.jp/kimono-lolita/up/2021/02/06/a.jpg"></a>
        <p>第1話（再掲）</p>
      </li>
</ul>
</body></html>
"""

MT_EMPTY_LIST_HTML = """
<html><body><ul class="thum">
<li><a href="https://laza.mandarake.co.jp/kimono-lolita/item-info.html"><img src="x.png"></a><p>第1話</p></li>
</ul></body></html>
"""

# An announcement in the chain: a product image linking the shop, its own title.
MT_AD_HTML = mt_page(
    "4kan",
    "4巻発売!画像クリックで公式通販ページに飛びます",
    next_name="005",
    image="http://laza.mandarake.co.jp/kimono-lolita/up/2022/07/06/FWdbqOoagAEJjA9.jpg",
)

MT_NO_IMAGE_HTML = """
<html><head><meta property="og:site_name" content="着物ちゃんとロリータちゃん"></head>
<body><h1 itemprop="name">お知らせ</h1><div class="entry"><p>更新はお休みです。</p></div></body></html>
"""

NOT_FOUND_HTML = "<html><head><title>404 Not Found</title></head><body><h1>Not Found</h1></body></html>"


def old_episode(frames: list[str], *, prev: str | None = None, next_page: str | None = None) -> str:
    """An older work's `pN.html`: the strips as iframes, an ad iframe, static arrows."""
    blocks = "\n".join(
        f'<div class="block"><div class="frame_wrap"><iframe src="{frame}" scrolling="no"></iframe></div></div>'
        for frame in frames
    )
    next_arrow = f'<a href="{next_page}"><img src="/img/arrow/1_next.png"></a>' if next_page else ""
    prev_arrow = f'<a href="{prev}"><img src="/img/arrow/1_prev.png"></a>' if prev else ""
    return f"""
<!DOCTYPE html>
<html lang="ja"><head><meta charset="utf-8">
<title>『ロリータばばあの言うことにゃ』岡野く仔  | まんだらけWEBコミック ラザ</title>
</head><body>
<nav><div class="comic_nav"><div class="menu"><ul>
<li><a href="./">作品トップ</a></li><li><a href="prod.html">コミックス情報</a></li>
</ul></div></div></nav>
<article>
<div class="comic_blocklist">
{blocks}
</div>
<div class="arrow"><div class="next">{next_arrow}</div><div class="prev">{prev_arrow}</div></div>
</article>
<div class="ad_ads row12"><div class="grid4">
<iframe src="https://rcm-fe.amazon-adsystem.com/e/cm?o=9&p=12" width="300" height="250" scrolling="no"></iframe>
</div></div>
</body></html>
"""


def old_strip(number: str, caption: str, ext: str = "png") -> str:
    """One strip frame of an older work."""
    return f"""
<!DOCTYPE html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="twitter:image" content="http://laza.mandarake.co.jp/comic001/img/{number}.{ext}" />
<title>『ロリータばばあの言うことにゃ』 岡野く仔 | まんだらけ</title>
</head><body class="comic_inner comic001">
<article><section><div class="container"><div class="content_c">
<div class="subject"><h1>{caption}</h1></div>
<div class="img_block"><img src="img/{number}.{ext}"></div>
<div class="share"><a href="https://twitter.com/intent/tweet?text=x"><img src="basic/naka_1.png"></a></div>
</div></div></section></article>
</body></html>
"""


# The work index: newest first, updates that ended linking the product page,
# a duplicate, one from another work. `p3` and `p37` have ended.
OLD_INDEX_HTML = """
<html lang="ja"><head><meta charset="utf-8">
<title>『ロリータばばあの言うことにゃ』 岡野く仔 | まんだらけWEBコミック ラザ</title>
</head><body>
<div class="select"><ul>
<li><p>NEW</p><a href="/comic001/p48.html"><img src="nav/250.jpg"></a><p>2016/4/4</p></li>
<li><p>公開終了</p><a href="/comic001/prod.html"><img src="nav/200.jpg"></a><p>2016/2/1</p></li>
<li><p>&nbsp;</p><a href="/comic001/p36.html"><img src="nav/191.jpg"></a><p>2016/1/18</p></li>
<li><p>&nbsp;</p><a href="http://laza.mandarake.co.jp/comic001/p36.html"><img src="nav/191.jpg"></a><p>x</p></li>
<li><p>&nbsp;</p><a href="/comic002/p9.html"><img src="nav/009.jpg"></a><p>2015/9/1</p></li>
<li><p>公開終了</p><a href="/comic001/prod.html"><img src="nav/034.jpg"></a><p>2015/5/11</p></li>
<li><p>&nbsp;</p><a href="/comic001/p2.html"><img src="nav/018.jpg"></a><p>2015/5/4</p></li>
<li><p>&nbsp;</p><a href="/comic001/p1.html"><img src="nav/001.jpg"></a><p>2015/5/1</p></li>
</ul></div>
</body></html>
"""


@pytest.fixture
def page(fake_response):
    """A page response the extractor can stream, optionally one that stalls after its body."""

    def make(text=None, content=b"", status=HTTPStatus.OK, *, stall=False):
        response = fake_response(content, text=text, status_code=status)

        def iter_bytes():
            yield response.content
            if stall:
                raise ReadTimeout("Read timed out.")

        response.iter_bytes = iter_bytes
        return response

    return make


@pytest.fixture
def client(fake_session):
    def make(routes):
        session = fake_session(routes)
        return Laza(session), session

    return make


@pytest.fixture
def mt_routes(page):
    """A work of two episodes with an announcement between them, and its list."""
    return {
        "/kimono-lolita/list.html": page(MT_LIST_HTML),
        "/kimono-lolita/manga/001.html": page(mt_page("001", "001：第一話 着物ちゃん", next_name="002")),
        "/kimono-lolita/manga/002.html": page(mt_page("002", "002：第一話 着物ちゃん", next_name="003")),
        "/kimono-lolita/manga/003.html": page(mt_page("003", "003：第一話 着物ちゃん", next_name="4kan")),
        "/kimono-lolita/manga/4kan.html": page(MT_AD_HTML),
        "/kimono-lolita/manga/005.html": page(mt_page("005", "005：第二話 ロリータちゃん", next_name="006")),
        "/kimono-lolita/manga/006.html": page(mt_page("006", "006：第二話 ロリータちゃん《2》", next_name=None)),
        "/kimono-lolita/manga/": page(NOT_FOUND_HTML, status=HTTPStatus.NOT_FOUND),
    }


@pytest.fixture
def old_routes(page):
    """An older work: its index, update p36 of two strips, and the strips."""
    return {
        "/comic001/p36.html": page(old_episode(["191.html", "192.html"], prev="p35.html")),
        "/comic001/191.html": page(old_strip("191", "191 ふざけてはいない。本当の愛だ。")),
        "/comic001/192.html": page(old_strip("192", "192 天使の悪魔", ext="jpg")),
        "/comic001/": page(OLD_INDEX_HTML),
        "/comic001/list.html": page("No input file specified.", status=HTTPStatus.NOT_FOUND),
    }


# --- URLs ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        MT_EPISODE_URL,
        "https://laza.mandarake.co.jp/kimono-lolita/manga/001.html",
        "http://laza.mandarake.co.jp/BTP_plus/manga/1.html",
        "http://laza.mandarake.co.jp/kimono-lolita/manga/4kan.html",
        OLD_EPISODE_URL,
        "https://laza.mandarake.co.jp/comic004/p144.html",
        MT_LIST_URL,
        "http://laza.mandarake.co.jp/kimono-lolita/",
        "http://laza.mandarake.co.jp/kimono-lolita",
        "https://laza.mandarake.co.jp/kimono-lolita/index.html",
        OLD_INDEX_URL,
    ],
)
def test_suitable_accepts_both_generations_over_http_and_https(url):
    assert Laza.suitable(url)


@pytest.mark.parametrize(
    "url",
    [
        "ftp://laza.mandarake.co.jp/kimono-lolita/manga/001.html",
        "https://www.mandarake.co.jp/kimono-lolita/manga/001.html",
        "https://order.mandarake.co.jp/order/detailPage/item?itemCode=1196207596",
        "http://laza.mandarake.co.jp/",
        "http://laza.mandarake.co.jp/comics.html",
        "http://laza.mandarake.co.jp/kimono-lolita/item-info.html",
        "http://laza.mandarake.co.jp/comic001/prod.html",
        "http://laza.mandarake.co.jp/comic001/chara.html",
        "http://laza.mandarake.co.jp/comic001/191.html",
        "http://laza.mandarake.co.jp/kimono-lolita/manga/",
        "http://laza.mandarake.co.jp/kimono-lolita/up/2021/01/29/a.png",
    ],
)
def test_suitable_rejects_other_hosts_and_pages_without_a_comic(url):
    assert not Laza.suitable(url)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (MT_LIST_URL, True),
        (OLD_INDEX_URL, True),
        ("http://laza.mandarake.co.jp/kimono-lolita", True),
        ("http://laza.mandarake.co.jp/comic001/index.html", True),
        (MT_EPISODE_URL, False),
        (OLD_EPISODE_URL, False),
    ],
)
def test_is_series(url, expected):
    assert Laza.is_series(url) is expected


@pytest.mark.parametrize(
    ("page_title", "expected"),
    [
        ("001：第一話 着物ちゃん", "第一話 着物ちゃん"),
        ("826：負け人類に恩を売れ《1》", "負け人類に恩を売れ"),
        ("\n\t\t\t1：ふみとお兄ちゃん\n\t\t", "ふみとお兄ちゃん"),
        ("2: 見出し", "見出し"),
        ("4巻発売!画像クリックで公式通販ページに飛びます", "4巻発売!画像クリックで公式通販ページに飛びます"),
        ("", ""),
    ],
)
def test_episode_title_drops_the_page_counter_and_mark(page_title, expected):
    assert episode_title(page_title) == expected


# --- Movable Type works -----------------------------------------------------------------


def test_mt_episode_walks_the_pages_until_the_title_changes(client, mt_routes):
    laza, session = client(mt_routes)
    episode = laza.episode(MT_EPISODE_URL)

    assert episode.url == f"{HOST}/kimono-lolita/manga/001.html"
    assert episode.series_title == "着物ちゃんとロリータちゃん"
    assert (episode.writer, episode.publisher) == ("", "まんだらけ")
    assert episode.episode_title == "第一話 着物ちゃん"
    assert [page.url for page in episode.pages] == [f"{HOST}/kimono-lolita/up/2021/01/29/00{n}.png" for n in (1, 2, 3)]
    # The next episode comes from the list, skipping the announcement.
    assert episode.next_url == f"{HOST}/kimono-lolita/manga/005.html"
    assert episode.metadata["label"] == "第1話"
    assert [page["title"] for page in episode.metadata["pages"]] == [
        "001：第一話 着物ちゃん",
        "002：第一話 着物ちゃん",
        "003：第一話 着物ちゃん",
    ]
    # Everything went over https, the list was read once, the announcement was read and left out.
    assert all(url.startswith(HOST) for url in session.calls)
    assert session.calls.count(f"{HOST}/kimono-lolita/list.html") == 1
    assert f"{HOST}/kimono-lolita/manga/4kan.html" in session.calls
    assert f"{HOST}/kimono-lolita/manga/005.html" not in session.calls


def test_mt_episode_is_read_from_its_first_listed_page(client, mt_routes):
    laza, _ = client(mt_routes)
    episode = laza.episode("https://laza.mandarake.co.jp/kimono-lolita/manga/003.html")

    assert episode.url == f"{HOST}/kimono-lolita/manga/001.html"
    assert len(episode.pages) == 3


def test_mt_last_episode_has_no_next_and_ignores_the_page_mark(client, mt_routes):
    laza, _ = client(mt_routes)
    episode = laza.episode("http://laza.mandarake.co.jp/kimono-lolita/manga/006.html")

    assert episode.url == f"{HOST}/kimono-lolita/manga/005.html"
    assert episode.episode_title == "第二話 ロリータちゃん"
    assert len(episode.pages) == 2
    assert (episode.prev_url, episode.next_url) == (f"{HOST}/kimono-lolita/manga/001.html", None)


def test_mt_announcement_in_the_chain_is_an_episode_of_its_own(client, mt_routes):
    laza, _ = client(mt_routes)
    episode = laza.episode("http://laza.mandarake.co.jp/kimono-lolita/manga/4kan.html")

    assert episode.url == f"{HOST}/kimono-lolita/manga/4kan.html"
    assert episode.episode_title == "4巻発売!画像クリックで公式通販ページに飛びます"
    assert [page.url for page in episode.pages] == [f"{HOST}/kimono-lolita/up/2022/07/06/FWdbqOoagAEJjA9.jpg"]
    assert episode.next_url == f"{HOST}/kimono-lolita/manga/005.html"
    assert episode.metadata["label"] == ""


def test_mt_episode_without_a_list_stops_where_the_title_changes(client, mt_routes, page):
    mt_routes["/kimono-lolita/list.html"] = page(NOT_FOUND_HTML, status=HTTPStatus.NOT_FOUND)
    laza, _ = client(mt_routes)
    episode = laza.episode("http://laza.mandarake.co.jp/kimono-lolita/manga/002.html")

    # No list to rewind with: the given page starts the episode, and the walk
    # ends at the announcement, which is then what follows.
    assert episode.url == f"{HOST}/kimono-lolita/manga/002.html"
    assert len(episode.pages) == 2
    assert episode.next_url == f"{HOST}/kimono-lolita/manga/4kan.html"


def test_mt_episode_stops_at_a_page_without_an_image(client, mt_routes, page):
    mt_routes["/kimono-lolita/manga/003.html"] = page(MT_NO_IMAGE_HTML)
    laza, _ = client(mt_routes)
    episode = laza.episode(MT_EPISODE_URL)

    assert len(episode.pages) == 2
    assert episode.next_url == f"{HOST}/kimono-lolita/manga/005.html"


def test_mt_episode_ends_where_the_chain_breaks(client, mt_routes, page):
    mt_routes["/kimono-lolita/manga/003.html"] = page(NOT_FOUND_HTML, status=HTTPStatus.NOT_FOUND)
    mt_routes["/kimono-lolita/list.html"] = page(MT_EMPTY_LIST_HTML)
    laza, _ = client(mt_routes)
    episode = laza.episode(MT_EPISODE_URL)

    assert len(episode.pages) == 2
    assert episode.next_url is None


def test_mt_listed_page_that_is_gone_is_not_an_episode(client, mt_routes, page):
    # An episode the list already names but the site has not published yet.
    mt_routes["/kimono-lolita/list.html"] = page(MT_LIST_HTML.replace("item-info.html", "manga/918.html"))
    mt_routes["/kimono-lolita/manga/918.html"] = page(NOT_FOUND_HTML, status=HTTPStatus.NOT_FOUND)
    laza, _ = client(mt_routes)
    with pytest.raises(NotAnEpisodePageError, match=r"918\.html is gone"):
        laza.episode("http://laza.mandarake.co.jp/kimono-lolita/manga/918.html")


def test_mt_page_without_a_comic_is_not_an_episode(client, mt_routes, page):
    mt_routes["/kimono-lolita/manga/001.html"] = page(MT_NO_IMAGE_HTML)
    laza, _ = client(mt_routes)
    with pytest.raises(NotAnEpisodePageError, match="no comic"):
        laza.episode(MT_EPISODE_URL)


def test_a_page_that_is_neither_generation_is_not_an_episode(client):
    laza, session = client({})
    with pytest.raises(NotAnEpisodePageError, match="not an episode page"):
        laza.episode("http://laza.mandarake.co.jp/kimono-lolita/item-info.html")
    assert session.calls == []


@pytest.mark.parametrize(
    "url",
    [
        MT_LIST_URL,
        "http://laza.mandarake.co.jp/kimono-lolita/",
        "https://laza.mandarake.co.jp/kimono-lolita/index.html",
    ],
)
def test_mt_series_urls_come_from_the_list(client, mt_routes, url):
    laza, session = client(mt_routes)
    urls = laza.series_urls(url)

    assert urls == [f"{HOST}/kimono-lolita/manga/001.html", f"{HOST}/kimono-lolita/manga/005.html"]
    assert all(Laza.suitable(url) for url in urls)
    assert session.calls == [f"{HOST}/kimono-lolita/list.html"]


def test_mt_series_urls_read_the_list_that_stalls(client, mt_routes, page):
    mt_routes["/kimono-lolita/list.html"] = page(MT_LIST_HTML, stall=True)
    laza, _ = client(mt_routes)

    assert len(laza.series_urls(MT_LIST_URL)) == 2


def test_series_urls_of_a_list_without_an_episode(client, page):
    laza, _ = client(
        {"/kimono-lolita/list.html": page(MT_EMPTY_LIST_HTML), "/kimono-lolita/": page(MT_EMPTY_LIST_HTML)}
    )
    with pytest.raises(NotAnEpisodePageError, match="lists no episode"):
        laza.series_urls(MT_LIST_URL)


def test_series_urls_rejects_an_episode_url(client):
    laza, _ = client({})
    with pytest.raises(UnsupportedUrlError):
        laza.series_urls(MT_EPISODE_URL)


# --- older works ------------------------------------------------------------------------


def test_old_update_is_read_frame_by_frame(client, old_routes):
    laza, session = client(old_routes)
    episode = laza.episode(OLD_EPISODE_URL)

    assert episode.url == f"{HOST}/comic001/p36.html"
    assert episode.series_title == "ロリータばばあの言うことにゃ"
    assert (episode.writer, episode.publisher) == ("岡野く仔", "まんだらけ")
    assert episode.episode_title == "191 ふざけてはいない。本当の愛だ。"
    assert [page.url for page in episode.pages] == [f"{HOST}/comic001/img/191.png", f"{HOST}/comic001/img/192.jpg"]
    # The update after p36 that is still public, per the index.
    assert episode.next_url == f"{HOST}/comic001/p48.html"
    assert episode.metadata["update"] == "p36"
    assert episode.metadata["date"] == "2016/1/18"
    assert [strip["caption"] for strip in episode.metadata["strips"]] == [
        "191 ふざけてはいない。本当の愛だ。",
        "192 天使の悪魔",
    ]
    assert all(url.startswith(HOST) for url in session.calls)
    assert "amazon-adsystem" not in "".join(session.calls)


def test_old_update_skips_a_frame_that_is_gone(client, old_routes, page):
    old_routes["/comic001/191.html"] = page("No input file specified.", status=HTTPStatus.NOT_FOUND)
    laza, _ = client(old_routes)
    episode = laza.episode(OLD_EPISODE_URL)

    assert [page.url for page in episode.pages] == [f"{HOST}/comic001/img/192.jpg"]
    assert episode.episode_title == "192 天使の悪魔"


def test_old_update_falls_back_to_its_own_arrow_without_an_index(client, old_routes, page):
    old_routes["/comic001/p36.html"] = page(old_episode(["191.html"], prev="p35.html", next_page="p37.html"))
    old_routes["/comic001/"] = page(NOT_FOUND_HTML, status=HTTPStatus.NOT_FOUND)
    laza, _ = client(old_routes)
    episode = laza.episode(OLD_EPISODE_URL)

    assert (episode.prev_url, episode.next_url) == (f"{HOST}/comic001/p35.html", f"{HOST}/comic001/p37.html")
    assert episode.metadata["date"] == ""


def test_old_update_that_ended_is_not_an_episode(client, old_routes, page):
    gone = page("No input file specified.", status=HTTPStatus.NOT_FOUND)
    laza, _ = client({"/comic001/p3.html": gone, **old_routes})
    with pytest.raises(NotAnEpisodePageError, match=r"p3\.html is gone"):
        laza.episode("http://laza.mandarake.co.jp/comic001/p3.html")


def test_old_update_without_a_strip_is_not_an_episode(client, old_routes, page):
    old_routes["/comic001/p36.html"] = page(old_episode([]))
    laza, _ = client(old_routes)
    with pytest.raises(NotAnEpisodePageError, match="no comic"):
        laza.episode(OLD_EPISODE_URL)


def test_old_series_urls_come_from_the_index_when_there_is_no_list(client, old_routes):
    laza, session = client(old_routes)
    urls = laza.series_urls(OLD_INDEX_URL)

    assert urls == [f"{HOST}/comic001/p{n}.html" for n in (1, 2, 36, 48)]
    assert all(Laza.suitable(url) for url in urls)
    assert session.calls == [f"{HOST}/comic001/list.html", f"{HOST}/comic001/"]


# --- streaming ----------------------------------------------------------------------------


def test_read_body_keeps_what_arrived_before_the_stall(page):
    assert read_body(page("<html>whole page</html>", stall=True)) == b"<html>whole page</html>"


def test_read_body_raises_when_nothing_arrived(page):
    with pytest.raises(ReadTimeout):
        read_body(page("", stall=True))


# --- download ---------------------------------------------------------------------------


def test_download_saves_the_pages(client, mt_routes, fake_response, tmp_path):
    png = BytesIO()
    Image.new("RGB", (6, 9), "white").save(png, format="PNG")
    mt_routes["/kimono-lolita/up/"] = fake_response(png.getvalue(), content_type="image/png")
    laza, session = client(mt_routes)

    result = Downloader(laza, tmp_path).download(MT_EPISODE_URL)

    assert result.status == "saved"
    assert result.save_dir == tmp_path / "laza.mandarake.co.jp" / "着物ちゃんとロリータちゃん" / "第一話 着物ちゃん"
    assert sorted(path.name for path in result.save_dir.iterdir()) == ["0.jpg", "1.jpg", "2.jpg"]
    assert Image.open(result.save_dir / "0.jpg").size == (6, 9)
    # Images are asked for with the episode as Referer.
    image_headers = [headers for url, headers in zip(session.calls, session.headers_seen, strict=True) if "/up/" in url]
    assert image_headers[0]["Referer"] == f"{HOST}/kimono-lolita/manga/001.html"


# --- the real site --------------------------------------------------------------------

# One episode per known host, free to read without an account: the first
# episode of the site's longest-running work.
TEST_URLS: dict[str, str] = {
    "laza.mandarake.co.jp": "http://laza.mandarake.co.jp/kimono-lolita/manga/001.html",
}


@pytest.mark.network
@pytest.mark.parametrize("host", TEST_URLS)
def test_site_download(tmp_path, host):
    result = Downloader(Laza(), tmp_path, only_first=True).download(TEST_URLS[host])
    assert result.status == "saved"
    assert (result.save_dir / "0.jpg").stat().st_size > 0


@pytest.mark.network
def test_site_old_update_download(tmp_path):
    result = Downloader(Laza(), tmp_path, only_first=True).download("http://laza.mandarake.co.jp/comic001/p1.html")
    assert result.status == "saved"
    assert result.episode.series_title == "ロリータばばあの言うことにゃ"
    assert result.episode.next_url == "https://laza.mandarake.co.jp/comic001/p2.html"


@pytest.mark.network
def test_site_list_page_lists_episodes():
    urls = Laza().series_urls("http://laza.mandarake.co.jp/kimono-lolita/list.html")
    assert urls[:2] == [
        "https://laza.mandarake.co.jp/kimono-lolita/manga/001.html",
        "https://laza.mandarake.co.jp/kimono-lolita/manga/011.html",
    ]
    assert all(Laza.suitable(url) for url in urls)


@pytest.mark.network
def test_site_episode_is_bounded_by_the_list():
    episode = Laza().episode("http://laza.mandarake.co.jp/kimono-lolita/manga/011.html")
    assert episode.episode_title == "第二話 ロリータちゃん"
    assert len(episode.pages) == 9
    assert episode.next_url == "https://laza.mandarake.co.jp/kimono-lolita/manga/020.html"
