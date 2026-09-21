from __future__ import annotations

from io import BytesIO

import pytest
from PIL import Image

from getjmanga.downloader import Downloader
from getjmanga.errors import NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractors.hifumi import Hifumi, work_url
from getjmanga.viewers.speedbinb import parse_ptimg

EPISODE_URL = "https://www.123hon.com/vw/mujintou_reijo/sv_pt00069b2277ab77a1_01/"
NEXT_URL = "https://www.123hon.com/vw/mujintou_reijo/sv_pt0006a8f9e8730428_09/"
RECOMMEND_URL = f"{EPISODE_URL}recommend/index.html"
SERIES_URL = "https://www.123hon.com/polca/web-comic/mujintou_reijo/"
NOVA_EPISODE_URL = "https://www.123hon.com/vw/deathgamer/sv_pt0006a90f5914e545_01/"
NOVA_SERIES_URL = "https://www.123hon.com/nova/web-comic/deathgamer/"

EPISODE_HTML = """
<html><head><title>ワタシ悪役令嬢、いま無人島にいるの。……と思ったけどチート王子住んでた。</title></head><body>
<div id="content_base">
  <div id="content" class="pages ptbinb-container" data-binbsp-direction="rtl"
       data-binbsp-recommend="recommend/index.html#recommend[next]">
    <div data-ptimg="data/0001.ptimg.json" data-binbsp-spread="center"></div>
    <div data-ptimg="data/0002.ptimg.json" data-binbsp-spread="right"></div>
  </div>
</div>
</body></html>
"""

# The frame shown past the last page: the way back to the work page.
RECOMMEND_HTML = """
<html><body><div id="recommend"><div class="r-inner">
  <div class="r-title">ワタシ悪役令嬢、いま無人島にいるの。……と思ったけどチート王子住んでた。</div>
  <div class="r-linkbutton r-main"><a href="https://www.123hon.com/polca/web-comic/mujintou_reijo/">シリーズ一覧</a></div>
  <div class="r-linkbutton r-sub"><a href="https://www.123hon.com/polca/">コミックポルカ</a></div>
</div></div></body></html>
"""

# An older export's frame: the retired per-imprint host, the old work page shape, a hand-written next link.
OLD_RECOMMEND_HTML = """
<html><body><div id="recommend">
  <div class="r-linkbutton r-main"><a href="https://polca.123hon.com/book_series/mujintou_reijo/">シリーズ一覧</a></div>
  <div class="r-linkbutton r-sub"><a href="https://polca.123hon.com/vw/ankn/sv_pt0005f3f950480957_ankn01/index.html">第1話</a></div>
</div></body></html>
"""

SERIES_HTML = """
<html><head>
<title>ワタシ悪役令嬢、いま無人島にいるの。……と思ったけどチート王子住んでた。 ｜ コミックポルカ</title>
</head><body>
<div id="contents" class="web-comic"><div class="inner">
  <div class="title-area">
    <h2 class="center">ワタシ悪役令嬢、いま無人島にいるの。……と思ったけどチート王子住んでた。</h2>
    <p class="comic-copyright">©SANKYO ©puri ©oneko</p>
  </div>
  <div class="detail"><ul class="read-story">
    <li><a href="https://www.123hon.com/vw/mujintou_reijo/sv_pt0006a8f9e8730428_09"
           target="_blank">最新話を読む</a></li>
    <li><a href="https://www.123hon.com/vw/mujintou_reijo/sv_pt00069b2277ab77a1_01"
           target="_blank">第１話を読む</a></li>
  </ul></div>
  <div class="reading-list"><ul class="item-list"><div class="read-episode">
    <li>
      <div class="story"><p><span class="story-num">第1話</span>　2026年03月13日更新</p></div>
      <div class="btn"><a href="https://www.123hon.com/vw/mujintou_reijo/sv_pt00069b2277ab77a1_01"
                           target="_blank">読む</a></div>
    </li>
    <li>
      <div class="thumbnail" style="opacity:0.4;"></div>
      <div class="story"><p><span class="story-num">第2話</span>　公開終了しました。</p></div>
      <div class="btn"><a href="#comics-store">購入</a></div>
    </li>
    <li>
      <div class="story"><p><span class="story-num">第1話（再掲）</span>　2026年03月13日更新</p></div>
      <div class="btn"><a href="/vw/mujintou_reijo/sv_pt00069b2277ab77a1_01/index.html" target="_blank">読む</a></div>
    </li>
    <li>
      <div class="story"><p><span class="story-num">第9話</span>　2026年08月28日更新</p></div>
      <div class="btn"><a href="https://www.123hon.com/vw/mujintou_reijo/sv_pt0006a8f9e8730428_09"
                           target="_blank">読む</a></div>
    </li>
  </div>
  <!-- <li class="last-story-list">
    <div class="btn"><a href="https://www.123hon.com/vw/mujintou_reijo/sv_pt0006ffffffffffff_99">読む</a></div>
  </li> -->
  </ul></div>
</div></div>
</body></html>
"""

