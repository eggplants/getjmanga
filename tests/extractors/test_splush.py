from __future__ import annotations

from http import HTTPStatus
from io import BytesIO

import pytest
from httpx import HTTPStatusError
from PIL import Image

from getjmanga.downloader import Downloader
from getjmanga.errors import NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractors.splush import Splush

WORK_URL = "https://www.splush.jp/series/14712/"
EPISODE_URL = "https://www.splush.jp/series/14716/"
NEXT_URL = "https://www.splush.jp/series/14734/"
EXPIRED_URL = "https://www.splush.jp/series/11306/"

# The parts of an episode page `episode()` reads, as the site writes them: a
# series heading, the navigation with the episode heading and the pager, one
# `div.comicImg` per page and the link back to the work page.
EPISODE_HTML = f"""
<html><head><title>そんなのズルいよ蒼汰くん 第一話（前編）｜Splush</title></head><body>
<div id="series"><article class="greyBox">
<header class="articleHead">
  <p class="iconLabel">無料連載</p>
  <h3 class="title">そんなのズルいよ蒼汰くん</h3>
  <p class="author">まめなえ</p>
</header>
<div class="navi">
  <h4 class="seriesNum">第一話（前編）</h4>
  <ul class="seriesPager"><li class="next"><a href="{NEXT_URL}">次の話へ</a></li></ul>
</div>
<section class="comicArea">
  <div class="comicImg"><img src="https://www.splush.jp/wp-content/uploads/2025/12/souta01_01tobira.jpg" alt=""/></div>
  <div class="comicImg"><img src="/wp-content/uploads/2025/12/souta01_03.jpg" alt=""/></div>
  <div class="comicImg"><img src="https://www.splush.jp/wp-content/uploads/2025/12/souta01_04.jpg" alt=""/></div>
</section>
<div class="navi">
  <ul class="seriesPager"><li class="next"><a href="{NEXT_URL}">次の話へ</a></li></ul>
  <p class="back"><a href="{WORK_URL}">作品トップへ</a></p>
</div>
</article></div>
</body></html>
"""

# The last episode of a work: a `prev` link but no `next`.
LAST_EPISODE_HTML = """
<html><head><title>そんなのズルいよ蒼汰くん 第五話（後編）｜Splush</title></head><body>
<h3 class="title">そんなのズルいよ蒼汰くん</h3>
<div class="navi">
  <h4 class="seriesNum">第五話（後編）</h4>
  <ul class="seriesPager"><li class="prev"><a href="https://www.splush.jp/series/17008/">前の話へ</a></li></ul>
</div>
<section class="comicArea">
  <div class="comicImg"><img src="https://www.splush.jp/wp-content/uploads/2026/08/sonnano52_01.jpg" alt=""/></div>
</section>
<div class="navi"><p class="back"><a href="https://www.splush.jp/series/14712/">作品トップへ</a></p></div>
</body></html>
"""

# An episode whose free run has ended: the notice stands where the pages were,
# the pager still names the neighbours.
EXPIRED_HTML = """
<html><head><title>B組の名物カップルはまだ付き合ってない 第三話（前編）｜Splush</title></head><body>
<h3 class="title">B組の名物カップルはまだ付き合ってない</h3>
<div class="navi">
  <h4 class="seriesNum">第三話（前編）</h4>
  <ul class="seriesPager">
    <li class="prev"><a href="https://www.splush.jp/series/10826/">前の話へ</a></li>
    <li class="next"><a href="https://www.splush.jp/series/11322/">次の話へ</a></li>
  </ul>
</div>
<p class="notice">公開終了しました。</p>
<div class="navi"><p class="back"><a href="https://www.splush.jp/series/10682/">作品トップへ</a></p></div>
</body></html>
"""

# A work page: the newest episode repeated in `section.latest`, then the
# lineup newest first, one of them linked twice.
WORK_HTML = """
<html><head><title>そんなのズルいよ蒼汰くん｜Splush</title></head><body>
<header class="articleHead">
  <h3 class="title">そんなのズルいよ蒼汰くん</h3>
  <p class="author">まめなえ</p>
</header>
<section class="latest">
  <a href="https://www.splush.jp/series/14734/"><article class="article">
    <p class="iconLatest"><span>最新話</span></p><h4 class="seriesNum">第一話（後編）</h4>
  </article></a>
</section>
<section class="lineup">
<h2 class="secTtl">作品を読む</h2>
<div class="lineupWrap">
  <a href="https://www.splush.jp/series/14734/"><article class="article">
    <p class="update">2026/01/02 更新</p><h4 class="seriesNum">第一話（後編）</h4>
  </article></a>
  <a href="/series/14734/#again"><article class="article"><h4 class="seriesNum">第一話（後編）</h4></article></a>
  <a href="https://www.splush.jp/series/14716/"><article class="article">
    <p class="update">2025/12/19 更新</p><h4 class="seriesNum">第一話（前編）</h4>
  </article></a>
  <a href="https://www.splush.jp/books/9784781625966/"><article class="article">a book, not an episode</article></a>
</div>
</section>
</body></html>
"""

