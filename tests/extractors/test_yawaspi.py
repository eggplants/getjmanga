from __future__ import annotations

from http import HTTPStatus
from io import BytesIO

import pytest
from httpx import HTTPStatusError
from PIL import Image

from getjmanga.downloader import Downloader
from getjmanga.errors import NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractors.yawaspi import Yawaspi

WORK_URL = "https://yawaspi.com/hajisef/index.html"
EPISODE_URL = "https://yawaspi.com/hajisef/comic/001_001.html"
NEXT_URL = "https://yawaspi.com/hajisef/comic/sp001_001.html"
LAST_URL = "https://yawaspi.com/hajisef/comic/024_001.html"
ONESHOT_URL = "https://yawaspi.com/chijyo/index.html"

# The listing every page of a work carries: newest first, one shop link for
# the volumes that hold the expired episodes, one episode linked twice.
LISTING_HTML = """
<section class="page__read"><div class="page__read__inner"><ul class="inner__content">
  <li><a href="/hajisef/comic/024_001.html">
    <div class="-new"><img src="/commons/img/pages/hajisef/024.jpg" alt=""></div>
    <dl><dt>第24話</dt><dd></dd></dl></a></li>
  <li><a href="/hajisef/comic/sp001_001.html#again"><dl><dt>第1巻発売記念PR</dt></dl></a></li>
  <li><a href="/hajisef/comic/sp001_001.html"><div><img src="/commons/img/pages/hajisef/sp001.jpg" alt=""></div>
    <dl><dt>第1巻発売記念PR</dt><dd></dd></dl></a></li>
  <li><a href="https://csbs.shogakukan.co.jp/book?book_group_id=19322" target="_blank">
    <div><img src="/commons/img/pages/hajisef/ecomic.jpg" alt=""></div>
    <dl><dt>第2話-第23話</dt><dd></dd></dl></a></li>
  <li><a href="/hajisef/comic/001_001.html"><div><img src="/commons/img/pages/hajisef/001.jpg" alt=""></div>
    <dl><dt>第1話</dt><dd></dd></dl></a></li>
</ul></div></section>
"""

# The parts of an episode page `episode()` reads, as the site writes them: the
# header with both titles, the vertical strip of images, the navigation and
# the work's listing repeated under it.
EPISODE_HTML = f"""
<html><head><title>第1話 | はじめてのセフレ | やわらかスピリッツ</title></head><body>
<div class="sidebar"><h3><span>新刊単行本</span></h3></div>
<div class="container">
<header class="header -pagedetail"><div class="page__header">
  <h2>はじめてのセフレ</h2>
  <p><strong>ゆりかわ</strong><br>Yurikawa</p>
  <h3>第1話</h3>
  <p><span class="-date">更新日: 2025/4/9</span></p>
</div></header>
<article class="page -pagedetail"><div class="page__inner">
<section class="page__detail"><div class="page__detail__inner">
  <div class="page__detail__vertical"><div class="vertical__inner"><ul>
    <li><img src="https://cdn.yawaspi.com/hajisef/001/jKOl/001_001_01.jpg" alt=""></li>
    <li><img src="//cdn.yawaspi.com/hajisef/001/jKOl/001_001_02.jpg" alt=""></li>
    <li><img src="https://cdn.yawaspi.com/hajisef/001/jKOl/001_001_03.jpg" alt=""></li>
  </ul></div></div>
  <ul class="detail__navi">
    <li><a href="sp001_001.html" class="-next"><p><span>第1巻発売記念PR</span></p></a></li>
  </ul>
</div></section>
{LISTING_HTML}
</div></article></div>
</body></html>
"""

# The newest episode: a `prev` link but no `next`.
LAST_EPISODE_HTML = """
<html><head><title>第24話 | はじめてのセフレ | やわらかスピリッツ</title></head><body>
<div class="page__header"><h2>はじめてのセフレ</h2><h3>第24話</h3></div>
<section class="page__detail"><div class="page__detail__vertical"><ul>
  <li><img src="https://cdn.yawaspi.com/hajisef/024/Ab12/024_001_01.jpg" alt=""></li>
</ul></div>
<ul class="detail__navi"><li><a href="sp003_001.html" class="-prev"><p><span>第3巻発売記念PR</span></p></a></li></ul>
</section>
</body></html>
"""

# A one-shot: `index.html` is the episode, there is no episode heading, and
# the second half is linked as `index2.html`.
ONESHOT_HTML = """
<html><head><title>痴女の夜 | やわらかスピリッツ</title></head><body>
<div class="page__header"><h2>痴女の夜</h2><p><strong>矢寺圭太</strong>keita yatera</p>
  <p><span class="-date">更新日: 2019/3/25</span></p></div>
<section class="page__detail"><div class="page__detail__vertical"><ul>
  <li><img src="https://cdn.yawaspi.com/chijyo/001/001_001_01.jpg" alt=""></li>
  <li><img src="https://cdn.yawaspi.com/chijyo/001/001_001_02.jpg" alt=""></li>
</ul></div>
<ul class="detail__navi"><li><a href="index2.html" class="-next"><p><span>後編</span></p></a></li></ul>
</section>
</body></html>
"""