EMPTY_SERIES_HTML = """
<html><body><div class="title-area"><h2 class="center">鬼畜英雄</h2></div>
<ul class="item-list"><div class="read-episode">
  <li><div class="story"><p><span class="story-num">第1話</span>　公開終了しました。</p></div>
      <div class="btn"><a href="#comics-store">購入</a></div></li>
</div></ul></body></html>
"""

NOT_A_WORK_HTML = "<html><head><title>ページが見つかりません</title></head><body><h1>404</h1></body></html>"

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
    """A session scripting the whole site; `overrides` come first, so a narrower route can win.

    The frame lives under the episode directory, so its route stays ahead of
    any episode route, overridden or not.
    """
    routes = {"/recommend/index.html": overrides.pop("/recommend/index.html", fake_response(text=RECOMMEND_HTML))}
    routes.update(overrides)
    defaults = {
        "/vw/mujintou_reijo/": fake_response(text=EPISODE_HTML),
        "/polca/web-comic/mujintou_reijo/": fake_response(text=SERIES_HTML),
    }
    routes.update({key: value for key, value in defaults.items() if key not in routes})
    return fake_session(routes)


# --- URLs ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        EPISODE_URL,
        "https://www.123hon.com/vw/mujintou_reijo/sv_pt00069b2277ab77a1_01",
        "https://www.123hon.com/vw/ankn/sv_pt0005f3f71ca576ec_ankn00/index.html",
        SERIES_URL,
        "https://www.123hon.com/polca/web-comic/mujintou_reijo",
        NOVA_SERIES_URL,
        "https://www.123hon.com/polca/book_series/gadegade/",
    ],
)
def test_suitable_accepts_episode_and_work_urls(url):
    assert Hifumi.suitable(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://www.123hon.com/vw/mujintou_reijo/sv_pt00069b2277ab77a1_01/",
        "https://123hon.com/vw/mujintou_reijo/sv_pt00069b2277ab77a1_01/",
        "https://polca.123hon.com/book_series/gadegade/",
        "https://www.123hon.com/",
        "https://www.123hon.com/polca/",
        "https://www.123hon.com/polca/web-comic/",
        "https://www.123hon.com/polca/comics/",
        "https://www.123hon.com/polca/web-comic-item/yuigonjo_15/",
        "https://www.123hon.com/hifumi/web-comic/something/",
        "https://www.123hon.com/vw/mujintou_reijo/",
        "https://www.123hon.com/vw/mujintou_reijo/sv_pt00069b2277ab77a1_01/data/0001.ptimg.json",
        "https://www.123hon.com/vw/mujintou_reijo/sv_pt00069b2277ab77a1_01/recommend/index.html",
        "https://comic-porta.com/p_data/ol_ningyo001al/",
    ],
)
def test_suitable_rejects_other_urls(url):
    assert not Hifumi.suitable(url)


