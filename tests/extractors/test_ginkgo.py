from __future__ import annotations

from http import HTTPStatus
from io import BytesIO

import pytest
from httpx import HTTPStatusError
from PIL import Image

from getjmanga.downloader import Downloader
from getjmanga.errors import NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractors.ginkgo import (
    IWATE_SERIES_TITLE,
    Ginkgo,
    episode_key,
)

IWATE_URL = "http://comiciwate.jp/comic/tsuchinokioku/"
IWATE_FOREIGN_URL = "http://comiciwate.jp/foreign/haruninattara_en/"
GAI_INDEX_URL = "http://www.manga-gai.net/manga/zuttari/zuttari_index/zuttari_index.html"
GAI_EPISODE_URL = "http://www.manga-gai.net/manga/zuttari/735/01.html"

# A コミックいわてWEB work page: the artist's profile (with its own images) in
# the first row, then the pages two to a row, then the feedback form iframe.
IWATE_HTML = """
<html><head><title>土の記憶 | コミックいわてWEB</title></head><body class="comic">
<div id="contents" class="clearfix">
<ul>
<li class="right top">
<div id="profile"><div class="profile_inner">
<div class="name"><p><img src="../images/profile_title.gif" width="66" height="15" /></p><p>蓮まこと</p></div>
<div class="photo"><img src="images/profile_ph.gif" width="144" height="144" /></div>
<div class="comment"><p>岩手県出身・在住。</p></div>
</div></div>
</li>
<li class="left top"><img src="images/01.jpg" width="564" height="800" /></li>
</ul>
<ul>
<li class="right"><img src="images/02.jpg" width="564" height="800" border="0" usemap="#Map" /></li>
<li class="left"><a href="https://example.com/" target="_blank">
<img src="images/03.jpg" width="564" height="800"　border="0" /></a></li>
</ul>
<ul>
<li class="right"><img src="images/04.png" width="564" height="800" />
<a href="/"><img src="../images/btn_link.gif" /></a></li>
<li class="left"><img src="images/02.jpg" width="564" height="800" /></li>
</ul>
<ul>
<li class="right"><iframe class="autoHeight" src="../form/index.php" width="536" height="805"></iframe></li>
</ul>
</div>
</body></html>
"""

# A translation: the pages carry the slug in their names, the title is translated.
IWATE_FOREIGN_HTML = """
<html><head><title>When It Becomes Spring | コミックいわてWEB</title></head><body>
<div id="contents">
<ul><li class="left top"><img src="images/haruninattara_en_01.jpg" width="564" height="800" /></li></ul>
<ul><li class="right"><img src="images/haruninattara_en_02.jpg" width="564" height="800" /></li></ul>
</div>
</body></html>
"""

IWATE_EMPTY_HTML = """
<html><head><title>準備中 | コミックいわてWEB</title></head><body>
<div id="contents"><ul><li class="right top"><div id="profile"><img src="images/profile_ph.gif" /></div></li></ul></div>
</body></html>
"""

NOT_FOUND_HTML = "<html><head><title>404 Not Found</title></head><body><h1>Not Found</h1></body></html>"


def gai_page(number: int, *, last: bool = False) -> str:
    """One page of a 漫画街 episode, as the site writes it."""
    nav = f'<a href="{number + 1:02d}.html">＜NEXT</a>' if not last else ""
    if number > 1:
        nav += f'　<a href="{number - 1:02d}.html">BACK＞</a>'
    return f"""
<html><head><meta http-equiv="Content-Type" content="text/html; charset=UTF-8" />
<title>::::::Manga-Website 漫画街::::::</title></head><body>
<div id="contents_menuWrap" class="sp-hide">
<br /><a href="../zuttari_index/zuttari_index.html">
<img src="../zuttari_index/zuttari_title.jpg" width="200" height="163" border="0" title="ずったり岩手"></a>
<a href="../../list.html"><img src="../../../images/bnr_comic_list_200x95.gif" alt="連載中作品リスト" /></a>
</div>
<div class="sp-header detail-page cf">
  <div class="nav-to-list"><a href="../../zuttari/zuttari_index/zuttari_index.html">バックナンバーはこちら!</a></div>
</div>
<div id="originalmanga">
<table cellspacing="0" cellpadding="0" class="mangatbl">
<tr><td><a href="{number + 1:02d}.html"><img src="{number:02d}.jpg" width="564"  alt="漫画" /></a></td></tr>
<tr class="sp-hide"><td height="50" valign="top" align="center">{nav}</td></tr>
</table>
</div>
</body></html>
"""


