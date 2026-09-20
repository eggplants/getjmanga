from __future__ import annotations

from http import HTTPStatus
from io import BytesIO

import pytest
from httpx import HTTPStatusError
from PIL import Image

from getjmanga.downloader import Downloader
from getjmanga.errors import NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractors.rookie import Rookie

SERIES_URL = "https://rookie.shonenjump.com/series/OmkvmYUVb1c"
EPISODE_URL = "https://rookie.shonenjump.com/series/OmkvmYUVb1c/OmkvmYUadZs"
NEXT_URL = "https://rookie.shonenjump.com/series/OmkvmYUVb1c/OmkvmYUanwM"

# The parts of an episode page `episode()` reads, as the site writes them:
# the viewer heading, the `section` with the page geometry and structure, one
# `img.js-page-image` per page, and the back matter with the next-episode button.
EPISODE_HTML = """
<html lang="ja" data-route="core:series:episode" data-site-type="ジャンプルーキー">
<head><title>毎日4コマ2 31話 - ジャンプルーキー！</title></head>
<body class="viewer viewer-horizontal is-pc">
<header><h2 class="series-title-container">
  <span class="series-title">毎日4コマ2</span>
  <span class="episode-number">31話</span>
</h2></header>
<section class="content-inner js-episode-read-mark-source scroll-horizontal js-horizontal-viewer"
  data-page-width="990" data-page-height="2145" data-episode-id="4208947663164044699"
  data-page-structure='{"spread":[{"screen":1,"align":"left","type":"flyleaf"}],
    "single":[{"screen":1,"type":"main","align":"right"}]}'>
<div class="image-container js-viewer-content" dir="rtl">
  <p class="page-area js-page-area js-front-flyleaf" dir="ltr"></p>
  <p class="page-area js-page-area" dir="ltr">
    <img class="js-page-image" src="https://cdn-img.rookie.shonenjump.com/public/pageimages/1-aa">
  </p>
  <p class="page-area js-page-area" dir="ltr"><img class="js-page-image" src="/public/pageimages/2-bb"></p>
  <div class="page-area js-page-area js-ad-area" dir="ltr">
    <img src="https://cdn.rookie.shonenjump.com/images/core/ad.svg">
  </div>
  <div class="page-area js-page-area js-back-matter-area" dir="ltr">
    <div class="page-upper"><div class="page-upper-content">
      <p class="series-title-container">
        <a class="js-series-no-switch" href="/series/OmkvmYUVb1c"><span class="series-title">毎日4コマ2</span></a>
        <span class="episode-number">第31話</span>
      </p>
      <p class="user-container">
        <a href="/users/1"><img class="user-icon" src="x"><span class="user-name">ハルル</span></a>
      </p>
      <p id="series-history">
        <span class="series-history-title">公開</span>
        <time class="series-history-date">2026年05月20日</time>
      </p>
      <p class="button-container">
        <a class="button next-episode-button" href="/series/OmkvmYUVb1c/OmkvmYUanwM">続きを読む (第 32 話)</a>
      </p>
    </div></div>
  </div>
</div>
</section>
</body></html>
"""

# The last episode of a series: the button leads back to the work page.
LAST_EPISODE_HTML = """
<html><head><title>風船の幽霊 1話 - ジャンプルーキー！</title></head><body>
<h2 class="series-title-container">
  <span class="series-title">風船の幽霊</span><span class="episode-number">1話</span>
</h2>
<section class="content-inner js-episode-read-mark-source js-horizontal-viewer"
  data-page-width="1200" data-page-height="1691" data-episode-id="5">
<div class="image-container js-viewer-content">
  <p class="page-area js-page-area">
    <img class="js-page-image" src="https://cdn-img.rookie.shonenjump.com/public/pageimages/9-ff">
  </p>
  <p class="button-container">
    <a class="button next-episode-button js-series-no-switch" href="/series/TWpXKpYhy44">作品ページへ</a>
  </p>
</div>
</section>
</body></html>
"""

