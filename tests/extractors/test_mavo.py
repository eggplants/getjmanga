from __future__ import annotations

from datetime import date
from io import BytesIO

import pytest
from PIL import Image

from getjmanga.downloader import Downloader
from getjmanga.errors import NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractors.mavo import Mavo

EPISODE_URL = "http://mavo.takekuma.jp/viewer.php?id=1545"
SERIES_URL = "http://mavo.takekuma.jp/title.php?title=123"

# An episode page: the work link in the heading, `h1` with the author link,
# the banner, two eager pages, lazy pages, a protector on each, and an ad.
VIEWER_HTML = """
<!doctype html><html><head><meta charset="UTF-8">
<title>電脳マヴォ：多摩美漫画文化論 優秀作品 なす（2021年後期優秀作品）/小島瑛</title>
<script>var adult = 0;</script></head><body>
<nav id="header"><div id="menu"><a href="index.php"><img src="images/header_logo1.png" alt="電脳マヴォ"></a>
<span class="hidden-xs"><strong><a href="title.php?title=123">多摩美漫画文化論 優秀作品</a></strong> &#8250;
なす（2021年後期優秀作品） / <a href="javascript:document.search.submit()">小島瑛</a></span>
<form action="search.php" method="get" name="search"><input name="keywords" type="hidden" value=" 小島瑛"></form>
</div></nav>
<div class="container" id="base"><div class="row"><div id="manga" class="">
<h1> なす（2021年後期優秀作品） / <a href="javascript:document.search.submit()">小島瑛</a></h1>
<p ></p>
<div id="banner"> <a href='title.php?title=123'>
<img src='manga/tamabi/01/banner1.png' alt='' class='img-responsive'/></a></div>
<div class='page'><img src='blank.gif' class='protector'>
<img src='manga/tamabi/01/001.jpg' class='img-responsive'/></div>
<div class='page'><img src='blank.gif' class='protector'>
<img src='manga/tamabi/01/002.jpg' class='img-responsive'/></div>
<div class='page'><img src='blank.gif' class='protector'>
<img src='blank.gif' data-original='manga/tamabi/01/003.jpg' class='lazy img-responsive' =''/></div>
<div class='page'><img src='blank.gif' class='protector'>
<img src='blank.gif' data-original='manga/tamabi/01/002.jpg' class='lazy img-responsive' =''/></div>
<div class='ad'><ins class='adsbygoogle adslot_1'></ins></div>
</div></div>
<p id="gotitle"><a href="title.php?title=123"><img src='images/backtotitle.jpg' /></a></p>
</div></body></html>
"""

# What an id that names no released episode gets: the failed query, no viewer.
ERROR_HTML = (
    "クエリの送信に失敗しました。<br />err:SELECT * FROM MANGA WHERE TITLEID= AND `RELEASE`=1 ORDER BY DATE DESC : "
)

# A viewer whose `#manga` holds only protectors.
EMPTY_VIEWER_HTML = """
<html><body><div id="menu"><strong><a href="title.php?title=123">多摩美漫画文化論 優秀作品</a></strong></div>
<div id="manga"><h1> 準備中 / <a href="#">誰か</a></h1>
<div class='page'><img src='blank.gif' class='protector'></div></div></body></html>
"""