# The page after the last image: the feedback form, served as Shift_JIS.
GAI_FORM_HTML = """
<html><head><meta http-equiv="Content-Type" content="text/html; charset=Shift_JIS" /></head><body>
<div id="contents_menuWrap"><a href="../zuttari_index/zuttari_index.html">
<img src="../zuttari_index/zuttari_title.jpg" title="ずったり岩手"></a></div>
<div id="originalmanga">
<table><tr><td width="560"><h4>この作品の感想をお書き下さい。</h4>
<form action="http://www.manga-gai.net/cgi-bin/wwwmail_k.cgi" method="post">
<input name="title" type="text" id="title" value="ずったり岩手　第735話" size="35" />
</form></td></tr>
<tr><td height="50" valign="top" align="center">　<a href="03.html">BACK＞</a></td></tr></table>
</div>
</body></html>
""".encode("shift_jis")

# A work index: a `<select>` listing newest first, a special in a block of
# its own, one episode listed twice, a link into another work and a stray
# image link, all in one select as the site does it.
GAI_INDEX_HTML = """
<html><head><meta http-equiv="Content-Type" content="text/html; charset=UTF-8" />
<title>::::::Manga-Website 漫画街::::::</title></head><body>
<div id="zuttari_sideWrap">
<FORM NAME="form1">
<select name="select1" size="10"
 onchange="if(document.form1.select1.value){location.href=document.form1.select1.value;}">
<option value="">▼▼▼下から選択してください▼▼▼</option>
<option value="http://www.manga-gai.net/manga/zuttari/736/01.html">第736話　初プリマ　の巻</option>
<option value="http://www.manga-gai.net/manga/zuttari/735/01.html">第735話　昔あそび　の巻</option>
<option value="http://www.manga-gai.net/manga/zuttari/735/01.html">第735話　昔あそび　の巻（再掲）</option>
<option value="../734/01.html">第734話　ばばデビュー　の巻</option>
<option value="http://www.manga-gai.net/manga/zuttari/02/01.html">第2話　重っこ料理　の巻</option>
<option value="http://www.manga-gai.net/manga/zuttari/01/01.html">第1話　そのだつくしでございます　の巻</option>
<option value="http://www.manga-gai.net/manga/manganomanga2/01/01.html">別の作品</option>
<option value="http://www.manga-gai.net/images/sumo.jpg">画像</option>
<option value="">▼▼▼下から選択してください▼▼▼</option>
<option value="http://www.manga-gai.net/manga/zuttari/734.5/01.html">番外編　の巻</option>
</select>
</FORM>
</div>
</body></html>
"""

GAI_EMPTY_INDEX_HTML = """
<html><head><title>::::::Manga-Website 漫画街::::::</title></head><body>
<div id="originalmanga"><table><tr>
<td><a href="https://www.cmoa.jp/title/221936/"><img src="25.jpg" /></a></td></tr></table></div>
</body></html>
"""


@pytest.fixture
def client(fake_session):
    def make(routes):
        session = fake_session(routes)
        return Ginkgo(session), session

    return make


@pytest.fixture
def gai_routes(fake_response):
    """Episode 735: three pages, the form, and the work index."""
    return {
        "/zuttari/735/01.html": fake_response(text=gai_page(1)),
        "/zuttari/735/02.html": fake_response(text=gai_page(2)),
        "/zuttari/735/03.html": fake_response(text=gai_page(3)),
        "/zuttari/735/04.html": fake_response(GAI_FORM_HTML),
        "zuttari_index.html": fake_response(text=GAI_INDEX_HTML),
    }


