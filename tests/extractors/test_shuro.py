from __future__ import annotations

from datetime import date
from http import HTTPStatus
from io import BytesIO

import pytest
from httpx import HTTPStatusError
from PIL import Image

from getjmanga.downloader import Downloader
from getjmanga.errors import NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractors.shuro import Shuro, episode_number, parse_manga_data, reading_order

WORK_URL = "https://shuro.world/manga/kappanokarty/"
EPISODE_URL = "https://shuro.world/episode/71005/"
NEXT_URL = "https://shuro.world/episode/146210/"

# The `const mangaData` script every episode page embeds: the work with its
# episodes as the site lists them, newest first.
MANGA_DATA = (
    '<script>const mangaData = [{"title":"\\u30ab\\u30c3\\u30d1\\u306e\\u30ab\\u30fc\\u30c6\\u30a3\\u3068\\u795f\\u308a'
    '\\u3069\\u3082\\u306e\\u611b","slug":"kappanokarty","id":71038,'
    '"permalink":"https:\\/\\/shuro.world\\/manga\\/kappanokarty\\/",'
    '"authors":[{"title":"\\u5bae\\u5d0e\\u590f\\u6b21\\u7cfb","role":"\\u6f2b\\u753b\\u5bb6"}],'
    '"episodes":[{"title":"\\u7b2c\\uff115\\u8a71","titleSub":"\\u300c\\u30a6\\u30a8\\u3061\\u3083\\u3093\\u300d",'
    '"permalink":"https:\\/\\/shuro.world\\/episode\\/146210\\/","relatedWork":71038},'
    '{"title":"\\u7b2c\\uff11\\u8a71","titleSub":"\\u300c\\u5272\\u308c\\u3066\\u307e\\u3059\\u3088\\u300d",'
    '"permalink":"https:\\/\\/shuro.world\\/episode\\/71005\\/","relatedWork":71038}]}];'
    "const publicationData = [];</script>"
)

# The parts of an episode page `episode()` reads: the sticky header naming the
# work and the episode, the vertical viewer with one `div.slide` per page (a
# wide spread gets `slide-horizontal`), and the `mangaData` script.
EPISODE_HTML = f"""
<html><head><title>第１話 「割れてますよ、頭の皿」 | カッパのカーティと祟りどもの愛 | SHURO | シュロ</title></head>
<body class=" template-episode template-episode">
<a href="{WORK_URL}" class="viewer-top posf" data-manga-id="71038">
  <div class="marquee marquee-text"><div class="marquee-container"><div class="marquee-content">
    <div class="marquee-element"><b>カッパのカーティと祟りどもの愛</b></div>
  </div></div></div>
  <div class="rowm-0_25 marquee marquee-text"><div class="marquee-container"><div class="marquee-content">
    <div class="marquee-element">第１話 「割れてますよ、頭の皿」</div>
  </div></div></div>
</a>
<div class="posr zi0">
  <div class="viewer-vertical js-sticky-container pb-6" data-viewer="1">
    <div class="slide rowm-1">
      <img src="https://img.shuro.world/wp-content/uploads/2025/12/09204919/cover.jpg" width="1353" height="1920">
    </div>
    <div class="slide rowm-1">
      <img src="/wp-content/uploads/2024/08/25210308/kappa_01_01.jpg" width="1350" height="1920" loading="lazy">
    </div>
    <div class="slide rowm-1 slide-horizontal">
      <img src="https://img.shuro.world/wp-content/uploads/2024/08/25210313/kappa_01_02-03-scaled.jpg"
           width="2560" height="1820">
    </div>
    <div class="viewer-controller js-sticky">
      <a href="#" class="top slide-prev" data-viewer="1"></a>
      <a href="#" class="bottom slide-next" data-viewer="1"></a>
    </div>
    <p class="slide-counter" data-viewer="1"><b>--/--</b></p>
  </div>
</div>
<div class="rowm-5">
  <h3>他のエピソード</h3>
  <a class="db posr line-box" href="{NEXT_URL}"><p><b>第１5話</b></p><p>「ウエちゃんが撮ったやつ」</p></a>
</div>
{MANGA_DATA}
</body></html>
"""