def test_is_series_only_for_a_work_page():
    assert Hifumi.is_series(SERIES_URL)
    assert Hifumi.is_series(NOVA_SERIES_URL)
    assert Hifumi.is_series("https://www.123hon.com/polca/web-comic/mujintou_reijo")
    assert not Hifumi.is_series(EPISODE_URL)
    assert not Hifumi.is_series("https://www.123hon.com/polca/web-comic/")
    assert not Hifumi.is_series("https://polca.123hon.com/book_series/gadegade/")


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (SERIES_URL, SERIES_URL),
        ("https://www.123hon.com/polca/web-comic/mujintou_reijo", SERIES_URL),
        ("https://www.123hon.com/polca/book_series/mujintou_reijo/", SERIES_URL),
        ("https://www.123hon.com/nova/web-comic/deathgamer", NOVA_SERIES_URL),
        # The retired per-imprint hosts, as older recommend frames still link them.
        ("https://polca.123hon.com/book_series/mujintou_reijo/", SERIES_URL),
        ("https://nova.123hon.com/web-comic/deathgamer/", NOVA_SERIES_URL),
        ("https://polca.123hon.com/", None),
        ("https://polca.123hon.com/vw/ankn/sv_pt0005f3f950480957_ankn01/index.html", None),
        ("https://hifumi.123hon.com/book_series/x/", None),
        ("https://123hon.com/book_series/x/", None),
        ("https://www.123hon.com/polca/", None),
        ("https://www.123hon.com/hifumi/web-comic/x/", None),
        (EPISODE_URL, None),
        ("not a url", None),
    ],
)
def test_work_url_canonicalises(url, expected):
    assert work_url(url) == expected


# --- work page ------------------------------------------------------------------------


# --- episode ------------------------------------------------------------------------


def test_episode_reads_the_titles_the_pages_and_the_next_episode(fake_session, fake_response):
    session = site(fake_session, fake_response)
    episode = Hifumi(session).episode("https://www.123hon.com/vw/mujintou_reijo/sv_pt00069b2277ab77a1_01")

    assert episode.url == EPISODE_URL
    assert episode.series_title == "ワタシ悪役令嬢、いま無人島にいるの。……と思ったけどチート王子住んでた。"
    assert (episode.writer, episode.publisher) == ("", "一二三書房")
    assert episode.episode_title == "第1話"
    assert [page.url for page in episode.pages] == [
        f"{EPISODE_URL}data/0001.ptimg.json",
        f"{EPISODE_URL}data/0002.ptimg.json",
    ]
    assert [page.extra["spread"] for page in episode.pages] == ["center", "right"]
    assert episode.next_url == NEXT_URL
    assert episode.metadata["direction"] == "rtl"
    assert episode.metadata["slug"] == "mujintou_reijo"
    assert episode.metadata["viewer_id"] == "sv_pt00069b2277ab77a1_01"
    assert episode.metadata["work_url"] == SERIES_URL
    assert episode.metadata["listed"] is True
    assert episode.metadata["ptimg"] == [page.url for page in episode.pages]
    # The canonical directory, the frame past the last page, then the work page it names.
    assert session.calls == [EPISODE_URL, RECOMMEND_URL, SERIES_URL]


def test_last_episode_has_no_next(fake_session, fake_response):
    session = site(fake_session, fake_response)
    episode = Hifumi(session).episode(NEXT_URL)

    assert episode.episode_title == "第9話"
    assert (episode.prev_url, episode.next_url) == (EPISODE_URL, None)


def test_episode_follows_an_old_recommend_frame_to_the_current_work_page(fake_session, fake_response):
    session = site(fake_session, fake_response, **{"/recommend/index.html": fake_response(text=OLD_RECOMMEND_HTML)})
    episode = Hifumi(session).episode(EPISODE_URL)

    assert episode.episode_title == "第1話"
    assert episode.next_url == NEXT_URL
    assert session.calls[-1] == SERIES_URL


def test_unlisted_episode_is_named_by_its_id(fake_session, fake_response):
    # An expired episode's directory stays up, but the work page no longer links it.
    url = "https://www.123hon.com/vw/mujintou_reijo/sv_pt0006ffffffffffff_02/"
    session = site(fake_session, fake_response)
    episode = Hifumi(session).episode(url)

    assert episode.readable
    assert episode.series_title == "ワタシ悪役令嬢、いま無人島にいるの。……と思ったけどチート王子住んでた。"
    assert episode.episode_title == "sv_pt0006ffffffffffff_02"
    assert episode.next_url is None
    assert episode.metadata["listed"] is False


def test_episode_without_a_recommend_frame_still_reads(fake_session, fake_response):
    html = EPISODE_HTML.replace('data-binbsp-recommend="recommend/index.html#recommend[next]"', "")
    session = fake_session({"/vw/": fake_response(text=html)})
    episode = Hifumi(session).episode(EPISODE_URL)

    assert episode.series_title == "ワタシ悪役令嬢、いま無人島にいるの。……と思ったけどチート王子住んでた。"
    assert episode.episode_title == "sv_pt00069b2277ab77a1_01"
    assert len(episode.pages) == 2
    assert episode.next_url is None
    assert episode.metadata["work_url"] is None
    assert session.calls == [EPISODE_URL]