# --- URLs ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        IWATE_URL,
        "http://comiciwate.jp/comic/tsuchinokioku",
        "https://comiciwate.jp/comic/tsuchinokioku/",
        "http://www.comiciwate.jp/comic/iwate_special/",
        IWATE_FOREIGN_URL,
        GAI_EPISODE_URL,
        "http://www.manga-gai.net/manga/zuttari/599.5/05.html",
        "http://manga-gai.net/manga/keisandoriru/jk68/01.html",
        GAI_INDEX_URL,
        "http://www.manga-gai.net/manga/keisandoriru/_keisan_index/keisan_index.html",
        "http://www.manga-gai.net/manga/oyaota/oyaota_index/oyaotaindex.html",
    ],
)
def test_suitable_accepts_both_sites_over_http(url):
    assert Ginkgo.suitable(url)


@pytest.mark.parametrize(
    "url",
    [
        "ftp://comiciwate.jp/comic/tsuchinokioku/",
        "http://comiciwate.jp/",
        "http://comiciwate.jp/comic/",
        "http://comiciwate.jp/comic/area/",
        "http://comiciwate.jp/comic/artist/",
        "http://comiciwate.jp/comic/tsuchinokioku/images/01.jpg",
        "http://comiciwate.jp/map/",
        "http://www.manga-gai.net/manga/list.html",
        "http://www.manga-gai.net/manga/manga0203.htm",
        "http://www.manga-gai.net/manga/zuttari/735/",
        "http://www.manga-gai.net/manga/zuttari/omikuzi/index.html",
        "http://www.manga-gai.net/mangakasoudan/00/01.html",
        "http://example.com/comic/tsuchinokioku/",
        "http://example.com/manga/zuttari/735/01.html",
    ],
)
def test_suitable_rejects_other_pages(url):
    assert not Ginkgo.suitable(url)


def test_is_series_only_for_a_work_index():
    assert Ginkgo.is_series(GAI_INDEX_URL)
    assert Ginkgo.is_series("http://www.manga-gai.net/manga/keisandoriru/_keisan_index/keisan_index.html")
    assert not Ginkgo.is_series(GAI_EPISODE_URL)
    assert not Ginkgo.is_series(IWATE_URL)


@pytest.mark.parametrize(
    ("names", "expected"),
    [
        (["736", "01", "599.5", "02", "600", "599"], ["01", "02", "599", "599.5", "600", "736"]),
        (["010", "009", "00", "001"], ["00", "001", "009", "010"]),
        (["jk68", "jk00", "12.5", "01", "jk01kan", "jk01"], ["01", "12.5", "jk00", "jk01", "jk01kan", "jk68"]),
        (["s", "s6", "01"], ["01", "s6", "s"]),
    ],
)
def test_episode_key_orders_directories_by_number(names, expected):
    assert sorted(names, key=episode_key) == expected


# --- コミックいわてWEB ------------------------------------------------------------------


def test_iwate_episode_is_one_of_the_anthology(client, fake_response):
    ginkgo, session = client({"/comic/tsuchinokioku/": fake_response(text=IWATE_HTML)})
    episode = ginkgo.episode("http://comiciwate.jp/comic/tsuchinokioku")

    # The missing slash is added rather than left to the site's redirect.
    assert session.calls == [IWATE_URL]
    assert episode.url == IWATE_URL
    assert episode.series_title == IWATE_SERIES_TITLE
    assert episode.episode_title == "土の記憶"
    assert [page.url for page in episode.pages] == [
        "http://comiciwate.jp/comic/tsuchinokioku/images/01.jpg",
        "http://comiciwate.jp/comic/tsuchinokioku/images/02.jpg",
        "http://comiciwate.jp/comic/tsuchinokioku/images/03.jpg",
        "http://comiciwate.jp/comic/tsuchinokioku/images/04.png",
    ]
    assert episode.next_url is None
    assert episode.metadata["author"] == "蓮まこと"
    assert episode.metadata["site"] == "comiciwate"
    assert session.headers_seen[-1]["User-Agent"].startswith("Mozilla/5.0")


