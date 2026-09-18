from __future__ import annotations

from io import BytesIO

import pytest
from PIL import Image

from getjmanga.downloader import Downloader
from getjmanga.extractors.common import NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractors.kirapo import Kirapo
from getjmanga.extractors.porta import parse_ptimg

EPISODE_URL = "https://kirapo.jp/pt/meteor/aroundforty/1017660/viewer"
SPECIAL_URL = "https://kirapo.jp/pt/meteor/aroundforty/2022179/viewer"
NEWEST_URL = "https://kirapo.jp/pt/meteor/aroundforty/2022923/viewer"
SERIES_URL = "https://kirapo.jp/meteor/titles/aroundforty"

SERIES_TITLE = "アラフォー冒険者、伝説となる　～SSランクの娘に強化されたらSSSランクになりました～"

# The reader joins the series and the episode with one plain space, and the
# series itself holds an ideographic one: only the work page can split it.
EPISODE_HTML = f"""
<html><head><title>{SERIES_TITLE} 第1話</title></head><body>
<div id="contents">
  <div id="content" class="pages ptbinb-container" data-binbsp-direction="rtl"
       data-binbsp-recommend="/p/ad_pages/gooad-index-meteor/#gooad[next] /p/ad_pages/aroundforty-meteor/#bl[next]">
    <div data-ptimg="data/0001.ptimg.json" data-binbsp-spread="center"></div>
    <div data-ptimg="data/0002.ptimg.json" data-binbsp-spread="right"></div>
  </div>
</div>
</body></html>
"""

# The work page: newest first, the header repeating two of the links, an
# ended episode without a link, a purchase entry linking other stores, and
# the older half folded away behind もっと見る but still in the HTML.
SERIES_HTML = f"""
<html><head><title>{SERIES_TITLE} / COMICメテオ - きら星ポータル きらポ</title></head><body>
<main>
<h2>{SERIES_TITLE}</h2>
<div class="title-header-area">
  <div class="header-side">
    <div class="fs-6 fw-bold latest-episode-title">第45話</div>
    <div><a class="button-pink episode-read" href="{NEWEST_URL}" data-episode-id="2022923">最新話を読む</a></div>
    <div><a class="button-pink episode-read" href="{EPISODE_URL}" data-episode-id="1017660">第1話を読む</a></div>
  </div>
</div>
<div class="content-container">
  <div class="episodes-container py-3 my-4">
    <div class="episode-item">
      <div class="episode-item-left my-2 my-lg-0"><div class="fw-bold fs-6 me-1">未公開話</div></div>
      <div class="episode-item-right phone-only">
        <span class="button-black episode-item-button purchase-button" data-mid="m0">購入</span>
      </div>
      <div class="episode-item-right phone-disable">
        <a class="button-black episode-item-button" href="https://booklive.jp/product/index/title_id/20028485/vol_no/001">ブックライブで購入</a>
      </div>
    </div>
    <div class="episode-item">
      <div class="episode-item-left my-2 my-lg-0"><div class="fw-bold fs-6 me-1">第45話</div></div>
      <div class="episode-item-right">
        <a id="2022923" class="button-pink episode-item-button episode-read" href="{NEWEST_URL}?utm=x"
           data-episode-id="2022923">読む</a>
      </div>
    </div>
    <div class="episode-item">
      <div class="episode-item-left my-2 my-lg-0"><span>第44話の公開は終了しました。</span></div>
    </div>
    <div id="more-episodes-button-container" class="episode-item py-3">
      <div id="more-episodes-button" class="more-episodes-button">
        <div class="more-episodes-button-text">もっと見る</div>
      </div>
    </div>
    <div class="episode-item hidden-episode d-none">
      <div class="episode-item-left my-2 my-lg-0"><div class="fw-bold fs-6 me-1">第11巻発売直前スペシャル</div></div>
      <div class="episode-item-right">
        <a id="2022179" class="button-pink episode-item-button episode-read"
           href="/pt/meteor/aroundforty/2022179/viewer" data-episode-id="2022179">読む</a>
      </div>
    </div>
    <div class="episode-item hidden-episode d-none">
      <div class="episode-item-left my-2 my-lg-0"><div class="fw-bold fs-6 me-1">同じ話</div></div>
      <div class="episode-item-right">
        <a class="button-pink episode-item-button episode-read" href="{SPECIAL_URL}/" data-episode-id="2022179">読む</a>
      </div>
    </div>
    <div class="episode-item hidden-episode d-none">
      <div class="episode-item-left my-2 my-lg-0"><div class="fw-bold fs-6 me-1">第1話</div></div>
      <div class="episode-item-right">
        <a id="1017660" class="button-pink episode-item-button episode-read" href="{EPISODE_URL}"
           data-episode-id="1017660">読む</a>
      </div>
    </div>
  </div>
</div>
</main>
</body></html>
"""