# A work page: the title image, the listing newest first with one episode
# listed twice, a related-title link, and the weekly top 10 linking other
# works' episodes outside `#title`.
TITLE_HTML = """
<!DOCTYPE html><html><head><title>電脳マヴォ： 多摩美漫画文化論 優秀作品</title></head><body>
<div id="main-box" class="col-md-9">
<div id="logo"><h2><img src="manga/tamabi/title.jpg" width="720" height="160"
alt="多摩美漫画文化論 優秀作品" class="img-responsive center-block"/></h2></div>
<div id="title"><ul class="manga"><h2 class="newtitle">最新話はこちら！</h2>
<a href='viewer.php?id=1560'><li><img src='manga/tamabi/10/thum600.jpg' /><div class='info'>
<p class='up-date'>更新：2022-09-03</p><p class='authors'>汐浦凪乃</p>
<p class='mangatitle'>ミナモノカミ（2022前期優秀作品）</p></div></li></a>
<a href='viewer.php?id=1545'><li><img src='manga/tamabi/01/thum600.jpg' /><div class='info'>
<p class='up-date'>更新：2022-03-21</p><p class='authors'>小島瑛</p>
<p class='mangatitle'>なす（2021年後期優秀作品）</p></div></li></a>
<a href='http://mavo.takekuma.jp/viewer.php?id=1545'><li><div class='info'>
<p class='mangatitle'>なす（再掲）</p></div></li></a>
<a href='viewer.php?id=1546'><li><img src='manga/tamabi/05/thum600.jpg' /><div class='info'>
<p class='up-date'>更新：2022-03-21</p><p class='authors'>鈴木夏知花</p>
<p class='mangatitle'>終わらない縁日（2021年前期優秀作品）</p></div></li></a>
</ul></div><!-- title -->
<div id='another_title'><h2 class="sameauther"> 関連タイトル</h2><ul class="manga">
<a href='title.php?title=4' id='text'><li><img src='manga/memo/title.jpg' /><div class='info'>
<p class='mangatitle'>よりぬきたけくまメモ</p></div></li></a></ul></div>
<div class="nav-wrap" id="w-top10"><ul class="minwidth_top10">
<li><div class='rank'>1</div><a href='viewer.php?id=1578'><p class='mangatitle-rank'>夜のロボット</p></a></li>
</ul></div>
</div></body></html>
"""

# A work id that names no work: an empty body.
EMPTY_TITLE_HTML = ""


def jpeg(color: str = "white") -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (8, 12), color).save(buffer, format="JPEG")
    return buffer.getvalue()


@pytest.fixture
def client(fake_session):
    def make(routes):
        session = fake_session(routes)
        return Mavo(session), session

    return make


@pytest.fixture
def routes(fake_response):
    return {
        "viewer.php?id=1545": fake_response(text=VIEWER_HTML),
        "title.php?title=123": fake_response(text=TITLE_HTML),
    }


# --- URLs ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        EPISODE_URL,
        "https://mavo.takekuma.jp/viewer.php?id=1545",
        "http://mavo.takekuma.jp/pcviewer.php?id=1",
        "http://mavo.takekuma.jp/viewer.php?id=1&foo=bar",
        SERIES_URL,
        "https://mavo.takekuma.jp/title.php?title=1",
    ],
)
def test_suitable_accepts_episode_and_work_pages_on_both_schemes(url):
    assert Mavo.suitable(url)


@pytest.mark.parametrize(
    "url",
    [
        "ftp://mavo.takekuma.jp/viewer.php?id=1",
        "https://toko.takekuma.jp/viewer.php?mangaid=1",
        "https://www.mavo.takekuma.jp/viewer.php?id=1",
        "http://mavo.takekuma.jp/",
        "http://mavo.takekuma.jp/index.php",
        "http://mavo.takekuma.jp/all-list.php",
        "http://mavo.takekuma.jp/viewer.php",
        "http://mavo.takekuma.jp/viewer.php?id=abc",
        "http://mavo.takekuma.jp/title.php?id=1",
        "http://mavo.takekuma.jp/manga/tamabi/01/001.jpg",
        "http://mavo.takekuma.jp/ebooks/index.html",
    ],
)
def test_suitable_rejects_other_pages(url):
    assert not Mavo.suitable(url)


def test_is_series_tells_a_work_page_from_an_episode():
    assert Mavo.is_series(SERIES_URL)
    assert not Mavo.is_series(EPISODE_URL)


# --- the episode page -----------------------------------------------------------------