# The newest episode: last in reading order, so nothing follows it.
LAST_EPISODE_HTML = f"""
<html><head><title>第１5話 「ウエちゃんが撮ったやつ」 | カッパのカーティと祟りどもの愛 | SHURO | シュロ</title></head>
<body class=" template-episode template-episode">
<a href="{WORK_URL}" class="viewer-top posf" data-manga-id="71038">
  <div class="marquee"><div class="marquee-element"><b>カッパのカーティと祟りどもの愛</b></div></div>
  <div class="marquee"><div class="marquee-element">第１5話 「ウエちゃんが撮ったやつ」</div></div>
</a>
<div class="viewer-vertical" data-viewer="1">
  <div class="slide rowm-1"><img src="https://img.shuro.world/wp-content/uploads/2026/08/kati2.jpg" width="1350"></div>
</div>
{MANGA_DATA}
</body></html>
"""

# A page with the viewer standing empty, and no `mangaData` to name a neighbour.
EMPTY_EPISODE_HTML = """
<html><head><title>第２話 | 何か | SHURO | シュロ</title></head><body class="template-episode">
<a href="https://shuro.world/manga/nanika/" class="viewer-top">
  <div class="marquee"><div class="marquee-element"><b>何か</b></div></div>
  <div class="marquee"><div class="marquee-element">第２話</div></div>
</a>
<div class="viewer-vertical" data-viewer="1">
  <div class="viewer-controller js-sticky"></div>
</div>
</body></html>
"""

# An information post: the site's chrome, no viewer.
INFORMATION_HTML = """
<html><head><title>６月の重版 | SHURO | シュロ</title></head><body class="template-information">
<div class="text"><p>重版しました。</p></div>
</body></html>
"""

# A work page: the episode listing, newest first, next to unrelated
# `line-box` links and a repeated episode.
WORK_HTML = """
<html><head><title>カッパのカーティと祟りどもの愛 | SHURO | シュロ</title></head><body class="template-manga">
<div class="rowm-8">
  <h3 class="font-sans-serif-thin">エピソード一覧</h3>
  <div class="rowm-2 line-container">
    <a class="db posr line-box" href="https://shuro.world/episode/146210/">
      <img src="https://img.shuro.world/wp-content/uploads/2026/08/kati2_obi.jpg" alt="第１5話">
      <p class="rowm-1"><b>第１5話</b></p><p class="rowm-0_5">「ウエちゃんが撮ったやつ」</p>
    </a>
    <a class="db posr line-box" href="/episode/71005/">
      <p class="rowm-1"><b>第１話</b></p><p class="rowm-0_5">「割れてますよ、頭の皿」</p>
    </a>
    <a class="db posr line-box" href="https://shuro.world/episode/71005/#again"><p><b>第１話</b></p></a>
    <a class="db posr line-box" href="https://shuro.world/publication/kappanokarty_01/"><p><b>単行本</b></p></a>
  </div>
</div>
</body></html>
"""

# A work whose episodes were all put online the same day: the site lists the
# newest two first, then the rest in the order they were entered.
TIED_WORK_HTML = """
<html><body class="template-manga">
<a class="line-box" href="https://shuro.world/episode/139809/"><p><b>第12話</b></p></a>
<a class="line-box" href="https://shuro.world/episode/123333/"><p><b>第11話</b></p></a>
<a class="line-box" href="https://shuro.world/episode/1734/"><p><b>第１話</b></p></a>
<a class="line-box" href="https://shuro.world/episode/16856/"><p><b>第２話</b></p></a>
<a class="line-box" href="https://shuro.world/episode/17683/"><p><b>第３話</b></p></a>
</body></html>
"""

EMPTY_WORK_HTML = """
<html><body class="template-manga">
<h3>エピソード一覧</h3>
<div class="line-container"></div>
<a class="line-box" href="https://shuro.world/publication/something/"><p><b>単行本</b></p></a>
</body></html>
"""


@pytest.fixture
def client(fake_session):
    def make(routes):
        session = fake_session(routes)
        return Shuro(session), session

    return make