EMPTY_SERIES_HTML = """
<html><body><main><h2>次回作</h2><div class="episodes-container">
<div class="episode-item"><div class="episode-item-left"><span>第1話の公開は終了しました。</span></div></div>
</div></main></body></html>
"""

# A 2x2 page of 10x10 tiles, spread out with a 2-pixel gutter in the resource.
PTIMG = {
    "ptimg-version": 1,
    "resources": {"i": {"src": "0001.jpg", "width": 24, "height": 24}},
    "views": [
        {
            "width": 20,
            "height": 20,
            "coords": [
                "i:2,2+10,10>10,10",
                "i:14,2+10,10>0,0",
                "i:2,14+10,10>10,0",
                "i:14,14+10,10>0,10",
            ],
        },
    ],
}


def scrambled_resource() -> Image.Image:
    """The resource `PTIMG` describes: four solid tiles, each where a transfer expects it."""
    image = Image.new("RGB", (24, 24), "white")
    for transfer in parse_ptimg(PTIMG, "https://x/data/0001.ptimg.json").transfers:
        colour = (transfer.dest_x * 10, transfer.dest_y * 10, 200)
        tile = Image.new("RGB", (transfer.width, transfer.height), colour)
        image.paste(tile, (transfer.x, transfer.y))
    return image


def png(image: Image.Image) -> bytes:
    out = BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def site(fake_session, fake_response, **overrides):
    """A session scripting the whole site; `overrides` come first, so a narrower route can win."""
    defaults = {
        "/pt/meteor/aroundforty/1017660/viewer": fake_response(text=EPISODE_HTML),
        "/meteor/titles/aroundforty": fake_response(text=SERIES_HTML),
    }
    return fake_session({**overrides, **{key: value for key, value in defaults.items() if key not in overrides}})


# --- URLs ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        EPISODE_URL,
        f"{EPISODE_URL}/",
        "https://kirapo.jp/pt/polaris/kiseizizitsukon/2022932/viewer",
        "https://kirapo.jp/pt/etoile/tuihouseijyo/2022728/viewer",
        "https://kirapo.jp/pt/ambre/kitakazetotaiyou/2022701/viewer",
        "https://kirapo.jp/pt/astir/usodemokoini/2022707/viewer",
        "https://kirapo.jp/pt/zulet/akogarenoouji/2021755/viewer",
        SERIES_URL,
        f"{SERIES_URL}/",
        "https://kirapo.jp/polaris/titles/doukyo",
    ],
)
def test_suitable_accepts_reader_and_work_pages_of_every_imprint(url):
    assert Kirapo.suitable(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://kirapo.jp/pt/meteor/aroundforty/1017660/viewer",
        "https://www.kirapo.jp/pt/meteor/aroundforty/1017660/viewer",
        "https://comic-meteor.jp/aroundforty/",
        "https://comic-polaris.jp/ptdata/doukyo/0001/",
        "https://kirapo.jp/",
        "https://kirapo.jp/meteor",
        "https://kirapo.jp/titles",
        "https://kirapo.jp/titles?genre=1000024",
        "https://kirapo.jp/authors/2000586",
        "https://kirapo.jp/pt/meteor/aroundforty/1017660",
        "https://kirapo.jp/pt/meteor/aroundforty/1017660/data/0001.ptimg.json",
        "https://kirapo.jp/p/ad_pages/gooad-index-meteor/",
    ],
)
def test_suitable_rejects_other_urls(url):
    assert not Kirapo.suitable(url)


