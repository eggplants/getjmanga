from __future__ import annotations

import json
from datetime import date
from io import BytesIO

import pytest
from PIL import Image

from getjmanga.downloader import Downloader
from getjmanga.errors import LoginError, NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractors import boost
from getjmanga.extractors.boost import BASE_URL, LICENSE_URL, LOGIN_URL, Boost
from getjmanga.viewers.publus import Pack, pages, tile_slices

SERIES_URL = f"{BASE_URL}/content/01700001"
EPISODE_URL = f"{BASE_URL}/product/01700001"
LOCKED_URL = f"{BASE_URL}/product/01700008"
CID = "mmKAG70FqV0g7seJinMQLris+JpZmpM1/AqmwPgbFhjT99rfHGwIvLC75678vnLP"
VIEWER_URL = (
    f"{BASE_URL}/viewer/viewer.html"
    "?cid=mmKAG70FqV0g7seJinMQLris%2BJpZmpM1%2FAqmwPgbFhjT99rfHGwIvLC75678vnLP&com-access-no-history"
)
CONTENT_URL = "https://cdn.comic-boost.com/contents/publus/S0170_ch_001/"
SERIES_TITLE = "ツンリゼ～ツンデレ悪役令嬢リーゼロッテと実況の遠藤くんと解説の小林さん～"

# What `configuration_pack.json` says once unwrapped, cut down to what is
# read: a shuffled page, a plain one and a non-linear one the viewer skips.
KEYS = (bytes(range(32)), bytes(range(32, 64)), bytes(range(64, 96)))
SHUFFLED_PAGE = {
    "No": 0,
    "Size": {"Width": 64, "Height": 48},
    "BlockWidth": 32,
    "BlockHeight": 32,
    "NS": 1,
    "PS": 2,
    "RS": 3,
}
PACK_JSON = {
    "configuration": {
        "page-progression-direction": "rtl",
        "contents": [
            {"file": "OEBPS/text/p-0001.xhtml", "index": 1, "type": "jpeg"},
            {"file": "OEBPS/text/p-0002.xhtml", "index": 2, "type": "jpeg"},
            {"file": "OEBPS/text/p-0003.xhtml", "index": 3, "type": "jpeg"},
        ],
    },
    "OEBPS/text/p-0001.xhtml": {
        "FileLinkInfo": {"PageCount": 1, "PageLinkInfoList": [{"Page": SHUFFLED_PAGE}]},
        "Linear": 1,
    },
    "OEBPS/text/p-0002.xhtml": {
        "FileLinkInfo": {"PageCount": 1, "PageLinkInfoList": [{"Page": {"No": 0, "Size": {"Width": 8, "Height": 8}}}]},
        "Linear": 1,
    },
    "OEBPS/text/p-0003.xhtml": {
        "FileLinkInfo": {"PageCount": 1, "PageLinkInfoList": [{"Page": SHUFFLED_PAGE}]},
        "Linear": 0,
    },
}
PACK = Pack(PACK_JSON, KEYS)

COLOPHON_HTML = f"""
<html><body><div class="colophon">
<div class="pos-center">
<a class=" primary next btn" href="/product/01700002" data-id="01700002" data-title="第2話" data-coin="0">
<span>次の話を読む</span></a>
</div>
<div class="pos-center"><a class="btn" href="/content/01700001"><span>作品詳細へ戻る</span></a></div>
<div class="js-share-btn-twitter colophon-btn-list-item twitter" data-title="{SERIES_TITLE}"
 data-title-sub="第1話" data-id="01700001" data-url="{SERIES_URL}"></div>
</div></body></html>
"""
LAST_COLOPHON_HTML = f"""
<html><body><div class="colophon">
<div class="pos-center"><a class=" btn" href="/product/01700009" data-id="01700009" data-title="第9話">
<span>前の話を読む</span></a></div>
<div class="pos-center"><a class="btn" href="/content/01700001"><span>作品詳細へ戻る</span></a></div>
<div class="js-share-btn-twitter" data-title="{SERIES_TITLE}" data-title-sub="第10話" data-id="01700010"></div>
</div></body></html>
"""
LOCKED_COLOPHON_HTML = COLOPHON_HTML.replace("第1話", "第8話").replace("01700002", "01700009").replace("第2話", "第9話")
EMPTY_COLOPHON_HTML = "<html><body><div class='colophon-outer'><div class='colophon'></div></div></body></html>"
LOCKED_PRODUCT_HTML = (
    "<html><head><script>alert('閲覧するにはログインが必要です。');history.back();</script></head></html>"
)
MISSING_PRODUCT_HTML = (
    "<html><head><script>alert('作品情報が見つかりませんでした。');history.back();</script></head></html>"
)