def test_iwate_translation_is_titled_with_its_language(client, fake_response):
    ginkgo, _ = client({"/foreign/haruninattara_en/": fake_response(text=IWATE_FOREIGN_HTML)})
    episode = ginkgo.episode(IWATE_FOREIGN_URL)

    assert episode.episode_title == "When It Becomes Spring (en)"
    assert [page.url for page in episode.pages] == [
        "http://comiciwate.jp/foreign/haruninattara_en/images/haruninattara_en_01.jpg",
        "http://comiciwate.jp/foreign/haruninattara_en/images/haruninattara_en_02.jpg",
    ]


def test_iwate_keeps_https_when_given(client, fake_response):
    ginkgo, session = client({"/comic/tsuchinokioku/": fake_response(text=IWATE_HTML)})
    episode = ginkgo.episode("https://comiciwate.jp/comic/tsuchinokioku/")

    assert session.calls == ["https://comiciwate.jp/comic/tsuchinokioku/"]
    assert episode.pages[0].url == "https://comiciwate.jp/comic/tsuchinokioku/images/01.jpg"


def test_iwate_page_without_a_comic_is_not_an_episode(client, fake_response):
    ginkgo, _ = client({"/comic/junbichu/": fake_response(text=IWATE_EMPTY_HTML)})
    with pytest.raises(NotAnEpisodePageError, match="no comic"):
        ginkgo.episode("http://comiciwate.jp/comic/junbichu/")


def test_iwate_missing_work_is_not_an_episode(client, fake_response):
    ginkgo, _ = client({"/comic/gone/": fake_response(text=NOT_FOUND_HTML, status_code=HTTPStatus.NOT_FOUND)})
    with pytest.raises(NotAnEpisodePageError, match="404"):
        ginkgo.episode("http://comiciwate.jp/comic/gone/")


def test_iwate_has_no_series_to_list(client):
    ginkgo, _ = client({})
    with pytest.raises(UnsupportedUrlError):
        ginkgo.series_urls(IWATE_URL)


# --- 漫画街: pages ------------------------------------------------------------------------


def test_gai_episode_walks_the_pages_and_names_itself_from_the_index(client, gai_routes):
    ginkgo, session = client(gai_routes)
    episode = ginkgo.episode(GAI_EPISODE_URL)

    assert episode.url == GAI_EPISODE_URL
    assert episode.series_title == "ずったり岩手"
    assert episode.episode_title == "第735話　昔あそび　の巻"
    assert [page.url for page in episode.pages] == [
        "http://www.manga-gai.net/manga/zuttari/735/01.jpg",
        "http://www.manga-gai.net/manga/zuttari/735/02.jpg",
        "http://www.manga-gai.net/manga/zuttari/735/03.jpg",
    ]
    assert episode.next_url == "http://www.manga-gai.net/manga/zuttari/736/01.html"
    assert episode.metadata["pages"] == [
        "http://www.manga-gai.net/manga/zuttari/735/01.html",
        "http://www.manga-gai.net/manga/zuttari/735/02.html",
        "http://www.manga-gai.net/manga/zuttari/735/03.html",
    ]
    # Four pages walked, then the index once.
    assert session.calls == [
        "http://www.manga-gai.net/manga/zuttari/735/01.html",
        "http://www.manga-gai.net/manga/zuttari/735/02.html",
        "http://www.manga-gai.net/manga/zuttari/735/03.html",
        "http://www.manga-gai.net/manga/zuttari/735/04.html",
        GAI_INDEX_URL,
    ]


def test_gai_episode_starts_from_the_first_page_whichever_is_given(client, gai_routes):
    ginkgo, session = client(gai_routes)
    episode = ginkgo.episode("http://www.manga-gai.net/manga/zuttari/735/03.html")

    assert episode.url == GAI_EPISODE_URL
    assert len(episode.pages) == 3
    assert session.calls[0] == GAI_EPISODE_URL


def test_gai_index_is_read_once_per_work(client, gai_routes, fake_response):
    gai_routes.update(
        {
            "/zuttari/736/01.html": fake_response(text=gai_page(1, last=True)),
        },
    )
    ginkgo, session = client(gai_routes)
    ginkgo.episode(GAI_EPISODE_URL)
    latest = ginkgo.episode("http://www.manga-gai.net/manga/zuttari/736/01.html")

    assert latest.episode_title == "第736話　初プリマ　の巻"
    assert latest.next_url is None
    assert session.calls.count(GAI_INDEX_URL) == 1