def test_is_series_only_for_a_work_page():
    assert Kirapo.is_series(SERIES_URL)
    assert Kirapo.is_series("https://kirapo.jp/polaris/titles/doukyo/")
    assert not Kirapo.is_series(EPISODE_URL)
    assert not Kirapo.is_series("https://kirapo.jp/titles")


# --- episode ------------------------------------------------------------------------


def test_episode_reads_the_titles_the_pages_and_the_next_episode(fake_session, fake_response):
    session = site(fake_session, fake_response)
    episode = Kirapo(session).episode(EPISODE_URL)

    assert episode.url == EPISODE_URL
    assert episode.series_title == SERIES_TITLE
    assert episode.episode_title == "第1話"
    assert [page.url for page in episode.pages] == [
        "https://kirapo.jp/pt/meteor/aroundforty/1017660/data/0001.ptimg.json",
        "https://kirapo.jp/pt/meteor/aroundforty/1017660/data/0002.ptimg.json",
    ]
    assert [page.extra["spread"] for page in episode.pages] == ["center", "right"]
    assert episode.next_url == SPECIAL_URL
    assert episode.metadata["direction"] == "rtl"
    assert episode.metadata["recommend"].startswith("/p/ad_pages/")
    assert (episode.metadata["imprint"], episode.metadata["slug"], episode.metadata["episode_id"]) == (
        "meteor",
        "aroundforty",
        "1017660",
    )
    assert episode.metadata["ptimg"] == [page.url for page in episode.pages]
    # The reader, then the work page its URL names.
    assert session.calls == [EPISODE_URL, SERIES_URL]
    assert session.headers_seen[0]["User-Agent"]


def test_episode_takes_the_episode_name_from_the_work_page(fake_session, fake_response):
    html = EPISODE_HTML.replace("第1話</title>", "第11巻発売直前スペシャル</title>")
    session = site(fake_session, fake_response, **{"/2022179/viewer": fake_response(text=html)})
    episode = Kirapo(session).episode(f"{SPECIAL_URL}/")

    assert (episode.series_title, episode.episode_title) == (SERIES_TITLE, "第11巻発売直前スペシャル")
    assert episode.next_url == NEWEST_URL


def test_last_episode_has_no_next(fake_session, fake_response):
    session = site(fake_session, fake_response, **{"/2022923/viewer": fake_response(text=EPISODE_HTML)})
    episode = Kirapo(session).episode(NEWEST_URL)

    assert episode.episode_title == "第45話"
    assert episode.next_url is None


def test_episode_survives_a_broken_work_page(fake_session, fake_response):
    session = site(fake_session, fake_response, **{"/meteor/titles/aroundforty": fake_response(status_code=500)})
    episode = Kirapo(session).episode(EPISODE_URL)

    assert episode.readable
    # Without the work page the reader's `<title>` is split on its ideographic space, wrongly but readably.
    assert episode.series_title == "アラフォー冒険者、伝説となる"
    assert episode.episode_title == "～SSランクの娘に強化されたらSSSランクになりました～ 第1話"
    assert episode.next_url is None


def test_episode_not_in_the_listing_keeps_the_reader_title(fake_session, fake_response):
    session = site(fake_session, fake_response, **{"/9999999/viewer": fake_response(text=EPISODE_HTML)})
    episode = Kirapo(session).episode("https://kirapo.jp/pt/meteor/aroundforty/9999999/viewer")

    assert (episode.series_title, episode.episode_title) == (SERIES_TITLE, "第1話")
    assert episode.next_url is None


def test_locked_episode_has_no_pages(fake_session, fake_response):
    html = EPISODE_HTML.replace(
        '<div data-ptimg="data/0001.ptimg.json" data-binbsp-spread="center"></div>', ""
    ).replace(
        '<div data-ptimg="data/0002.ptimg.json" data-binbsp-spread="right"></div>',
        "",
    )
    session = site(fake_session, fake_response, **{"/1017660/viewer": fake_response(text=html)})
    episode = Kirapo(session).episode(EPISODE_URL)

    assert not episode.readable
    assert episode.pages == ()
    assert episode.next_url == SPECIAL_URL


def test_expired_episode_is_gone(fake_session, fake_response):
    session = fake_session({"/pt/": fake_response(status_code=404, text="<html>not found</html>")})
    with pytest.raises(NotAnEpisodePageError, match="404"):
        Kirapo(session).episode("https://kirapo.jp/pt/meteor/aroundforty/2022500/viewer")