# A viewer page whose image container is empty: an episode, but nothing to read.
EMPTY_EPISODE_HTML = """
<html><head><title>風船の幽霊 2話 - ジャンプルーキー！</title></head><body>
<section class="content-inner js-episode-read-mark-source js-horizontal-viewer"
  data-page-width="1200" data-page-height="1691" data-episode-id="6">
<div class="image-container js-viewer-content"></div>
</section>
<p class="button-container">
  <a class="button next-episode-button" href="/series/TWpXKpYhy44/TWpXKpYh2Y9">続きを読む</a>
</p>
</body></html>
"""

# A work page: the category, the title, the author and the episode list, oldest first.
SERIES_HTML = """
<html lang="ja" data-route="core:series"><head><title>毎日4コマ2 - ジャンプルーキー！</title></head><body>
<div class="series-title-container">
  <a href="/categories/comedy"><p class="series-category">コメディ/ギャグ</p></a>
  <h1 class="series-title">毎日4コマ2</h1>
</div>
<span class="user-name"><a href="/users/14728546961186664698"><strong>ハルル</strong></a> 作</span>
<a href="/series/OmkvmYUVb1c/OmkvmYUadZs">最新話</a>
<ul class="js-episode-list">
  <li class="episode-wrapper js-episode-item">
    <a class="episode-content" href="/series/OmkvmYUVb1c/OmkvmYUVb1k"><span class="episode-title">第 1 話</span></a>
  </li>
  <li class="episode-wrapper js-episode-item">
    <a class="episode-content" href="/series/OmkvmYUVb1c/OmkvmYUVcfQ"><span class="episode-title">第 2 話</span></a>
  </li>
  <li class="episode-wrapper js-episode-item">
    <a class="episode-content" href="/series/OmkvmYUVb1c/OmkvmYUVcfQ#again">
      <span class="episode-title">第 2 話</span>
    </a>
  </li>
  <li class="episode-wrapper js-episode-item">
    <a class="episode-content" href="https://rookie.shonenjump.com/series/OmkvmYUVb1c/OmkvmYUadZs">
      <span class="episode-title">第 31 話</span>
    </a>
  </li>
</ul>
<a href="/series/TWpXKpYmBi0">西遊日記</a>
</body></html>
"""

NOT_FOUND_HTML = """
<html lang="ja" data-route="core:404">
<head><title>ページが見つかりません - ジャンプルーキー！</title></head><body></body></html>
"""


def jpeg(colour=(200, 30, 30)):
    raw = BytesIO()
    Image.new("RGB", (16, 24), colour).save(raw, "JPEG")
    return raw.getvalue()


def dominant_channel(image):
    """Which of R, G, B the top-left pixel is strongest in (JPEG noise aside)."""
    channels = [band.getpixel((0, 0)) for band in image.convert("RGB").split()]
    return channels.index(max(channels))


# --- urls ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (EPISODE_URL, True),
        (EPISODE_URL + "/", True),
        (SERIES_URL, True),
        (SERIES_URL + "/", True),
        ("https://rookie.shonenjump.com/series/TWpXKpYj_o0/TWpXKpYj-oA", True),
        ("http://rookie.shonenjump.com/series/OmkvmYUVb1c/OmkvmYUadZs", False),
        ("https://shonenjumpplus.com/series/OmkvmYUVb1c/OmkvmYUadZs", False),
        ("https://rookie.shonenjump.com/", False),
        ("https://rookie.shonenjump.com/ranking", False),
        ("https://rookie.shonenjump.com/users/14728546961186664698", False),
        ("https://rookie.shonenjump.com/series/OmkvmYUVb1c/OmkvmYUadZs/comments", False),
        ("https://rookie.shonenjump.com/embed/series/OmkvmYUVb1c/OmkvmYUadZs", False),
    ],
)
def test_suitable(url, expected):
    assert Rookie.suitable(url) is expected


def test_is_series_means_a_work_page():
    assert Rookie.is_series(SERIES_URL)
    assert Rookie.is_series(SERIES_URL + "/")
    assert not Rookie.is_series(EPISODE_URL)
    assert not Rookie.is_series("https://rookie.shonenjump.com/ranking")


# --- reading an episode -------------------------------------------------------------


