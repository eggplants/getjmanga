from __future__ import annotations

from datetime import date
from io import BytesIO

import pytest
from PIL import Image

from getjmanga.downloader import Downloader
from getjmanga.errors import NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractors.porta import Porta
from getjmanga.viewers.speedbinb import parse_ptimg

EPISODE_URL = "https://comic-porta.com/p_data/ol_ningyo001al/"
SERIES_URL = "https://comic-porta.com/series/7981/"
RECOMMEND_URL = "https://comic-porta.com/comic_data_temp/ol_ningyo/001.html"

EPISODE_HTML = f"""
<html><head><title>OLと人魚　OLと人魚</title></head><body>
<div id="content_base">
  <div id="content" class="pages ptbinb-container" data-binbsp-direction="rtl"
       data-binbsp-recommend="{RECOMMEND_URL}#AD_a[next] {RECOMMEND_URL}#AD_b[next]">
    <div data-ptimg="data/0001.ptimg.json" data-binbsp-spread="left"></div>
    <div data-ptimg="data/0002.ptimg.json" data-binbsp-spread="right"></div>
  </div>
</div>
</body></html>
"""

# The frame shown past the last page. Its `次の話へ` button is hand-written
# and stale (it points at another work), only `作品詳細` is trusted.
RECOMMEND_HTML = """
<html><body>
<div id="AD_a"><a href="/series/251/"><img src="banner.jpg"></a></div>
<div id="AD_b">
  <div class="singleimage-linkbutton next"><a href="/p_data/akuno_neko020/">次の話へ</a></div>
  <div class="singleimage-linkbutton detail"><a href="/series/7981/" target="_blank">作品詳細</a></div>
</div>
</body></html>
"""

SERIES_HTML = """
<html><head><title>OLと人魚｜COMIC ポルタ｜イースト・プレス</title></head><body>
<h2 class="title">OLと人魚</h2>
<p class="authors">司馬舞</p>
<div class="series-pickup"><ul>
  <li class="pickup1"><a href="https://comic-porta.com/p_data/ol_ningyo002ns/">newest</a></li>
  <li class="pickup2"><a href="https://comic-porta.com/p_data/ol_ningyo001al/">older</a></li>
</ul></div>
<section id="backnumber">
  <dl class="bn-list"><dt class="list-title">1～</dt><dd><ul class="episode-list">
    <li class="episode"><p class="update">2025年5月23日 更新</p><p class="title">OLと人魚</p>
      <p class="episode-btn"><a href="https://comic-porta.com/p_data/ol_ningyo001al/?utm=x"><span>無料版</span></a></p></li>
    <li class="episode"><p class="update">2025年6月13日 更新</p><p class="title">ゆびきりげんまん</p>
      <p class="episode-btn"><a href="https://comic-porta.com/p_data/ol_ningyo002ns/"><span>無料版</span></a></p></li>
    <li class="episode"><p class="title">おわった話</p><p class="note">無料公開は終了しました</p></li>
    <li class="episode"><p class="title">同じ話</p>
      <p class="episode-btn"><a href="/p_data/ol_ningyo002ns/"><span>無料版</span></a></p></li>
    <li class="episode"><p class="title">期間限定</p>
      <p class="episode-btn"><a href="https://comic-porta.com/p_data/ol_ningyo009xx/"><span>期間限定</span></a></p></li>
  </ul></dd></dl>
</section>
</body></html>
"""