# --- URLs ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        EPISODE_URL,
        "https://shuro.world/episode/71005",
        WORK_URL,
        "https://shuro.world/manga/kappanokarty",
        "https://shuro.world/manga/%e5%a4%95%e6%9a%ae%e5%ae%87%e5%ae%99%e8%88%b9/",
    ],
)
def test_suitable_accepts_episode_and_work_urls(url):
    assert Shuro.suitable(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://shuro.world/episode/71005/",
        "https://www.shuro.world/episode/71005/",
        "https://shuro.world/",
        "https://shuro.world/episode/",
        "https://shuro.world/manga/",
        "https://shuro.world/manga/type/natsume/",
        "https://shuro.world/manga/page/2/",
        "https://shuro.world/publication/kappanokarty_01/",
        "https://shuro.world/information/140359/",
        "https://img.shuro.world/wp-content/uploads/2024/08/25210308/kappa_01_01.jpg",
    ],
)
def test_suitable_rejects_other_urls(url):
    assert not Shuro.suitable(url)


def test_is_series_tells_a_work_page_from_an_episode_without_a_request(client):
    shuro, session = client({})
    assert shuro.is_series(WORK_URL)
    assert not shuro.is_series(EPISODE_URL)
    assert session.calls == []


# --- the listing order ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "number"),
    [
        ("第１話", 1),
        ("第１5話", 15),
        ("#23", 23),
        ("session０", 0),
        ("〈２巻・ドイツ編〉第２話", 2),
        ("後編", None),
        ("試し読み", None),
        ("", None),
    ],
)
def test_episode_number_folds_full_width_digits(label, number):
    assert episode_number(label) == number


def test_reading_order_reverses_a_newest_first_listing_without_numbers():
    listing = [("/episode/1199/", "後編"), ("/episode/1115/", "前編")]
    assert reading_order(listing) == ["/episode/1115/", "/episode/1199/"]


def test_reading_order_sorts_by_number_when_every_label_has_a_distinct_one():
    listing = [("/episode/72071/", "第１話"), ("/episode/92862/", "第１４話"), ("/episode/73387/", "第２話")]
    assert reading_order(listing) == ["/episode/72071/", "/episode/73387/", "/episode/92862/"]


def test_reading_order_falls_back_to_reversing_when_a_label_has_no_number():
    listing = [("/episode/3/", "#3"), ("/episode/2/", "寄稿イラスト大公開"), ("/episode/1/", "#1")]
    assert reading_order(listing) == ["/episode/1/", "/episode/2/", "/episode/3/"]


def test_reading_order_falls_back_to_reversing_when_numbers_repeat():
    listing = [
        ("/episode/757/", "〈２巻〉第２話"),
        ("/episode/748/", "〈２巻〉第１話"),
        ("/episode/25837/", "〈１巻〉お試し読み"),
    ]
    assert reading_order(listing) == ["/episode/25837/", "/episode/748/", "/episode/757/"]


# --- mangaData ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "html",
    [
        INFORMATION_HTML,
        "<script>const mangaData = [</script>",
        '<script>const mangaData = {"title": "not a list"};</script>',
        '<script>const mangaData = ["not a work"];</script>',
    ],
)
def test_parse_manga_data_is_empty_without_a_usable_script(html):
    assert parse_manga_data(html) == []


# --- episode --------------------------------------------------------------------------


def test_episode_reads_the_titles_the_pages_and_the_next_episode(client, fake_response):
    shuro, session = client({"/episode/71005/": fake_response(text=EPISODE_HTML)})
    episode = shuro.episode(EPISODE_URL)

    assert episode.url == EPISODE_URL
    assert episode.series_title == "カッパのカーティと祟りどもの愛"
    assert (episode.writer, episode.publisher) == ("宮崎夏次系 (漫画家)", "マガジンハウス")
    assert episode.episode_title == "第１話 「割れてますよ、頭の皿」"
    assert [page.url for page in episode.pages] == [
        "https://img.shuro.world/wp-content/uploads/2025/12/09204919/cover.jpg",
        "https://shuro.world/wp-content/uploads/2024/08/25210308/kappa_01_01.jpg",
        "https://img.shuro.world/wp-content/uploads/2024/08/25210313/kappa_01_02-03-scaled.jpg",
    ]
    assert (episode.pages[0].width, episode.pages[0].height) == (1353, 1920)
    assert (episode.pages[2].width, episode.pages[2].height) == (2560, 1820)
    assert episode.next_url == NEXT_URL
    assert episode.number == 1
    assert episode.readable
    assert episode.metadata["work_url"] == WORK_URL
    assert episode.metadata["episode"]["title"] == "第１話"
    assert episode.metadata["works"][0]["slug"] == "kappanokarty"
    assert "episodes" not in episode.metadata["works"][0]
    assert episode.metadata["images"] == [page.url for page in episode.pages]
    assert "User-Agent" in session.headers_seen[-1]
    assert session.params_seen[-1] is None