def test_gai_episode_stops_at_a_page_that_is_gone(client, fake_response):
    ginkgo, _ = client(
        {
            "/zuttari/735/01.html": fake_response(text=gai_page(1)),
            "/zuttari/735/02.html": fake_response(text=NOT_FOUND_HTML, status_code=HTTPStatus.NOT_FOUND),
            "zuttari_index.html": fake_response(text=GAI_INDEX_HTML),
        },
    )
    episode = ginkgo.episode(GAI_EPISODE_URL)
    assert [page.url for page in episode.pages] == ["http://www.manga-gai.net/manga/zuttari/735/01.jpg"]


def test_gai_episode_without_an_index_falls_back_to_the_path(client, fake_response):
    lone = gai_page(1, last=True).replace('title="ずったり岩手"', "").replace("nav-to-list", "nav")
    ginkgo, session = client({"/manganomanga1/00/01.html": fake_response(text=lone)})
    episode = ginkgo.episode("http://www.manga-gai.net/manga/manganomanga1/00/01.html")

    assert episode.series_title == "manganomanga1"
    assert episode.episode_title == "00"
    assert episode.next_url is None
    assert len(session.calls) == 1


def test_gai_episode_survives_an_index_that_is_gone(client, fake_response):
    ginkgo, _ = client(
        {
            "/zuttari/735/01.html": fake_response(text=gai_page(1, last=True)),
            "zuttari_index.html": fake_response(text=NOT_FOUND_HTML, status_code=HTTPStatus.NOT_FOUND),
        },
    )
    episode = ginkgo.episode(GAI_EPISODE_URL)
    assert episode.episode_title == "735"
    assert len(episode.pages) == 1


def test_gai_missing_episode_is_not_an_episode(client, fake_response):
    ginkgo, _ = client({"/zuttari/9999/": fake_response(text=NOT_FOUND_HTML, status_code=HTTPStatus.NOT_FOUND)})
    with pytest.raises(NotAnEpisodePageError, match="404"):
        ginkgo.episode("http://www.manga-gai.net/manga/zuttari/9999/01.html")


def test_gai_first_page_without_an_image_is_not_an_episode(client, fake_response):
    ginkgo, _ = client({"/zuttari/735/01.html": fake_response(GAI_FORM_HTML)})
    with pytest.raises(NotAnEpisodePageError, match="no comic"):
        ginkgo.episode(GAI_EPISODE_URL)


def test_other_errors_still_raise(client, fake_response):
    ginkgo, _ = client({"/zuttari/735/01.html": fake_response(text="", status_code=HTTPStatus.SERVICE_UNAVAILABLE)})
    with pytest.raises(HTTPStatusError):
        ginkgo.episode(GAI_EPISODE_URL)


def test_episode_rejects_a_url_of_neither_shape(client):
    ginkgo, session = client({})
    with pytest.raises(NotAnEpisodePageError):
        ginkgo.episode("http://www.manga-gai.net/manga/list.html")
    assert session.calls == []


# --- 漫画街: the index --------------------------------------------------------------------


def test_series_urls_lists_oldest_first(client, fake_response):
    ginkgo, _ = client({"zuttari_index.html": fake_response(text=GAI_INDEX_HTML)})
    urls = ginkgo.series_urls(GAI_INDEX_URL)

    assert urls == [
        "http://www.manga-gai.net/manga/zuttari/01/01.html",
        "http://www.manga-gai.net/manga/zuttari/02/01.html",
        "http://www.manga-gai.net/manga/zuttari/734/01.html",
        "http://www.manga-gai.net/manga/zuttari/734.5/01.html",
        "http://www.manga-gai.net/manga/zuttari/735/01.html",
        "http://www.manga-gai.net/manga/zuttari/736/01.html",
    ]
    assert all(Ginkgo.suitable(url) for url in urls)