LICENSE = {"status": "200", "url": CONTENT_URL, "cti": f"第1話 - {SERIES_TITLE}", "lp": "", "cty": 1, "lpd": 1}


def listing_html(ids, *, last):
    items = "".join(
        f'<a id="product-{i}" class="  book-product-list-item" href="/product/{pid}" data-id="{pid}"'
        f' data-title="第{i}話"></a>'
        for i, pid in enumerate(ids, start=1)
    )
    pager = f'<li class="pagination-list-item to-next{" disabled" if last else ""}"><a href="#"></a></li>'
    return f'<html><body><div class="book-product-list">{items}</div><ul>{pager}</ul></body></html>'


CONTENT_HTML = """
<html><body>
<ul class="author-list">
  <li class="author">原作：<a href="/author/%E6%81%B5">恵ノ島すず</a></li>
  <li class="author">作画：<a href="/author/%E4%BB%8A">今中千尋</a></li>
  <li class="author">キャラクター原案：<a href="/author/%E3%81%88">えいひ</a></li>
</ul>
<div class="book-product-list">
  <a id="product-1" class="book-product-list-item" href="/product/01700001" data-id="01700001" data-title="第1話">
    <div class="right"><p class="update-date">2026/02/03</p></div></a>
  <a id="product-2" class="book-product-list-item" href="/product/01700002" data-id="01700002" data-title="第2話">
    <div class="right"><p class="update-date">2026/02/17</p></div></a>
</div>
<ul><li class="pagination-list-item to-next disabled"><a href="#"></a></li></ul>
<ul class="author-list"><li class="author"><a href="/author/x">河合朗</a></li></ul>
</body></html>
"""

LOGIN_REFUSED = """
<html><body><div class="text-warning-list">
<p class="text-warning">メールアドレスまたはパスワードが違います。(1002)</p>
</div></body></html>
"""


def jpeg_bytes(image):
    raw = BytesIO()
    image.save(raw, "JPEG", quality=100)
    return raw.getvalue()


def scramble(image, extra):
    """What the CDN serves for a page `pages()` seeded: the inverse of `descramble()`."""
    block = extra["block"]
    out = Image.new(image.mode, image.size)
    for piece in tile_slices(
        image.width, image.height, block[0], block[1], pattern=extra["pattern"], seeds=tuple(extra["seeds"])
    ):
        tile = image.crop((piece.dst_x, piece.dst_y, piece.dst_x + piece.width, piece.dst_y + piece.height))
        out.paste(tile, (piece.src_x, piece.src_y))
    return out


@pytest.fixture
def client(fake_session, fake_response, monkeypatch):
    """A `Boost` over a session scripting the site for episode 1, a locked and the last one.

    The pack comes unwrapped: the cipher is `viewers/publus.py`'s and tested there.
    """
    monkeypatch.setattr(boost, "decode_pack", lambda _text: PACK)

    def build(extra=None):
        clean = Image.new("RGB", (64, 48), (200, 30, 30))
        seeds = pages(PACK, CONTENT_URL)[0].extra
        routes = {
            "/colophon/01700001": fake_response(text=COLOPHON_HTML),
            "/colophon/01700008": fake_response(text=LOCKED_COLOPHON_HTML),
            "/colophon/01700010": fake_response(text=LAST_COLOPHON_HTML),
            "/colophon/": fake_response(text=EMPTY_COLOPHON_HTML),
            "/product/01700001": fake_response(text="<html></html>", url=VIEWER_URL),
            "/product/01700010": fake_response(text="<html></html>", url=VIEWER_URL),
            "/product/01700008": fake_response(text=LOCKED_PRODUCT_HTML),
            "/product/": fake_response(text=MISSING_PRODUCT_HTML),
            "/content/": fake_response(text=CONTENT_HTML),
            LICENSE_URL: fake_response(payload=LICENSE),
            "configuration_pack.json": fake_response(text="{}"),
            "p-0001.xhtml/": fake_response(jpeg_bytes(scramble(clean, seeds)), content_type="image/jpeg"),
            ".jpeg": fake_response(jpeg_bytes(Image.new("RGB", (8, 8), (1, 2, 3))), content_type="image/jpeg"),
        }
        routes.update(extra or {})  # an override keeps the route's place in the match order
        session = fake_session(routes)
        return Boost(session), session

    return build