def test_episode_with_a_split_title_and_no_work_page(fake_session, fake_response):
    html = EPISODE_HTML.replace(
        "<title>ワタシ悪役令嬢、いま無人島にいるの。……と思ったけどチート王子住んでた。</title>",
        "<title>姉が剣聖で妹が賢者で　第1話</title>",
    )
    session = site(
        fake_session,
        fake_response,
        **{"/vw/mujintou_reijo/": fake_response(text=html), "/recommend/index.html": fake_response(status_code=404)},
    )
    episode = Hifumi(session).episode(EPISODE_URL)

    assert (episode.series_title, episode.episode_title) == ("姉が剣聖で妹が賢者で", "第1話")


def test_episode_without_any_title_is_named_after_its_directory(fake_session, fake_response):
    html = EPISODE_HTML.replace(
        "<title>ワタシ悪役令嬢、いま無人島にいるの。……と思ったけどチート王子住んでた。</title>", ""
    )
    session = fake_session({"/recommend/": fake_response(status_code=404), "/vw/": fake_response(text=html)})
    episode = Hifumi(session).episode(EPISODE_URL)

    assert (episode.series_title, episode.episode_title) == ("mujintou_reijo", "sv_pt00069b2277ab77a1_01")


@pytest.mark.parametrize(
    "frame",
    [
        # The frame is gone.
        "missing",
        # The frame has no link back to a work page.
        "<html><body><a href='https://www.123hon.com/polca/'>コミックポルカ</a><a href='/'>x</a></body></html>",
    ],
)
def test_episode_without_a_usable_frame_has_no_next(fake_session, fake_response, frame):
    response = fake_response(status_code=404) if frame == "missing" else fake_response(text=frame)
    session = site(fake_session, fake_response, **{"/recommend/index.html": response})
    episode = Hifumi(session).episode(EPISODE_URL)

    assert episode.readable
    assert episode.next_url is None
    assert SERIES_URL not in session.calls


@pytest.mark.parametrize("status", [404, 500])
def test_episode_survives_a_broken_work_page(fake_session, fake_response, status):
    session = site(
        fake_session, fake_response, **{"/polca/web-comic/mujintou_reijo/": fake_response(status_code=status)}
    )
    episode = Hifumi(session).episode(EPISODE_URL)

    assert episode.readable
    assert episode.episode_title == "sv_pt00069b2277ab77a1_01"
    assert episode.next_url is None


def test_locked_episode_has_no_pages(fake_session, fake_response):
    html = EPISODE_HTML.replace("data-ptimg", "data-nothing")
    session = site(fake_session, fake_response, **{"/vw/mujintou_reijo/": fake_response(text=html)})
    episode = Hifumi(session).episode(EPISODE_URL)

    assert not episode.readable
    assert episode.pages == ()
    assert episode.next_url == NEXT_URL


@pytest.mark.parametrize("status", [403, 404])
def test_missing_episode_is_gone(fake_session, fake_response, status):
    # S3 answers 403 (AccessDenied) for a directory that is not there.
    session = fake_session({"/vw/": fake_response(status_code=status, text="<Error><Code>AccessDenied</Code></Error>")})
    with pytest.raises(NotAnEpisodePageError, match=str(status)):
        Hifumi(session).episode("https://www.123hon.com/vw/mujintou_reijo/sv_pt0000000000000000_02/")


def test_page_without_a_reader_is_not_an_episode(fake_session, fake_response):
    session = fake_session({"/vw/": fake_response(text=SERIES_HTML)})
    with pytest.raises(NotAnEpisodePageError, match="no SpeedBinb reader"):
        Hifumi(session).episode(EPISODE_URL)


def test_episode_refuses_a_work_page_url(fake_session):
    with pytest.raises(UnsupportedUrlError):
        Hifumi(fake_session({})).episode(SERIES_URL)


def test_other_http_errors_propagate(fake_session, fake_response):
    session = fake_session({"/vw/": fake_response(status_code=503)})
    with pytest.raises(Exception, match="503"):
        Hifumi(session).episode(EPISODE_URL)