def test_page_without_a_reader_is_not_an_episode(fake_session, fake_response):
    session = fake_session({"/meteor/titles/": fake_response(text=SERIES_HTML)})
    with pytest.raises(NotAnEpisodePageError, match="no SpeedBinb reader"):
        Kirapo(session).episode(SERIES_URL)


def test_other_http_errors_propagate(fake_session, fake_response):
    session = fake_session({"/pt/": fake_response(status_code=503)})
    with pytest.raises(Exception, match="503"):
        Kirapo(session).episode(EPISODE_URL)


# --- series ------------------------------------------------------------------------


def test_series_urls_lists_the_linked_episodes_oldest_first(fake_session, fake_response):
    session = fake_session({"/meteor/titles/aroundforty": fake_response(text=SERIES_HTML)})
    urls = Kirapo(session).series_urls(SERIES_URL)

    # The header links are ignored; the query and a trailing slash are dropped;
    # a repeated link counts once; an ended episode and a purchase entry have
    # no reader to list.
    assert urls == [EPISODE_URL, SPECIAL_URL, NEWEST_URL]
    assert all(Kirapo.suitable(url) for url in urls)


def test_series_urls_raises_on_an_empty_listing(fake_session, fake_response):
    session = fake_session({"/titles/": fake_response(text=EMPTY_SERIES_HTML)})
    with pytest.raises(NotAnEpisodePageError, match="no readable episode"):
        Kirapo(session).series_urls("https://kirapo.jp/polaris/titles/jikai/")


def test_series_urls_refuses_an_episode_url(fake_session, fake_response):
    with pytest.raises(UnsupportedUrlError):
        Kirapo(fake_session({})).series_urls(EPISODE_URL)


# --- download ------------------------------------------------------------------------


def test_image_puts_the_page_together_through_porta(fake_session, fake_response):
    session = site(
        fake_session,
        fake_response,
        **{
            "/data/0001.ptimg.json": fake_response(payload=PTIMG, content_type="application/json"),
            "/data/0001.jpg": fake_response(png(scrambled_resource()), content_type="image/png"),
        },
    )
    extractor = Kirapo(session)
    episode = extractor.episode(EPISODE_URL)

    page = extractor.image(episode.pages[0], episode)

    assert page.size == (20, 20)
    assert page.getpixel((5, 5)) == (0, 0, 200)  # the tile the second transfer moved to (0, 0)
    assert page.getpixel((15, 15)) == (100, 100, 200)  # the tile the first transfer moved to (10, 10)
    # The JSON and the image were asked for with the episode as Referer.
    assert session.calls[-2:] == [
        "https://kirapo.jp/pt/meteor/aroundforty/1017660/data/0001.ptimg.json",
        "https://kirapo.jp/pt/meteor/aroundforty/1017660/data/0001.jpg",
    ]
    for headers in session.headers_seen[-2:]:
        assert headers["Referer"] == EPISODE_URL


# --- the real site --------------------------------------------------------------------

# One episode per known host, free to read without an account: the first
# episode of a long-running series, kept online while the series runs.
TEST_URLS: dict[str, str] = {
    "kirapo.jp": "https://kirapo.jp/pt/meteor/aroundforty/1017660/viewer",
}


@pytest.mark.network
@pytest.mark.parametrize("host", TEST_URLS)
def test_site_download(tmp_path, host):
    result = Downloader(Kirapo(), tmp_path, only_first=True).download(TEST_URLS[host])
    assert result.status == "saved"
    assert (result.save_dir / "0.jpg").stat().st_size > 0


@pytest.mark.network
def test_work_page_lists_episodes():
    urls = Kirapo().series_urls("https://kirapo.jp/meteor/titles/aroundforty")
    assert TEST_URLS["kirapo.jp"] in urls
    assert all(Kirapo.suitable(url) for url in urls)


@pytest.mark.network
def test_polaris_imprint_reads_too():
    episode = Kirapo().episode("https://kirapo.jp/pt/polaris/kiseizizitsukon/2022932/viewer")
    assert episode.readable
    assert episode.metadata["imprint"] == "polaris"