# A viewer with nothing in it: the site has no locked episodes, but this is
# what one would look like.
EMPTY_EPISODE_HTML = """
<html><body>
<div class="page__header"><h2>はじめてのセフレ</h2><h3>第25話</h3></div>
<section class="page__detail"><div class="page__detail__vertical"><ul></ul></div>
<ul class="detail__navi"><li><a href="026_001.html" class="-next"><p><span>第26話</span></p></a></li></ul>
</section>
</body></html>
"""

# A work page: the header without an episode heading, the update log and the listing.
WORK_HTML = f"""
<html><head><title>はじめてのセフレ | やわらかスピリッツ</title></head><body>
<header class="header -page"><div class="page__header"><h2>はじめてのセフレ</h2></div></header>
<section class="page__info"><ul>
  <li><a href="./comic/024_001.html"><dl><dt>2026/9/2</dt><dd>第24話 を更新しました。</dd></dl></a></li>
</ul></section>
{LISTING_HTML}
</body></html>
"""

EMPTY_WORK_HTML = """
<html><body><div class="page__header"><h2>まだ始まらない</h2></div>
<section class="page__read"><div class="page__read__inner"><ul class="inner__content"></ul></div></section>
</body></html>
"""

# A work whose page is a plain landing page: neither kind.
LANDING_HTML = """
<html><head><title>ヴァンピアーズ | やわらかスピリッツ</title></head><body>
<div class="page__header"><h2>ヴァンピアーズ</h2></div>
<section class="page__pickup"><a href="https://shogakukan-comic.jp/book?isbn=9784098607242">単行本</a></section>
</body></html>
"""


@pytest.fixture
def client(fake_session):
    def make(routes):
        session = fake_session(routes)
        return Yawaspi(session), session

    return make


# --- URLs ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        WORK_URL,
        EPISODE_URL,
        ONESHOT_URL,
        "https://yawaspi.com/hajisef/",
        "https://yawaspi.com/hajisef",
        "https://yawaspi.com/brotherbuddy/index2.html",
        "https://yawaspi.com/alcoholandogre-girls/comic/sp003_001.html",
        "https://www.yawaspi.com/hajisef/comic/001_001.html",
        "https://yawaspi.com/hajisef/comic/001_001.html?from=top",
    ],
)
def test_suitable_accepts_work_and_episode_urls(url):
    assert Yawaspi.suitable(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://yawaspi.com/hajisef/comic/001_001.html",
        "https://yawaspi.com/",
        "https://yawaspi.com/series/",
        "https://yawaspi.com/completion/index.html",
        "https://yawaspi.com/shortstory/",
        "https://yawaspi.com/commons/img/pages/hajisef/001.jpg",
        "https://yawaspi.com/hajisef/comic/",
        "https://yawaspi.com/hajisef/comic/001_001.jpg",
        "https://yawaspi.com/hajisef/about.html",
        "https://cdn.yawaspi.com/hajisef/001/jKOl/001_001_01.jpg",
        "https://www.sunday-webry.com/episode/1",
    ],
)
def test_suitable_rejects_other_urls(url):
    assert not Yawaspi.suitable(url)


# --- parsing --------------------------------------------------------------------------


# --- is_series and the cache ----------------------------------------------------------


def test_is_series_fetches_a_work_url_once_and_series_urls_reuses_it(client, fake_response):
    yawaspi, session = client({"/hajisef/index.html": fake_response(text=WORK_HTML)})

    assert yawaspi.is_series(WORK_URL)
    assert yawaspi.is_series("https://yawaspi.com/hajisef/")
    assert yawaspi.is_series("https://yawaspi.com/hajisef")
    assert yawaspi.series_urls(WORK_URL) == [EPISODE_URL, NEXT_URL, LAST_URL]
    # The three spellings are one page.
    assert session.calls == [WORK_URL]


def test_is_series_is_false_for_a_one_shot_and_episode_reuses_the_page(client, fake_response):
    yawaspi, session = client({"/chijyo/index.html": fake_response(text=ONESHOT_HTML)})

    assert not yawaspi.is_series("https://yawaspi.com/chijyo/")
    episode = yawaspi.episode(ONESHOT_URL)

    assert episode.series_title == "痴女の夜"
    assert session.calls == [ONESHOT_URL]