def test_episode_is_dated_by_its_first_page_upload(client, fake_response, uploaded):
    shuro, _ = client(
        {
            "/episode/71005/": fake_response(text=EPISODE_HTML),
            "/09204919/cover.jpg": fake_response(b"", headers=uploaded),
        }
    )
    assert shuro.episode(EPISODE_URL).published == date(2025, 8, 21)


def test_episode_matches_itself_in_manga_data_whatever_the_url_spelling(client, fake_response):
    shuro, _ = client({"/episode/71005": fake_response(text=EPISODE_HTML)})
    episode = shuro.episode("https://shuro.world/episode/71005?ref=top")
    assert episode.next_url == NEXT_URL


def test_last_episode_has_no_next(client, fake_response):
    shuro, _ = client({"/episode/146210/": fake_response(text=LAST_EPISODE_HTML)})
    episode = shuro.episode(NEXT_URL)

    assert episode.episode_title == "第１5話 「ウエちゃんが撮ったやつ」"
    assert len(episode.pages) == 1
    assert (episode.prev_url, episode.next_url) == (EPISODE_URL, None)
    assert episode.number == 2


def test_episode_with_an_empty_viewer_has_no_pages(client, fake_response):
    shuro, _ = client({"/episode/99/": fake_response(text=EMPTY_EPISODE_HTML)})
    episode = shuro.episode("https://shuro.world/episode/99/")

    assert episode.series_title == "何か"
    assert episode.episode_title == "第２話"
    assert episode.pages == ()
    assert not episode.readable
    assert episode.next_url is None
    assert episode.metadata["episode"] is None
    assert episode.metadata["works"] == []


def test_page_without_a_viewer_is_not_an_episode(client, fake_response):
    shuro, _ = client({"/information/140359/": fake_response(text=INFORMATION_HTML)})
    with pytest.raises(NotAnEpisodePageError, match="no viewer"):
        shuro.episode("https://shuro.world/information/140359/")


def test_work_page_is_not_an_episode(client, fake_response):
    shuro, _ = client({"/manga/kappanokarty/": fake_response(text=WORK_HTML)})
    with pytest.raises(NotAnEpisodePageError, match="no viewer"):
        shuro.episode(WORK_URL)


def test_gone_episode_is_not_an_episode(client, fake_response):
    shuro, _ = client({"/episode/71006/": fake_response(text="not found", status_code=HTTPStatus.NOT_FOUND)})
    with pytest.raises(NotAnEpisodePageError, match="404"):
        shuro.episode("https://shuro.world/episode/71006/")


def test_other_http_errors_propagate(client, fake_response):
    shuro, _ = client({"/episode/71005/": fake_response(text="", status_code=HTTPStatus.SERVICE_UNAVAILABLE)})
    with pytest.raises(HTTPStatusError):
        shuro.episode(EPISODE_URL)


# --- series ---------------------------------------------------------------------------


def test_series_urls_lists_the_episodes_oldest_first_deduplicated(client, fake_response):
    shuro, session = client({"/manga/kappanokarty/": fake_response(text=WORK_HTML)})
    urls = shuro.series_urls(WORK_URL)

    assert urls == [EPISODE_URL, NEXT_URL]
    assert all(Shuro.suitable(url) for url in urls)
    assert session.calls == [WORK_URL]