# --- series ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        SERIES_URL,
        "https://www.123hon.com/polca/web-comic/mujintou_reijo",
        "https://www.123hon.com/polca/book_series/mujintou_reijo/",
    ],
)
def test_series_urls_lists_the_linked_episodes_oldest_first(fake_session, fake_response, url):
    session = fake_session({"/polca/web-comic/mujintou_reijo/": fake_response(text=SERIES_HTML)})
    urls = Hifumi(session).series_urls(url)

    assert urls == [EPISODE_URL, NEXT_URL]
    assert all(Hifumi.suitable(url) for url in urls)
    assert session.calls == [SERIES_URL]


def test_series_urls_raises_on_an_empty_listing(fake_session, fake_response):
    session = fake_session({"/nova/web-comic/": fake_response(text=EMPTY_SERIES_HTML)})
    with pytest.raises(NotAnEpisodePageError, match="no readable episode"):
        Hifumi(session).series_urls("https://www.123hon.com/nova/web-comic/kichikueiyu/")


def test_series_urls_raises_on_a_missing_work(fake_session, fake_response):
    session = fake_session({"/nova/web-comic/": fake_response(status_code=404, text=NOT_A_WORK_HTML)})
    with pytest.raises(NotAnEpisodePageError, match="404"):
        Hifumi(session).series_urls("https://www.123hon.com/nova/web-comic/kichikueiyu/")


@pytest.mark.parametrize("url", [EPISODE_URL, "https://polca.123hon.com/book_series/mujintou_reijo/"])
def test_series_urls_refuses_what_is_not_a_work_page(fake_session, url):
    with pytest.raises(UnsupportedUrlError):
        Hifumi(fake_session({})).series_urls(url)


# --- download ------------------------------------------------------------------------


def test_image_puts_the_page_together(fake_session, fake_response):
    session = site(
        fake_session,
        fake_response,
        **{
            "/data/0001.ptimg.json": fake_response(payload=PTIMG, content_type="application/json"),
            "/data/0001.jpg": fake_response(png(scrambled_resource()), content_type="image/png"),
        },
    )
    extractor = Hifumi(session)
    episode = extractor.episode(EPISODE_URL)

    page = extractor.image(episode.pages[0], episode)

    assert page.size == (20, 20)
    assert page.getpixel((5, 5)) == (0, 0, 200)  # the tile the second transfer moved to (0, 0)
    assert page.getpixel((15, 15)) == (100, 100, 200)  # the tile the first transfer moved to (10, 10)
    # The JSON and the image were asked for with the episode as Referer.
    assert session.calls[-2:] == [f"{EPISODE_URL}data/0001.ptimg.json", f"{EPISODE_URL}data/0001.jpg"]
    for headers in session.headers_seen[-2:]:
        assert headers["Referer"] == EPISODE_URL


# --- the real site --------------------------------------------------------------------

# One free episode per imprint; both imprints share the one host in `HOSTS`.
TEST_URLS: dict[str, str] = {
    "www.123hon.com/polca": EPISODE_URL,
    "www.123hon.com/nova": NOVA_EPISODE_URL,
}


@pytest.mark.network
@pytest.mark.parametrize("imprint", TEST_URLS)
def test_site_download(tmp_path, imprint):
    result = Downloader(Hifumi(), tmp_path, only_first=True).download(TEST_URLS[imprint])
    assert result.status == "saved"
    assert result.episode.episode_title == "第1話"
    assert result.episode.next_url is not None
    assert (result.save_dir / "0.jpg").stat().st_size > 0


@pytest.mark.network
@pytest.mark.parametrize(("series_url", "first"), [(SERIES_URL, EPISODE_URL), (NOVA_SERIES_URL, NOVA_EPISODE_URL)])
def test_work_page_lists_episodes(series_url, first):
    urls = Hifumi().series_urls(series_url)
    assert urls[0] == first
    assert all(Hifumi.suitable(url) for url in urls)


@pytest.mark.network
def test_missing_episode_is_gone_on_the_site():
    with pytest.raises(NotAnEpisodePageError, match="403"):
        Hifumi().episode("https://www.123hon.com/vw/mujintou_reijo/sv_pt0000000000000000_02/")