def test_the_query_string_is_not_sent(client, fake_response):
    yawaspi, session = client({"/comic/001_001.html": fake_response(text=EPISODE_HTML)})
    yawaspi.episode(f"{EPISODE_URL}?from=top#page3")
    assert session.calls == [EPISODE_URL]


# --- episode --------------------------------------------------------------------------


def test_episode_reads_the_titles_the_pages_and_the_next_episode(client, fake_response):
    yawaspi, _ = client({"/comic/001_001.html": fake_response(text=EPISODE_HTML)})
    episode = yawaspi.episode(EPISODE_URL)

    assert episode.url == EPISODE_URL
    assert episode.series_title == "はじめてのセフレ"
    assert episode.episode_title == "第1話"
    assert [page.url for page in episode.pages] == [
        "https://cdn.yawaspi.com/hajisef/001/jKOl/001_001_01.jpg",
        "https://cdn.yawaspi.com/hajisef/001/jKOl/001_001_02.jpg",
        "https://cdn.yawaspi.com/hajisef/001/jKOl/001_001_03.jpg",
    ]
    assert all(page.extra == {} for page in episode.pages)
    assert episode.next_url == NEXT_URL
    assert episode.readable
    assert episode.metadata["author"] == "ゆりかわ"
    assert episode.metadata["updated"] == "更新日: 2025/4/9"
    assert episode.metadata["prev_url"] is None
    assert episode.metadata["images"] == [page.url for page in episode.pages]


def test_episode_follows_a_redirect_to_the_canonical_host(client, fake_response):
    yawaspi, _ = client({"/comic/001_001.html": fake_response(text=EPISODE_HTML, url=EPISODE_URL)})
    episode = yawaspi.episode("https://www.yawaspi.com/hajisef/comic/001_001.html")
    assert episode.url == EPISODE_URL


def test_last_episode_has_no_next(client, fake_response):
    yawaspi, _ = client({"/comic/024_001.html": fake_response(text=LAST_EPISODE_HTML)})
    episode = yawaspi.episode(LAST_URL)

    assert episode.episode_title == "第24話"
    assert (episode.prev_url, episode.next_url) == ("https://yawaspi.com/hajisef/comic/sp003_001.html", None)
    assert episode.metadata["prev_url"] == "https://yawaspi.com/hajisef/comic/sp003_001.html"


def test_one_shot_is_titled_after_the_work_and_names_its_second_half(client, fake_response):
    yawaspi, _ = client({"/chijyo/index.html": fake_response(text=ONESHOT_HTML)})
    episode = yawaspi.episode(ONESHOT_URL)

    assert episode.series_title == "痴女の夜"
    assert episode.episode_title == "痴女の夜"
    assert len(episode.pages) == 2
    assert episode.next_url == "https://yawaspi.com/chijyo/index2.html"
    assert Yawaspi.suitable(episode.next_url)


def test_viewer_without_images_has_no_pages_but_keeps_its_next(client, fake_response):
    yawaspi, _ = client({"/comic/025_001.html": fake_response(text=EMPTY_EPISODE_HTML)})
    episode = yawaspi.episode("https://yawaspi.com/hajisef/comic/025_001.html")

    assert episode.episode_title == "第25話"
    assert episode.pages == ()
    assert not episode.readable
    assert episode.next_url == "https://yawaspi.com/hajisef/comic/026_001.html"


def test_episode_refuses_a_work_page(client, fake_response):
    yawaspi, _ = client({"/hajisef/index.html": fake_response(text=WORK_HTML)})
    with pytest.raises(NotAnEpisodePageError, match="work page"):
        yawaspi.episode(WORK_URL)


def test_episode_refuses_a_url_of_the_wrong_shape_without_a_request(client):
    yawaspi, session = client({})
    with pytest.raises(NotAnEpisodePageError, match="neither"):
        yawaspi.episode("https://yawaspi.com/series/index.html")
    assert session.calls == []


def test_page_of_neither_kind_is_not_an_episode(client, fake_response):
    yawaspi, _ = client({"/vampeerz/index.html": fake_response(text=LANDING_HTML)})
    with pytest.raises(NotAnEpisodePageError, match="neither"):
        yawaspi.episode("https://yawaspi.com/vampeerz/")


def test_expired_episode_is_gone(client, fake_response):
    # An episode whose free run has ended is a 404: the site keeps no page for it.
    yawaspi, _ = client({"/comic/002_001.html": fake_response(text="404 Not Found", status_code=HTTPStatus.NOT_FOUND)})
    with pytest.raises(NotAnEpisodePageError, match="404"):
        yawaspi.episode("https://yawaspi.com/hajisef/comic/002_001.html")


def test_other_http_errors_propagate(client, fake_response):
    yawaspi, _ = client({"/comic/001_001.html": fake_response(text="", status_code=HTTPStatus.SERVICE_UNAVAILABLE)})
    with pytest.raises(HTTPStatusError):
        yawaspi.episode(EPISODE_URL)