def test_series_urls_puts_a_same_day_listing_in_episode_order(client, fake_response):
    shuro, _ = client({"/manga/undertheshurotree/": fake_response(text=TIED_WORK_HTML)})
    assert shuro.series_urls("https://shuro.world/manga/undertheshurotree/") == [
        "https://shuro.world/episode/1734/",
        "https://shuro.world/episode/16856/",
        "https://shuro.world/episode/17683/",
        "https://shuro.world/episode/123333/",
        "https://shuro.world/episode/139809/",
    ]


def test_series_urls_raises_on_an_empty_listing(client, fake_response):
    shuro, _ = client({"/manga/nothing/": fake_response(text=EMPTY_WORK_HTML)})
    with pytest.raises(NotAnEpisodePageError, match="lists no episode"):
        shuro.series_urls("https://shuro.world/manga/nothing/")


def test_series_urls_raises_on_a_gone_work(client, fake_response):
    shuro, _ = client({"/manga/gone/": fake_response(text="not found", status_code=HTTPStatus.NOT_FOUND)})
    with pytest.raises(NotAnEpisodePageError, match="404"):
        shuro.series_urls("https://shuro.world/manga/gone/")


def test_series_urls_refuses_a_url_of_the_wrong_shape(client):
    shuro, session = client({})
    with pytest.raises(UnsupportedUrlError, match="not a work page"):
        shuro.series_urls(EPISODE_URL)
    assert session.calls == []


# --- downloading ----------------------------------------------------------------------


def _jpeg(color):
    raw = BytesIO()
    Image.new("RGB", (6, 8), color).save(raw, "JPEG", quality=100)
    return raw.getvalue()


def test_download_writes_the_first_page_as_served(client, fake_response, tmp_path):
    shuro, session = client(
        {
            "/episode/71005/": fake_response(text=EPISODE_HTML),
            # Flat greys survive the JPEG round trip through the downloader exactly.
            "cover.jpg": fake_response(_jpeg((128, 128, 128)), content_type="image/jpeg"),
            "kappa_01_01.jpg": fake_response(_jpeg((64, 64, 64)), content_type="image/jpeg"),
        },
    )
    result = Downloader(shuro, tmp_path, only_first=True).download(EPISODE_URL)

    assert result.status == "saved"
    assert (
        result.save_dir
        == tmp_path / "shuro.world" / "カッパのカーティと祟りどもの愛" / "第１話 「割れてますよ、頭の皿」"
    )
    written = Image.open(result.save_dir / "0.jpg")
    assert written.size == (6, 8)
    assert written.getpixel((3, 4)) == (128, 128, 128)
    assert not (result.save_dir / "1.jpg").exists()
    # The image is asked for with the episode as Referer, which the CDN does not need but tolerates.
    assert session.calls[-1].endswith("cover.jpg")
    assert session.headers_seen[-1]["Referer"] == EPISODE_URL


# --- logging in -----------------------------------------------------------------------


# --- the real site --------------------------------------------------------------------

# One episode per known host, free to read without an account: the first
# episode of a running work, which stays online next to the newest ones.
TEST_URLS: dict[str, str] = {
    "shuro.world": EPISODE_URL,
}


@pytest.mark.network
@pytest.mark.parametrize("host", TEST_URLS)
def test_site_download(tmp_path, host):
    result = Downloader(Shuro(), tmp_path, only_first=True).download(TEST_URLS[host])
    assert result.status == "saved"
    assert result.episode.series_title == "カッパのカーティと祟りどもの愛"
    assert result.episode.next_url is not None
    assert (result.save_dir / "0.jpg").stat().st_size > 0


@pytest.mark.network
def test_work_page_lists_episodes_oldest_first():
    shuro = Shuro()
    assert shuro.is_series(WORK_URL)
    urls = shuro.series_urls(WORK_URL)
    assert urls[0] == EPISODE_URL
    assert all(Shuro.suitable(url) for url in urls)


@pytest.mark.network
def test_gone_episode_is_not_an_episode_on_the_site():
    with pytest.raises(NotAnEpisodePageError, match="404"):
        Shuro().episode("https://shuro.world/episode/71006/")