def test_episode_reads_the_titles_the_pages_and_the_next_url(fake_session, fake_response):
    # The episode page, then -- for the previous episode, which only the series page lists -- that page.
    session = fake_session(
        {"/OmkvmYUadZs": fake_response(text=EPISODE_HTML), "/series/": fake_response(text=SERIES_HTML)}
    )
    episode = Rookie(session).episode(EPISODE_URL)

    assert episode.series_title == "毎日4コマ2"
    assert episode.episode_title == "第31話"
    assert [page.url for page in episode.pages] == [
        "https://cdn-img.rookie.shonenjump.com/public/pageimages/1-aa",
        "https://rookie.shonenjump.com/public/pageimages/2-bb",
    ]
    assert (episode.pages[0].width, episode.pages[0].height) == (990, 2145)
    assert episode.pages[0].extra == {}
    assert (episode.prev_url, episode.next_url) == (f"{SERIES_URL}/OmkvmYUVcfQ", NEXT_URL)
    assert episode.metadata["episode_id"] == "4208947663164044699"
    assert episode.metadata["author"] == "ハルル"
    assert episode.metadata["published"] == "2026年05月20日"
    assert episode.metadata["page_structure"]["single"][0]["type"] == "main"
    assert session.calls == [EPISODE_URL, SERIES_URL]
    assert session.headers_seen[-1]["User-Agent"]


def test_the_last_episode_has_no_next_url(fake_session, fake_response):
    session = fake_session({"/series/": fake_response(text=LAST_EPISODE_HTML)})
    episode = Rookie(session).episode("https://rookie.shonenjump.com/series/TWpXKpYhy44/TWpXKpYh2Y8")
    assert episode.series_title == "風船の幽霊"
    assert episode.episode_title == "1話"
    assert [page.url for page in episode.pages] == ["https://cdn-img.rookie.shonenjump.com/public/pageimages/9-ff"]
    assert episode.next_url is None


def test_a_viewer_without_images_has_no_pages_but_still_names_the_next(fake_session, fake_response):
    session = fake_session({"/series/": fake_response(text=EMPTY_EPISODE_HTML)})
    episode = Rookie(session).episode("https://rookie.shonenjump.com/series/TWpXKpYhy44/TWpXKpYh2Y8")
    assert episode.pages == ()
    assert episode.next_url == "https://rookie.shonenjump.com/series/TWpXKpYhy44/TWpXKpYh2Y9"
    # No heading at all: the titles fall back to the `<title>`.
    assert episode.series_title == "風船の幽霊"
    assert episode.episode_title == "2話"


def test_a_page_without_a_viewer_is_not_an_episode(fake_session, fake_response):
    session = fake_session({"/series/": fake_response(text=SERIES_HTML)})
    with pytest.raises(NotAnEpisodePageError, match="no viewer"):
        Rookie(session).episode(EPISODE_URL)


def test_a_gone_episode_is_not_an_episode(fake_session, fake_response):
    session = fake_session({"/series/": fake_response(text=NOT_FOUND_HTML, status_code=HTTPStatus.NOT_FOUND)})
    with pytest.raises(NotAnEpisodePageError, match="404"):
        Rookie(session).episode(EPISODE_URL)


def test_other_failures_still_raise(fake_session, fake_response):
    session = fake_session({"/series/": fake_response(text="", status_code=HTTPStatus.SERVICE_UNAVAILABLE)})
    with pytest.raises(HTTPStatusError):
        Rookie(session).episode(EPISODE_URL)


def test_episode_rejects_a_series_url(fake_session):
    with pytest.raises(UnsupportedUrlError):
        Rookie(fake_session({})).episode(SERIES_URL)


def test_episode_takes_an_unlisted_host_when_forced(fake_session, fake_response):
    session = fake_session({"/series/": fake_response(text=EPISODE_HTML)})
    episode = Rookie(session).episode("https://mirror.example/series/OmkvmYUVb1c/OmkvmYUadZs")
    assert episode.next_url == "https://mirror.example/series/OmkvmYUVb1c/OmkvmYUanwM"