EMPTY_WORK_HTML = """
<html><body><h3 class="title">まだ始まらない</h3>
<section class="lineup"><h2 class="secTtl">作品を読む</h2><div class="lineupWrap"></div></section>
</body></html>
"""

# What `/series/<id>/` shows for an id that is an image attachment: neither kind of page.
ATTACHMENT_HTML = """
<html><head><title>Splush｜イーストプレス</title></head><body>
<div id="mainCol"><img src="https://www.splush.jp/wp-content/uploads/2025/12/sonnano_main.jpg"/></div>
</body></html>
"""


@pytest.fixture
def client(fake_session):
    def make(routes):
        session = fake_session(routes)
        return Splush(session), session

    return make


# --- URLs ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        WORK_URL,
        EPISODE_URL,
        "https://www.splush.jp/series/14716",
        "https://splush.jp/series/14716/",
    ],
)
def test_suitable_accepts_series_urls(url):
    assert Splush.suitable(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://www.splush.jp/series/14716/",
        "https://www.splush.jp/",
        "https://www.splush.jp/series/",
        "https://www.splush.jp/series/14712/attachment/sonnano_main/",
        "https://www.splush.jp/books/9784781625966/",
        "https://www.splush.jp/series-backnumber/",
        "https://comic-porta.com/series/7981/",
    ],
)
def test_suitable_rejects_other_urls(url):
    assert not Splush.suitable(url)


# --- parsing --------------------------------------------------------------------------


# --- is_series and the cache ----------------------------------------------------------


def test_is_series_fetches_the_page_once_and_episode_reuses_it(client, fake_response):
    splush, session = client({"/series/14716/": fake_response(text=EPISODE_HTML)})

    assert not splush.is_series(EPISODE_URL)
    assert not splush.is_series("https://www.splush.jp/series/14716")
    episode = splush.episode(EPISODE_URL)

    assert episode.episode_title == "第一話（前編）"
    assert session.calls == [EPISODE_URL]


def test_is_series_is_true_for_a_work_page_and_series_urls_reuses_it(client, fake_response):
    splush, session = client({"/series/14712/": fake_response(text=WORK_HTML)})

    assert splush.is_series(WORK_URL)
    assert splush.series_urls(WORK_URL) == [EPISODE_URL, NEXT_URL]
    assert session.calls == [WORK_URL]


# --- episode --------------------------------------------------------------------------


def test_episode_reads_the_titles_the_pages_and_the_next_episode(client, fake_response):
    splush, _ = client({"/series/14716/": fake_response(text=EPISODE_HTML)})
    episode = splush.episode(EPISODE_URL)

    assert episode.url == EPISODE_URL
    assert episode.series_title == "そんなのズルいよ蒼汰くん"
    assert episode.episode_title == "第一話（前編）"
    assert [page.url for page in episode.pages] == [
        "https://www.splush.jp/wp-content/uploads/2025/12/souta01_01tobira.jpg",
        "https://www.splush.jp/wp-content/uploads/2025/12/souta01_03.jpg",
        "https://www.splush.jp/wp-content/uploads/2025/12/souta01_04.jpg",
    ]
    assert episode.next_url == NEXT_URL
    assert episode.readable
    assert episode.metadata["work_url"] == WORK_URL
    assert episode.metadata["expired"] is False
    assert episode.metadata["images"] == [page.url for page in episode.pages]


def test_episode_follows_a_redirect_to_the_canonical_url(client, fake_response):
    splush, _ = client({"/series/14716/": fake_response(text=EPISODE_HTML, url=EPISODE_URL)})
    episode = splush.episode("https://splush.jp/series/14716")
    assert episode.url == EPISODE_URL


def test_last_episode_has_no_next(client, fake_response):
    splush, _ = client({"/series/17023/": fake_response(text=LAST_EPISODE_HTML)})
    episode = splush.episode("https://www.splush.jp/series/17023/")

    assert episode.episode_title == "第五話（後編）"
    assert episode.next_url is None
    assert episode.metadata["prev_url"] == "https://www.splush.jp/series/17008/"


def test_expired_episode_has_no_pages_but_keeps_its_next(client, fake_response):
    splush, _ = client({"/series/11306/": fake_response(text=EXPIRED_HTML)})
    episode = splush.episode(EXPIRED_URL)

    assert episode.series_title == "B組の名物カップルはまだ付き合ってない"
    assert episode.episode_title == "第三話（前編）"
    assert episode.pages == ()
    assert not episode.readable
    assert episode.next_url == "https://www.splush.jp/series/11322/"
    assert episode.metadata["notice"] == "公開終了しました。"
    assert episode.metadata["expired"] is True