def test_series_urls_rejects_an_episode(client):
    ginkgo, _ = client({})
    with pytest.raises(UnsupportedUrlError):
        ginkgo.series_urls(GAI_EPISODE_URL)


def test_series_urls_with_nothing_listed(client, fake_response):
    ginkgo, _ = client({"1930_index.html": fake_response(text=GAI_EMPTY_INDEX_HTML)})
    with pytest.raises(NotAnEpisodePageError, match="lists no episode"):
        ginkgo.series_urls("http://www.manga-gai.net/manga/1930/1930_index/1930_index.html")


def test_series_urls_of_a_missing_index(client, fake_response):
    ginkgo, _ = client({"gone_index.html": fake_response(text=NOT_FOUND_HTML, status_code=HTTPStatus.NOT_FOUND)})
    with pytest.raises(NotAnEpisodePageError, match="lists no episode"):
        ginkgo.series_urls("http://www.manga-gai.net/manga/gone/gone_index/gone_index.html")


# --- downloading ----------------------------------------------------------------------


def _jpeg(color):
    raw = BytesIO()
    Image.new("RGB", (6, 8), color).save(raw, "JPEG", quality=100)
    return raw.getvalue()


def test_download_writes_a_gai_episode(client, gai_routes, fake_response, tmp_path):
    gai_routes.update(
        {
            "/zuttari/735/01.jpg": fake_response(_jpeg((200, 200, 200)), content_type="image/jpeg"),
            "/zuttari/735/02.jpg": fake_response(_jpeg((100, 100, 100)), content_type="image/jpeg"),
            "/zuttari/735/03.jpg": fake_response(_jpeg((50, 50, 50)), content_type="image/jpeg"),
        },
    )
    ginkgo, session = client(gai_routes)
    result = Downloader(ginkgo, tmp_path).download(GAI_EPISODE_URL)

    assert result.status == "saved"
    assert result.save_dir == tmp_path / "ずったり岩手" / "第735話　昔あそび　の巻"
    assert sorted(path.name for path in result.save_dir.iterdir()) == ["0.jpg", "1.jpg", "2.jpg"]
    assert Image.open(result.save_dir / "2.jpg").getpixel((3, 4)) == (50, 50, 50)
    assert session.headers_seen[-1]["Referer"] == GAI_EPISODE_URL


# --- logging in -----------------------------------------------------------------------


# --- the real site --------------------------------------------------------------------

# One episode per known host, free to read without an account: a one-shot
# of コミックいわてWEB and the first episode of 漫画街's longest-running serial.
TEST_URLS: dict[str, str] = {
    "comiciwate.jp": "http://comiciwate.jp/comic/tsuchinokioku/",
    "www.comiciwate.jp": "http://www.comiciwate.jp/comic/agyon/",
    "manga-gai.net": "http://manga-gai.net/manga/zuttari/01/01.html",
    "www.manga-gai.net": "http://www.manga-gai.net/manga/hatori/01/01.html",
}


@pytest.mark.network
@pytest.mark.parametrize("host", TEST_URLS)
def test_site_download(tmp_path, host):
    result = Downloader(Ginkgo(), tmp_path, only_first=True).download(TEST_URLS[host])
    assert result.status == "saved"
    assert (result.save_dir / "0.jpg").stat().st_size > 0


@pytest.mark.network
def test_index_page_lists_episodes():
    urls = Ginkgo().series_urls(GAI_INDEX_URL)
    assert urls[0] == "http://www.manga-gai.net/manga/zuttari/01/01.html"
    assert "http://www.manga-gai.net/manga/zuttari/599.5/01.html" in urls
    assert all(Ginkgo.suitable(url) for url in urls)


@pytest.mark.network
def test_gai_episode_is_named_and_walked():
    episode = Ginkgo().episode("http://www.manga-gai.net/manga/hatori/03/01.html")
    assert episode.series_title == "羽鳥くんはみんのソレを知っている"
    assert episode.episode_title == "第3話 花を編む"
    assert episode.next_url == "http://www.manga-gai.net/manga/hatori/04/01.html"
    assert len(episode.pages) > 1