# --- URLs ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        EPISODE_URL,
        "https://comic-boost.com/product/01700001/",
        "https://www.comic-boost.com/product/00290111",
        SERIES_URL,
        "https://comic-boost.com/content/00290001/",
        "https://www.comic-boost.com/content/00290001",
    ],
)
def test_suitable_accepts_episode_and_work_urls(url):
    assert Boost.suitable(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://comic-boost.com/product/01700001",
        "https://comic-boost.com/",
        "https://comic-boost.com/product/",
        "https://comic-boost.com/product/1700001",
        "https://comic-boost.com/colophon/01700001",
        "https://comic-boost.com/viewer/viewer.html?cid=abc",
        "https://comic-boost.com/genre/1",
        "https://manga-5.com/product/00850001",
    ],
)
def test_suitable_rejects_other_urls(url):
    assert not Boost.suitable(url)


@pytest.mark.parametrize(
    ("url", "expected"),
    [(EPISODE_URL, False), (SERIES_URL, True), ("https://www.comic-boost.com/content/00290001/", True)],
)
def test_is_series(url, expected):
    assert Boost.is_series(url) is expected


# --- episodes ----------------------------------------------------------------------


def test_episode_reads_the_titles_the_pages_and_the_next_episode(client):
    boost, session = client()
    episode = boost.episode(EPISODE_URL)

    assert episode.url == EPISODE_URL
    assert episode.series_title == SERIES_TITLE
    assert episode.episode_title == "第1話"
    assert (episode.prev_url, episode.next_url) == (None, f"{BASE_URL}/product/01700002")
    assert (episode.writer, episode.publisher) == (
        "恵ノ島すず (原作), 今中千尋 (作画), えいひ (キャラクター原案)",
        "幻冬舎コミックス",
    )
    assert (episode.published, episode.number) == (date(2026, 2, 3), 1)
    assert [page.url for page in episode.pages] == [
        f"{CONTENT_URL}OEBPS/text/p-0001.xhtml/0.jpeg",
        f"{CONTENT_URL}OEBPS/text/p-0002.xhtml/0.jpeg",
    ]
    assert set(episode.pages[0].extra) == {"pattern", "seeds", "block"}
    assert episode.pages[0].width == 64
    assert episode.pages[0].height == 48
    assert episode.pages[1].extra == {}
    assert episode.metadata["license"] == LICENSE
    assert episode.metadata["configuration"] == PACK_JSON["configuration"]
    assert episode.metadata["colophon"]["data-title-sub"] == "第1話"
    json.dumps(episode.metadata)

    # The colophon, the product page, the license call with its cid, then the pack.
    assert session.calls[:2] == [f"{BASE_URL}/colophon/01700001", EPISODE_URL]
    assert session.calls[2] == LICENSE_URL
    assert session.params_seen[2] == {"cid": CID}
    assert session.headers_seen[2]["Referer"] == VIEWER_URL
    assert session.calls[3] == f"{CONTENT_URL}configuration_pack.json"


def test_episode_normalises_the_host_and_the_trailing_slash(client):
    boost, session = client()
    episode = boost.episode("https://www.comic-boost.com/product/01700001/")
    assert episode.url == EPISODE_URL
    assert session.calls[1] == EPISODE_URL


def test_locked_episode_has_no_pages_but_keeps_its_titles(client):
    boost, session = client()
    episode = boost.episode(LOCKED_URL)

    assert episode.pages == ()
    assert not episode.readable
    assert episode.series_title == SERIES_TITLE
    assert episode.episode_title == "第8話"
    assert episode.next_url == f"{BASE_URL}/product/01700009"
    assert LICENSE_URL not in session.calls


def test_last_episode_has_no_next(client):
    boost, _ = client()
    episode = boost.episode(f"{BASE_URL}/product/01700010")
    assert episode.readable
    assert episode.episode_title == "第10話"
    assert (episode.prev_url, episode.next_url) == (f"{BASE_URL}/product/01700009", None)


def test_unknown_episode_is_not_an_episode_page(client):
    boost, _ = client()
    with pytest.raises(NotAnEpisodePageError, match="no episode 01709999"):
        boost.episode(f"{BASE_URL}/product/01709999")


def test_readable_episode_without_a_colophon_falls_back_to_the_license_title(client, fake_response):
    boost, _ = client({"/colophon/01700001": fake_response(text=EMPTY_COLOPHON_HTML)})
    episode = boost.episode(EPISODE_URL)
    assert episode.readable
    assert episode.series_title == SERIES_TITLE
    assert episode.episode_title == "第1話"
    assert episode.next_url is None


def test_refused_license_means_locked(client, fake_response):
    boost, _ = client({LICENSE_URL: fake_response(payload={"status": 401})})
    episode = boost.episode(EPISODE_URL)
    assert not episode.readable
    assert episode.episode_title == "第1話"
    assert episode.metadata["license"] == {"status": 401}


def test_episode_rejects_a_work_url(client):
    boost, _ = client()
    with pytest.raises(UnsupportedUrlError):
        boost.episode(SERIES_URL)