# --- series ---------------------------------------------------------------------------


def test_series_urls_lists_the_episodes_oldest_first_deduplicated(client, fake_response):
    yawaspi, _ = client({"/hajisef/index.html": fake_response(text=WORK_HTML)})
    urls = yawaspi.series_urls("https://yawaspi.com/hajisef/")

    assert urls == [EPISODE_URL, NEXT_URL, LAST_URL]
    assert all(Yawaspi.suitable(url) for url in urls)


def test_series_urls_raises_on_an_empty_listing(client, fake_response):
    yawaspi, _ = client({"/newwork/index.html": fake_response(text=EMPTY_WORK_HTML)})
    with pytest.raises(NotAnEpisodePageError, match="no readable episode"):
        yawaspi.series_urls("https://yawaspi.com/newwork/")


def test_series_urls_refuses_a_one_shot(client, fake_response):
    yawaspi, _ = client({"/chijyo/index.html": fake_response(text=ONESHOT_HTML)})
    with pytest.raises(UnsupportedUrlError, match="episode"):
        yawaspi.series_urls(ONESHOT_URL)


def test_series_urls_refuses_a_url_of_the_wrong_shape(client):
    yawaspi, session = client({})
    with pytest.raises(UnsupportedUrlError):
        yawaspi.series_urls(EPISODE_URL)
    with pytest.raises(UnsupportedUrlError):
        yawaspi.series_urls("https://yawaspi.com/series/")
    assert session.calls == []


# --- downloading ----------------------------------------------------------------------


def _jpeg(color):
    raw = BytesIO()
    Image.new("RGB", (6, 8), color).save(raw, "JPEG", quality=100)
    return raw.getvalue()


def test_download_writes_the_first_page_as_served(client, fake_response, tmp_path):
    yawaspi, session = client(
        {
            "/comic/001_001.html": fake_response(text=EPISODE_HTML),
            # Flat greys survive the JPEG round trip through the downloader exactly.
            "001_001_01.jpg": fake_response(_jpeg((128, 128, 128)), content_type="image/jpeg"),
            "001_001_02.jpg": fake_response(_jpeg((64, 64, 64)), content_type="image/jpeg"),
        },
    )
    result = Downloader(yawaspi, tmp_path, only_first=True).download(EPISODE_URL)

    assert result.status == "saved"
    assert result.save_dir == tmp_path / "yawaspi.com" / "はじめてのセフレ" / "第1話"
    written = Image.open(result.save_dir / "0.jpg")
    assert written.size == (6, 8)
    assert written.getpixel((3, 4)) == (128, 128, 128)
    assert not (result.save_dir / "1.jpg").exists()
    # The image is asked for as-is, with the episode as Referer, which the CDN does not need but tolerates.
    assert session.calls[-1] == "https://cdn.yawaspi.com/hajisef/001/jKOl/001_001_01.jpg"
    assert session.headers_seen[-1]["Referer"] == EPISODE_URL


# --- logging in -----------------------------------------------------------------------


# --- the real site --------------------------------------------------------------------

# One episode per known host, free to read without an account: the first
# episode of a work. `www.yawaspi.com` redirects to `yawaspi.com`.
TEST_URLS: dict[str, str] = {
    "www.yawaspi.com": "https://www.yawaspi.com/hajisef/comic/001_001.html",
    "yawaspi.com": "https://yawaspi.com/hajisef/comic/001_001.html",
}


@pytest.mark.network
@pytest.mark.parametrize("host", TEST_URLS)
def test_site_download(tmp_path, host):
    result = Downloader(Yawaspi(), tmp_path, only_first=True).download(TEST_URLS[host])
    assert result.status == "saved"
    assert (result.save_dir / "0.jpg").stat().st_size > 0


@pytest.mark.network
def test_work_page_lists_episodes():
    yawaspi = Yawaspi()
    assert yawaspi.is_series("https://yawaspi.com/hajisef/")
    urls = yawaspi.series_urls("https://yawaspi.com/hajisef/")
    assert urls[0] == "https://yawaspi.com/hajisef/comic/001_001.html"
    assert all(Yawaspi.suitable(url) for url in urls)


@pytest.mark.network
def test_one_shot_index_is_an_episode():
    yawaspi = Yawaspi()
    assert not yawaspi.is_series("https://yawaspi.com/chijyo/")
    episode = yawaspi.episode("https://yawaspi.com/chijyo/")
    assert episode.episode_title == "痴女の夜"
    assert episode.readable


@pytest.mark.network
def test_expired_episode_is_not_a_page():
    with pytest.raises(NotAnEpisodePageError, match="404"):
        Yawaspi().episode("https://yawaspi.com/osaka/comic/050_001.html")