# --- listing a series ---------------------------------------------------------------


def test_series_urls_keeps_the_page_order_and_drops_duplicates(fake_session, fake_response):
    session = fake_session({"/series/": fake_response(text=SERIES_HTML)})
    urls = Rookie(session).series_urls(SERIES_URL)
    assert urls == [
        "https://rookie.shonenjump.com/series/OmkvmYUVb1c/OmkvmYUVb1k",
        "https://rookie.shonenjump.com/series/OmkvmYUVb1c/OmkvmYUVcfQ",
        EPISODE_URL,
    ]
    assert all(Rookie.suitable(url) and not Rookie.is_series(url) for url in urls)


def test_series_urls_falls_back_to_every_episode_link(fake_session, fake_response):
    html = SERIES_HTML.replace('class="js-episode-list"', "")
    session = fake_session({"/series/": fake_response(text=html)})
    urls = Rookie(session).series_urls(SERIES_URL)
    assert urls[0] == EPISODE_URL
    assert len(urls) == 3


def test_series_urls_raises_on_an_empty_listing(fake_session, fake_response):
    session = fake_session(
        {"/series/": fake_response(text='<html><body><ul class="js-episode-list"></ul></body></html>')}
    )
    with pytest.raises(NotAnEpisodePageError, match="no episode"):
        Rookie(session).series_urls(SERIES_URL)


def test_series_urls_raises_on_a_gone_series(fake_session, fake_response):
    session = fake_session({"/series/": fake_response(text=NOT_FOUND_HTML, status_code=HTTPStatus.NOT_FOUND)})
    with pytest.raises(NotAnEpisodePageError, match="404"):
        Rookie(session).series_urls(SERIES_URL)


def test_series_urls_rejects_an_episode_url(fake_session):
    with pytest.raises(UnsupportedUrlError):
        Rookie(fake_session({})).series_urls(EPISODE_URL)


# --- downloading --------------------------------------------------------------------


def test_download_writes_the_pages_as_served(tmp_path, fake_session, fake_response):
    session = fake_session(
        {
            "/pageimages/1-aa": fake_response(jpeg((200, 30, 30)), content_type="image/jpeg"),
            "/pageimages/2-bb": fake_response(jpeg((30, 200, 30)), content_type="image/jpeg"),
            "/series/": fake_response(text=EPISODE_HTML),
        },
    )
    result = Downloader(Rookie(session), tmp_path).download(EPISODE_URL)

    assert result.status == "saved"
    assert result.save_dir == tmp_path / "rookie.shonenjump.com" / "毎日4コマ2" / "第31話"
    assert sorted(path.name for path in result.save_dir.iterdir()) == ["0.jpg", "1.jpg"]
    with Image.open(result.save_dir / "0.jpg") as image:
        assert image.size == (16, 24)
        assert dominant_channel(image) == 0
    with Image.open(result.save_dir / "1.jpg") as image:
        assert dominant_channel(image) == 1
    # The images are asked for with the episode as Referer, like the browser does.
    assert session.headers_seen[-1]["Referer"] == EPISODE_URL


# --- logging in ---------------------------------------------------------------------


# --- the real site ------------------------------------------------------------------

# One episode per known host, free to read without an account.
TEST_URLS: dict[str, str] = {
    "rookie.shonenjump.com": "https://rookie.shonenjump.com/series/OmkvmYUVb1c/OmkvmYUVb1k",
}


@pytest.mark.network
@pytest.mark.parametrize("host", TEST_URLS)
def test_site_download(tmp_path, host):
    result = Downloader(Rookie(), tmp_path, only_first=True).download(TEST_URLS[host])
    assert result.status == "saved"
    assert result.episode.series_title == "毎日4コマ2"
    assert result.episode.next_url is not None
    assert (result.save_dir / "0.jpg").stat().st_size > 0


@pytest.mark.network
def test_site_lists_the_series():
    urls = Rookie().series_urls(SERIES_URL)
    assert urls[0] == TEST_URLS["rookie.shonenjump.com"]
    assert EPISODE_URL in urls
    assert all(Rookie.suitable(url) for url in urls)