# --- series ------------------------------------------------------------------------


def test_series_urls_walks_the_pages_oldest_first(fake_session, fake_response):
    session = fake_session(
        {
            "/content/": [
                fake_response(text=listing_html(["01700001", "01700002"], last=False)),
                fake_response(text=listing_html(["01700002", "01700003"], last=True)),
            ],
        }
    )
    urls = Boost(session).series_urls("https://www.comic-boost.com/content/01700001/")
    assert urls == [f"{BASE_URL}/product/0170000{i}" for i in (1, 2, 3)]
    assert all(Boost.suitable(url) for url in urls)
    assert session.calls == [SERIES_URL, SERIES_URL]
    assert session.params_seen == [{"order": "asc", "p": 1}, {"order": "asc", "p": 2}]


def test_series_urls_stops_when_a_page_brings_nothing_new(fake_session, fake_response):
    session = fake_session({"/content/": fake_response(text=listing_html(["01700001"], last=False))})
    assert Boost(session).series_urls(SERIES_URL) == [EPISODE_URL]
    assert len(session.calls) == 2


def test_series_urls_rejects_an_episode_url(client):
    boost, _ = client()
    with pytest.raises(UnsupportedUrlError):
        boost.series_urls(EPISODE_URL)


def test_series_urls_raises_on_an_empty_listing(fake_session, fake_response):
    session = fake_session({"/content/": fake_response(text=listing_html([], last=True))})
    with pytest.raises(NotAnEpisodePageError, match="lists no episode"):
        Boost(session).series_urls(SERIES_URL)


# --- images ------------------------------------------------------------------------


def test_image_descrambles_a_page_and_leaves_a_plain_one_alone(client):
    boost, session = client()
    episode = boost.episode(EPISODE_URL)

    page = boost.image(episode.pages[0], episode)
    assert page.size == (64, 48)
    assert all(abs(a - b) < 8 for a, b in zip(page.convert("RGB").getpixel((5, 40)), (200, 30, 30), strict=True))
    assert session.headers_seen[-1]["Referer"] == EPISODE_URL

    plain = boost.image(episode.pages[1], episode)
    assert plain.size == (8, 8)
    assert all(abs(a - b) < 8 for a, b in zip(plain.convert("RGB").getpixel((0, 0)), (1, 2, 3), strict=True))


# --- logging in ----------------------------------------------------------------------


def test_login_posts_the_form_and_accepts_a_redirect_elsewhere(fake_session, fake_response):
    session = fake_session({LOGIN_URL: fake_response(text="<html><body>マイページ</body></html>", url=f"{BASE_URL}/")})
    Boost(session).login(EPISODE_URL, "someone@example.com", "hunter2")

    url, body = session.posts[0]
    assert url == LOGIN_URL
    assert body == {"account[email]": "someone@example.com", "account[password]": "hunter2"}


def test_login_raises_with_the_site_reason(fake_session, fake_response):
    session = fake_session({LOGIN_URL: fake_response(text=LOGIN_REFUSED, url=f"{LOGIN_URL}?msgid=1002")})
    with pytest.raises(LoginError, match=r"違います。\(1002\)"):
        Boost(session).login(EPISODE_URL, "someone@example.com", "wrong")


def test_login_raises_when_bounced_back_without_a_reason(fake_session, fake_response):
    session = fake_session({LOGIN_URL: fake_response(text="<html></html>", url=LOGIN_URL)})
    with pytest.raises(LoginError, match="no reason given"):
        Boost(session).login(EPISODE_URL, "someone@example.com", "wrong")


# --- the real site -------------------------------------------------------------------

# One free episode per known host: the first episode of a long-running series.
TEST_URLS: dict[str, str] = {
    "comic-boost.com": "https://comic-boost.com/product/01700001",
    "www.comic-boost.com": "https://www.comic-boost.com/product/00290001",
}


@pytest.mark.network
@pytest.mark.parametrize("host", TEST_URLS)
def test_site_download(tmp_path, host):
    result = Downloader(Boost(), tmp_path, only_first=True).download(TEST_URLS[host])
    assert result.status == "saved"
    assert (result.save_dir / "0.jpg").stat().st_size > 0


@pytest.mark.network
def test_site_locked_episode_has_no_pages():
    episode = Boost().episode(LOCKED_URL)
    assert not episode.readable
    assert episode.episode_title == "第8話"
    assert episode.next_url == f"{BASE_URL}/product/01700009"


@pytest.mark.network
def test_site_series_lists_episodes():
    urls = Boost().series_urls(SERIES_URL)
    assert urls[0] == EPISODE_URL
    assert len(urls) >= 10
    assert all(Boost.suitable(url) for url in urls)