EMPTY_SERIES_HTML = """
<html><body><h2 class="title">勇者の運命</h2>
<section id="backnumber"><ul class="episode-list"></ul></section></body></html>
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


def jpeg(image: Image.Image) -> bytes:
    out = BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


# --- URLs ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        EPISODE_URL,
        "https://comic-porta.com/p_data/ol_ningyo001al",
        SERIES_URL,
        "https://comic-porta.com/series/7981",
    ],
)
def test_suitable_accepts_episode_and_series_urls(url):
    assert Porta.suitable(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://comic-porta.com/p_data/ol_ningyo001al/",
        "https://www.comic-porta.com/p_data/ol_ningyo001al/",
        "https://www.splush.jp/series/14716/",
        "https://comic-porta.com/",
        "https://comic-porta.com/series/",
        "https://comic-porta.com/news/",
        "https://comic-porta.com/p_data/ol_ningyo001al/data/0001.ptimg.json",
        "https://comic-porta.com/comic_data_temp/ol_ningyo/001.html",
    ],
)
def test_suitable_rejects_other_urls(url):
    assert not Porta.suitable(url)


def test_is_series_only_for_a_work_page():
    assert Porta.is_series(SERIES_URL)
    assert Porta.is_series("https://comic-porta.com/series/7981")
    assert not Porta.is_series(EPISODE_URL)
    assert not Porta.is_series("https://comic-porta.com/series/")


# --- episode ------------------------------------------------------------------------


def site(fake_session, fake_response, **overrides):
    """A session scripting the whole site; `overrides` come first, so a narrower route can win."""
    defaults = {
        "/p_data/ol_ningyo001al/": fake_response(text=EPISODE_HTML),
        "/comic_data_temp/": fake_response(text=RECOMMEND_HTML),
        "/series/7981/": fake_response(text=SERIES_HTML),
    }
    return fake_session({**overrides, **{key: value for key, value in defaults.items() if key not in overrides}})


def test_episode_reads_the_titles_the_pages_and_the_next_episode(fake_session, fake_response):
    session = site(fake_session, fake_response)
    episode = Porta(session).episode(EPISODE_URL)

    assert episode.url == EPISODE_URL
    assert episode.series_title == "OLと人魚"
    assert (episode.writer, episode.publisher) == ("司馬舞", "イースト・プレス")
    assert (episode.published, episode.number) == (date(2025, 5, 23), 1)
    assert episode.episode_title == "OLと人魚"
    assert [page.url for page in episode.pages] == [
        "https://comic-porta.com/p_data/ol_ningyo001al/data/0001.ptimg.json",
        "https://comic-porta.com/p_data/ol_ningyo001al/data/0002.ptimg.json",
    ]
    assert [page.extra["spread"] for page in episode.pages] == ["left", "right"]
    assert episode.next_url == "https://comic-porta.com/p_data/ol_ningyo002ns/"
    assert episode.metadata["direction"] == "rtl"
    assert episode.metadata["ptimg"] == [page.url for page in episode.pages]
    # The frame past the last page, then the work page it names.
    assert session.calls == [EPISODE_URL, RECOMMEND_URL, SERIES_URL]


def test_episode_takes_the_series_title_from_the_work_page(fake_session, fake_response):
    html = EPISODE_HTML.replace("<title>OLと人魚　OLと人魚</title>", "<title>OLと人魚 ゆびきりげんまん</title>")
    session = site(fake_session, fake_response, **{"/p_data/ol_ningyo001al/": fake_response(text=html)})
    episode = Porta(session).episode(EPISODE_URL)

    assert (episode.series_title, episode.episode_title) == ("OLと人魚", "ゆびきりげんまん")


def test_last_episode_has_no_next(fake_session, fake_response):
    html = SERIES_HTML.replace('<a href="https://comic-porta.com/p_data/ol_ningyo009xx/">', '<a href="/">')
    session = site(
        fake_session,
        fake_response,
        **{"/series/7981/": fake_response(text=html), "/p_data/ol_ningyo002ns/": fake_response(text=EPISODE_HTML)},
    )
    episode = Porta(session).episode("https://comic-porta.com/p_data/ol_ningyo002ns/")
    assert (episode.prev_url, episode.next_url) == (EPISODE_URL, None)


def test_episode_without_a_recommend_frame_still_reads(fake_session, fake_response):
    html = EPISODE_HTML.replace(f'data-binbsp-recommend="{RECOMMEND_URL}#AD_a[next] {RECOMMEND_URL}#AD_b[next]"', "")
    session = fake_session({"/p_data/": fake_response(text=html)})
    episode = Porta(session).episode(EPISODE_URL)

    assert (episode.series_title, episode.episode_title) == ("OLと人魚", "OLと人魚")
    assert len(episode.pages) == 2
    assert episode.next_url is None
    assert session.calls == [EPISODE_URL]


@pytest.mark.parametrize(
    "frame",
    [
        # The frame is gone.
        "missing",
        # The frame has no link back to the work page, or one that is not a work page.
        "<html><body><a href='/series/251/'>bn</a><a href='/news/'>作品詳細</a></body></html>",
    ],
)
def test_episode_without_a_usable_frame_has_no_next(fake_session, fake_response, frame):
    response = fake_response(status_code=404) if frame == "missing" else fake_response(text=frame)
    session = site(fake_session, fake_response, **{"/comic_data_temp/": response})
    episode = Porta(session).episode(EPISODE_URL)

    assert episode.next_url is None
    assert SERIES_URL not in session.calls


def test_episode_survives_a_broken_work_page(fake_session, fake_response):
    session = site(fake_session, fake_response, **{"/series/7981/": fake_response(status_code=500)})
    episode = Porta(session).episode(EPISODE_URL)

    assert episode.readable
    assert episode.next_url is None


def test_locked_episode_has_no_pages(fake_session, fake_response):
    html = EPISODE_HTML.replace('<div data-ptimg="data/0001.ptimg.json" data-binbsp-spread="left"></div>', "").replace(
        '<div data-ptimg="data/0002.ptimg.json" data-binbsp-spread="right"></div>',
        "",
    )
    session = site(fake_session, fake_response, **{"/p_data/ol_ningyo001al/": fake_response(text=html)})
    episode = Porta(session).episode(EPISODE_URL)

    assert not episode.readable
    assert episode.pages == ()
    assert episode.next_url == "https://comic-porta.com/p_data/ol_ningyo002ns/"


def test_expired_episode_is_gone(fake_session, fake_response):
    session = fake_session({"/p_data/": fake_response(status_code=404, text="<html>not found</html>")})
    with pytest.raises(NotAnEpisodePageError, match="404"):
        Porta(session).episode("https://comic-porta.com/p_data/tonarinoyokaisan004/")


def test_page_without_a_reader_is_not_an_episode(fake_session, fake_response):
    session = fake_session({"/series/": fake_response(text=SERIES_HTML)})
    with pytest.raises(NotAnEpisodePageError, match="no SpeedBinb reader"):
        Porta(session).episode(SERIES_URL)


def test_other_http_errors_propagate(fake_session, fake_response):
    session = fake_session({"/p_data/": fake_response(status_code=503)})
    with pytest.raises(Exception, match="503"):
        Porta(session).episode(EPISODE_URL)


# --- series ------------------------------------------------------------------------


def test_series_urls_lists_the_linked_episodes_oldest_first(fake_session, fake_response):
    session = fake_session({"/series/7981/": fake_response(text=SERIES_HTML)})
    urls = Porta(session).series_urls(SERIES_URL)

    # The pickup block (newest first) is ignored; the query is dropped; a
    # repeated link counts once; an expired episode has no link to list.
    assert urls == [
        "https://comic-porta.com/p_data/ol_ningyo001al/",
        "https://comic-porta.com/p_data/ol_ningyo002ns/",
        "https://comic-porta.com/p_data/ol_ningyo009xx/",
    ]
    assert all(Porta.suitable(url) for url in urls)


def test_series_urls_raises_on_an_empty_listing(fake_session, fake_response):
    session = fake_session({"/series/": fake_response(text=EMPTY_SERIES_HTML)})
    with pytest.raises(NotAnEpisodePageError, match="no readable episode"):
        Porta(session).series_urls("https://comic-porta.com/series/8172/")


def test_series_urls_refuses_an_episode_url(fake_session, fake_response):
    with pytest.raises(UnsupportedUrlError):
        Porta(fake_session({})).series_urls(EPISODE_URL)


# --- download ------------------------------------------------------------------------


def test_download_writes_the_descrambled_first_page(fake_session, fake_response, tmp_path):
    session = site(
        fake_session,
        fake_response,
        **{
            "/data/0001.ptimg.json": fake_response(payload=PTIMG, content_type="application/json"),
            "/data/0001.jpg": fake_response(jpeg(scrambled_resource()), content_type="image/png"),
        },
    )
    result = Downloader(Porta(session), tmp_path, only_first=True).download(EPISODE_URL)

    assert result.status == "saved"
    assert result.save_dir == tmp_path / "comic-porta.com" / "OLと人魚" / "OLと人魚"
    with Image.open(result.save_dir / "0.jpg") as page:
        assert page.size == (20, 20)
        # JPEG blurs the edges a little; the tile centres are exact enough.
        top_left = page.getpixel((5, 5))
        bottom_right = page.getpixel((15, 15))
        assert isinstance(top_left, tuple)
        assert isinstance(bottom_right, tuple)
        assert top_left[2] > 150
        assert bottom_right[0] > 80
    # The JSON and the image were asked for with the episode as Referer.
    for headers in session.headers_seen[-2:]:
        assert headers["Referer"] == EPISODE_URL


# --- the real site --------------------------------------------------------------------

# One episode per known host, free to read without an account.
TEST_URLS: dict[str, str] = {
    "comic-porta.com": "https://comic-porta.com/p_data/ol_ningyo001al/",
}


@pytest.mark.network
@pytest.mark.parametrize("host", TEST_URLS)
def test_site_download(tmp_path, host):
    result = Downloader(Porta(), tmp_path, only_first=True).download(TEST_URLS[host])
    assert result.status == "saved"
    assert (result.save_dir / "0.jpg").stat().st_size > 0
    assert result.episode.next_url == "https://comic-porta.com/p_data/ol_ningyo002ns/"


@pytest.mark.network
def test_work_page_lists_episodes():
    urls = Porta().series_urls("https://comic-porta.com/series/7981/")
    assert urls[0] == "https://comic-porta.com/p_data/ol_ningyo001al/"
    assert all(Porta.suitable(url) for url in urls)


@pytest.mark.network
def test_expired_episode_is_gone_on_the_site():
    with pytest.raises(NotAnEpisodePageError):
        Porta().episode("https://comic-porta.com/p_data/tonarinoyokaisan004/")