def test_episode_reads_the_page_and_names_the_next_from_the_work_page(client, routes):
    mavo, session = client(routes)
    episode = mavo.episode(EPISODE_URL)

    assert episode.url == EPISODE_URL
    assert episode.series_title == "多摩美漫画文化論 優秀作品"
    assert episode.episode_title == "なす（2021年後期優秀作品）"
    assert [page.url for page in episode.pages] == [
        "http://mavo.takekuma.jp/manga/tamabi/01/001.jpg",
        "http://mavo.takekuma.jp/manga/tamabi/01/002.jpg",
        "http://mavo.takekuma.jp/manga/tamabi/01/003.jpg",
    ]
    assert all(page.extra == {} for page in episode.pages)
    # The listing is newest first; the next episode is the one listed above.
    assert episode.next_url == "http://mavo.takekuma.jp/viewer.php?id=1560"
    assert episode.metadata["author"] == "小島瑛"
    assert (episode.writer, episode.publisher) == ("小島瑛", "電脳マヴォ")
    assert (episode.published, episode.number) == (date(2022, 3, 21), 2)
    assert episode.metadata["id"] == "1545"
    assert session.calls == [EPISODE_URL, "http://mavo.takekuma.jp/title.php?title=123"]
    assert session.params_seen == [None, None]


def test_episode_normalises_pcviewer_and_keeps_the_scheme_asked_for(client, routes):
    mavo, session = client(routes)
    episode = mavo.episode("https://mavo.takekuma.jp/pcviewer.php?id=1545&utm=x")

    assert episode.url == "https://mavo.takekuma.jp/viewer.php?id=1545"
    assert episode.next_url == "https://mavo.takekuma.jp/viewer.php?id=1560"
    assert session.calls[0] == "https://mavo.takekuma.jp/viewer.php?id=1545"


def test_episode_resolves_the_pages_against_the_redirected_url(client, fake_response):
    # `http://` answers with a 301 to https; the pages are relative to where it landed.
    mavo, _ = client(
        {
            "viewer.php?id=1545": fake_response(text=VIEWER_HTML, url="https://mavo.takekuma.jp/viewer.php?id=1545"),
            "title.php?title=123": fake_response(text=TITLE_HTML, url="https://mavo.takekuma.jp/title.php?title=123"),
        }
    )
    episode = mavo.episode(EPISODE_URL)

    assert episode.url == EPISODE_URL
    assert episode.pages[0].url == "https://mavo.takekuma.jp/manga/tamabi/01/001.jpg"
    assert episode.next_url == "http://mavo.takekuma.jp/viewer.php?id=1560"


def test_the_newest_episode_has_no_next(client, fake_response):
    newest = VIEWER_HTML.replace("なす（2021年後期優秀作品）", "ミナモノカミ（2022前期優秀作品）")
    mavo, _ = client(
        {
            "viewer.php?id=1560": fake_response(text=newest),
            "title.php?title=123": fake_response(text=TITLE_HTML),
        }
    )
    episode = mavo.episode("http://mavo.takekuma.jp/viewer.php?id=1560")

    assert episode.episode_title == "ミナモノカミ（2022前期優秀作品）"
    assert (episode.prev_url, episode.next_url) == ("http://mavo.takekuma.jp/viewer.php?id=1545", None)


def test_episode_without_a_work_link_still_reads(client, fake_response):
    orphan = VIEWER_HTML.replace(
        '<a href="title.php?title=123">多摩美漫画文化論 優秀作品</a>', "多摩美漫画文化論 優秀作品"
    )
    mavo, session = client({"viewer.php?id=1545": fake_response(text=orphan)})
    episode = mavo.episode(EPISODE_URL)

    assert episode.series_title == "1545"
    assert episode.episode_title == "なす（2021年後期優秀作品）"
    assert episode.next_url is None
    assert len(episode.pages) == 3
    assert session.calls == [EPISODE_URL]


def test_episode_not_listed_by_its_work_page_has_no_next(client, fake_response):
    mavo, _ = client(
        {
            "viewer.php?id=1545": fake_response(text=VIEWER_HTML),
            "title.php?title=123": fake_response(text=EMPTY_TITLE_HTML),
        }
    )
    episode = mavo.episode(EPISODE_URL)

    assert episode.next_url is None
    assert len(episode.pages) == 3