def test_episode_refuses_a_work_page(client, fake_response):
    splush, _ = client({"/series/14712/": fake_response(text=WORK_HTML)})
    with pytest.raises(NotAnEpisodePageError, match="work page"):
        splush.episode(WORK_URL)


def test_page_of_neither_kind_is_not_an_episode(client, fake_response):
    splush, _ = client({"/series/14713/": fake_response(text=ATTACHMENT_HTML)})
    with pytest.raises(NotAnEpisodePageError, match="neither"):
        splush.episode("https://www.splush.jp/series/14713/")


def test_missing_page_is_not_an_episode(client, fake_response):
    splush, _ = client({"/series/99999/": fake_response(text="404", status_code=HTTPStatus.NOT_FOUND)})
    with pytest.raises(NotAnEpisodePageError, match="404"):
        splush.episode("https://www.splush.jp/series/99999/")


def test_other_http_errors_propagate(client, fake_response):
    splush, _ = client({"/series/14716/": fake_response(text="", status_code=HTTPStatus.SERVICE_UNAVAILABLE)})
    with pytest.raises(HTTPStatusError):
        splush.episode(EPISODE_URL)


# --- series ---------------------------------------------------------------------------


def test_series_urls_lists_the_episodes_oldest_first_deduplicated(client, fake_response):
    splush, _ = client({"/series/14712/": fake_response(text=WORK_HTML)})
    urls = splush.series_urls(WORK_URL)

    assert urls == [EPISODE_URL, NEXT_URL]
    assert all(Splush.suitable(url) for url in urls)


def test_series_urls_raises_on_an_empty_listing(client, fake_response):
    splush, _ = client({"/series/15000/": fake_response(text=EMPTY_WORK_HTML)})
    with pytest.raises(NotAnEpisodePageError, match="no readable episode"):
        splush.series_urls("https://www.splush.jp/series/15000/")


def test_series_urls_refuses_an_episode_page(client, fake_response):
    splush, _ = client({"/series/14716/": fake_response(text=EPISODE_HTML)})
    with pytest.raises(UnsupportedUrlError, match="episode"):
        splush.series_urls(EPISODE_URL)


def test_series_urls_refuses_a_url_of_the_wrong_shape(client):
    splush, session = client({})
    with pytest.raises(UnsupportedUrlError):
        splush.series_urls("https://www.splush.jp/series-backnumber/")
    assert session.calls == []


# --- downloading ----------------------------------------------------------------------


def _jpeg(color):
    raw = BytesIO()
    Image.new("RGB", (6, 8), color).save(raw, "JPEG", quality=100)
    return raw.getvalue()


def test_download_writes_the_first_page_as_served(client, fake_response, tmp_path):
    splush, session = client(
        {
            "/series/14716/": fake_response(text=EPISODE_HTML),
            # Flat greys survive the JPEG round trip through the downloader exactly.
            "souta01_01tobira.jpg": fake_response(_jpeg((128, 128, 128)), content_type="image/jpeg"),
            "souta01_03.jpg": fake_response(_jpeg((64, 64, 64)), content_type="image/jpeg"),
        },
    )
    result = Downloader(splush, tmp_path, only_first=True).download(EPISODE_URL)

    assert result.status == "saved"
    assert result.save_dir == tmp_path / "そんなのズルいよ蒼汰くん" / "第一話（前編）"
    written = Image.open(result.save_dir / "0.jpg")
    assert written.size == (6, 8)
    assert written.getpixel((3, 4)) == (128, 128, 128)
    assert not (result.save_dir / "1.jpg").exists()
    # The image is asked for with the episode as Referer, which the site does not need but tolerates.
    assert session.calls[-1].endswith("souta01_01tobira.jpg")
    assert session.headers_seen[-1]["Referer"] == EPISODE_URL


# --- logging in -----------------------------------------------------------------------


# --- the real site --------------------------------------------------------------------

# One episode per known host, free to read without an account: the first
# episode of a work. `splush.jp` redirects to `www.splush.jp`.
TEST_URLS: dict[str, str] = {
    "splush.jp": "https://splush.jp/series/14716/",
    "www.splush.jp": "https://www.splush.jp/series/14716/",
}


@pytest.mark.network
@pytest.mark.parametrize("host", TEST_URLS)
def test_site_download(tmp_path, host):
    result = Downloader(Splush(), tmp_path, only_first=True).download(TEST_URLS[host])
    assert result.status == "saved"
    assert result.episode.url == EPISODE_URL
    assert (result.save_dir / "0.jpg").stat().st_size > 0


@pytest.mark.network
def test_work_page_lists_episodes_oldest_first():
    splush = Splush()
    assert splush.is_series(WORK_URL)
    urls = splush.series_urls(WORK_URL)
    assert urls[:2] == [EPISODE_URL, NEXT_URL]
    assert all(Splush.suitable(url) for url in urls)