def test_episode_raises_for_an_unreleased_id_and_a_work_page(client, fake_response):
    mavo, _ = client({"viewer.php?id=300": fake_response(text=ERROR_HTML)})
    with pytest.raises(NotAnEpisodePageError, match="no viewer"):
        mavo.episode("http://mavo.takekuma.jp/viewer.php?id=300")
    mavo, _ = client({"viewer.php?id=301": fake_response(text=EMPTY_VIEWER_HTML)})
    with pytest.raises(NotAnEpisodePageError, match="no pages"):
        mavo.episode("http://mavo.takekuma.jp/viewer.php?id=301")
    with pytest.raises(NotAnEpisodePageError, match="not an episode page"):
        mavo.episode(SERIES_URL)


# --- the work page --------------------------------------------------------------------


def test_series_urls_lists_the_work_page(client, routes):
    mavo, session = client(routes)
    urls = mavo.series_urls(SERIES_URL)

    assert urls == [
        "http://mavo.takekuma.jp/viewer.php?id=1546",
        "http://mavo.takekuma.jp/viewer.php?id=1545",
        "http://mavo.takekuma.jp/viewer.php?id=1560",
    ]
    assert all(Mavo.suitable(url) for url in urls)
    assert session.calls == ["http://mavo.takekuma.jp/title.php?title=123"]


def test_series_urls_rejects_an_episode_url_and_an_empty_work(client, fake_response):
    mavo, _ = client({"title.php?title=999999": fake_response(text=EMPTY_TITLE_HTML)})
    with pytest.raises(UnsupportedUrlError):
        mavo.series_urls(EPISODE_URL)
    with pytest.raises(NotAnEpisodePageError, match="lists no episode"):
        mavo.series_urls("http://mavo.takekuma.jp/title.php?title=999999")


# --- the download ---------------------------------------------------------------------


def test_download_writes_the_pages(tmp_path, client, routes, fake_response):
    routes["/manga/tamabi/01/"] = fake_response(jpeg(), content_type="image/jpeg")
    mavo, session = client(routes)
    result = Downloader(mavo, tmp_path).download(EPISODE_URL)

    assert result.status == "saved"
    assert result.save_dir == tmp_path / "mavo.takekuma.jp" / "多摩美漫画文化論 優秀作品" / "なす（2021年後期優秀作品）"
    assert sorted(path.name for path in result.save_dir.iterdir()) == ["0.jpg", "1.jpg", "2.jpg"]
    assert Image.open(result.save_dir / "0.jpg").size == (8, 12)
    # The images are fetched with the episode as Referer, as the default does.
    assert session.headers_seen[-1]["Referer"] == EPISODE_URL


# --- the real site --------------------------------------------------------------------

# One episode per known host, free to read without an account: the first
# episode of the site's first serial.
TEST_URLS: dict[str, str] = {
    "mavo.takekuma.jp": "http://mavo.takekuma.jp/viewer.php?id=1",
}


@pytest.mark.network
@pytest.mark.parametrize("host", TEST_URLS)
def test_site_download(tmp_path, host):
    result = Downloader(Mavo(), tmp_path, only_first=True).download(TEST_URLS[host])
    assert result.status == "saved"
    assert (result.save_dir / "0.jpg").stat().st_size > 0


@pytest.mark.network
def test_work_page_lists_episodes():
    urls = Mavo().series_urls("https://mavo.takekuma.jp/title.php?title=1")
    assert urls[0] == "https://mavo.takekuma.jp/viewer.php?id=1"
    assert "https://mavo.takekuma.jp/viewer.php?id=7" in urls
    assert all(Mavo.suitable(url) for url in urls)


@pytest.mark.network
def test_episode_is_named_and_walked():
    episode = Mavo().episode("http://mavo.takekuma.jp/viewer.php?id=1")
    assert episode.series_title == "少女地獄"
    assert episode.episode_title == "第1話「なんでもない」前編"
    assert episode.next_url == "http://mavo.takekuma.jp/viewer.php?id=7"
    assert len(episode.pages) > 1
