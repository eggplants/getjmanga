from __future__ import annotations

from datetime import date
from io import BytesIO

import pytest
from PIL import Image

from getjmanga.downloader import Downloader
from getjmanga.errors import NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractors.michikusa import Michikusa, parse_listing
from getjmanga.viewers.speedbinb import parse_ptimg

EPISODE_URL = "https://michikusacomics.jp/wp-content/uploads/data/11_vegetable/01/index.html"
NEXT_URL = "https://michikusacomics.jp/wp-content/uploads/data/11_vegetable/02/index.html"
LAST_URL = "https://michikusacomics.jp/wp-content/uploads/data/11_vegetable/03/index.html"
SERIES_URL = "https://michikusacomics.jp/product/vegetable"

EPISODE_HTML = """
<html><head><title>べじたぶるサンドイッチ　その1　つくしとわらび</title></head><body>
<div id="contents">
  <div id="content" class="pages ptbinb-container" data-binbsp-direction="rtl" data-binbsp-recommend="last.html[next]">
    <div data-ptimg="data/0001.ptimg.json" data-binbsp-spread="left"></div>
    <div data-ptimg="data/0002.ptimg.json" data-binbsp-spread="right"></div>
  </div>
</div>
</body></html>
"""

# The frame past the last page, made by WordPress: a "next" button (when
# there is a next episode) and a "home" button back to the work page.
LAST_HTML = """
<html><body><div id="recommend"><div class="content">
  <div class="header"><div class="header_logo"><a href="/"><img src="logo.png"></a></div></div>
  <div class="text_button next_story">
    <div>
      <a href="https://michikusacomics.jp/wp-content/uploads/data/11_vegetable/02/index.html"><img src="next.svg"></a>
    </div>
  </div>
  <div class="text_button buy_book"><a href="https://www.amazon.co.jp/dp/4910352562"><img src="buy.svg"></a></div>
  <div class="text_button home"><a href="https://michikusacomics.jp/product/vegetable"><img src="home.svg"></a></div>
</div></div></body></html>
"""

SERIES_HTML = """
<html><head><title>べじたぶるサンドイッチ - 路草</title></head><body>
<article class="product">
  <h1 class="entry-title  page-title">べじたぶるサンドイッチ</h1>
  <h4>作者プロフィール</h4>
  <span id="authorName" class="authorName">齊藤万丈</span>
  <div class="latest_episode">
    <a href="https://michikusacomics.jp/wp-content/uploads/data/11_vegetable/03/index.html"><img src="latest.svg"></a>
  </div>
  <div class="released_episodes">
    <div class="show-pc"><h4>公開中のエピソード</h4><div class="items">
      <div class="item"><a href="https://michikusacomics.jp/wp-content/uploads/data/11_vegetable/01/index.html">その１</a></div>
      <div class="item"><a href="/wp-content/uploads/data/11_vegetable/02/index.html">その２</a></div>
      <div class="item"><a href="https://michikusacomics.jp/wp-content/uploads/data/11_vegetable/03/index.html">その３</a></div>
      <div class="item"><a href="https://michikusacomics.jp/wp-content/uploads/data/11_vegetable/01/index.html">その１</a></div>
    </div></div>
    <div class="show-sp"><div class="items container">
      <div class="mix" data-order="0"><a href="https://michikusacomics.jp/wp-content/uploads/data/11_vegetable/01/index.html">
        <div class="item"><img src="1.jpg"><div class="info">
          <div>その１</div><div class="title">つくしとわらび</div><div class="publication_date">2022.03.09</div>
        </div></div></a></div>
      <div class="mix" data-order="5"><a href="https://michikusacomics.jp/wp-content/uploads/data/11_vegetable/02/index.html">
        <div class="item"><img src="2.jpg"><div class="info">
          <div>その２</div><div class="title">決闘しよう</div><div class="publication_date">2022.04.06</div>
        </div></div></a></div>
      <div class="mix" data-order="6"><a href="https://michikusacomics.jp/wp-content/uploads/data/11_vegetable/03/index.html">
        <div class="item"><img src="3.jpg"><div class="info">
          <div>その３</div><div class="title"></div><div class="publication_date">2022.05.11</div>
        </div></div></a></div>
    </div></div>
  </div>
</article>
</body></html>
"""

EMPTY_SERIES_HTML = """
<html><body><h1 class="entry-title">おわった作品</h1>
<div class="released_episodes"><div class="show-pc"><div class="items"></div></div></div></body></html>
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


def site(fake_session, fake_response, **extra):
    """A session scripted with the work, the episode and its last-page frame; `extra` routes win."""
    return fake_session(
        {
            "/11_vegetable/01/index.html": fake_response(text=EPISODE_HTML),
            "/11_vegetable/01/last.html": fake_response(text=LAST_HTML),
            "/product/vegetable": fake_response(text=SERIES_HTML),
            **extra,
        },
    )


# --- URLs ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        EPISODE_URL,
        "https://michikusacomics.jp/wp-content/uploads/data/11_vegetable/01/",
        "https://michikusacomics.jp/wp-content/uploads/data/11_vegetable/01",
        "https://michikusacomics.jp/wp-content/uploads/data/03_heartstopper/yearbook/index.html",
        "https://michikusacomics.jp/wp-content/uploads/data/30_88KOKURA/02/index.html",
        SERIES_URL,
        "https://michikusacomics.jp/product/vegetable/",
        "https://michikusacomics.jp/product/onitoyoake_yoru",
    ],
)
def test_suitable_accepts_episode_and_series_urls(url):
    assert Michikusa.suitable(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://michikusacomics.jp/wp-content/uploads/data/11_vegetable/01/index.html",
        "https://www.michikusacomics.jp/product/vegetable",
        "https://comic-porta.com/p_data/ol_ningyo001al/",
        "https://michikusacomics.jp/",
        "https://michikusacomics.jp/product",
        "https://michikusacomics.jp/product/page/2",
        "https://michikusacomics.jp/product/feed",
        "https://michikusacomics.jp/magazine",
        "https://michikusacomics.jp/wp-content/uploads/data/11_vegetable/",
        "https://michikusacomics.jp/wp-content/uploads/data/11_vegetable/01/last.html",
        "https://michikusacomics.jp/wp-content/uploads/data/11_vegetable/01/data/0001.ptimg.json",
    ],
)
def test_suitable_rejects_other_urls(url):
    assert not Michikusa.suitable(url)


def test_is_series_only_for_a_work_page():
    assert Michikusa.is_series(SERIES_URL)
    assert Michikusa.is_series("https://michikusacomics.jp/product/vegetable/")
    assert not Michikusa.is_series(EPISODE_URL)
    assert not Michikusa.is_series("https://michikusacomics.jp/product/page/2")


# --- the work page ---------------------------------------------------------------


def test_parse_listing_without_cards_keeps_the_plain_labels():
    html = SERIES_HTML.split('<div class="show-sp">', 1)[0] + "</div></article></body></html>"
    listing = parse_listing(html, SERIES_URL)
    assert listing.episodes == {EPISODE_URL: "その１", NEXT_URL: "その２", LAST_URL: "その３"}


# --- episode -----------------------------------------------------------------------


def test_episode_reads_the_titles_the_pages_and_the_next_episode(fake_session, fake_response):
    session = site(fake_session, fake_response)
    episode = Michikusa(session).episode(EPISODE_URL)

    assert episode.url == EPISODE_URL
    assert episode.series_title == "べじたぶるサンドイッチ"
    assert (episode.writer, episode.publisher) == ("齊藤万丈", "トゥーヴァージンズ")
    assert episode.episode_title == "その１　つくしとわらび"
    assert [page.url for page in episode.pages] == [
        "https://michikusacomics.jp/wp-content/uploads/data/11_vegetable/01/data/0001.ptimg.json",
        "https://michikusacomics.jp/wp-content/uploads/data/11_vegetable/01/data/0002.ptimg.json",
    ]
    assert [page.extra["spread"] for page in episode.pages] == ["left", "right"]
    assert episode.next_url == NEXT_URL
    assert episode.number == 1
    assert episode.metadata["direction"] == "rtl"
    assert episode.metadata["recommend"] == "last.html[next]"
    assert episode.metadata["work_url"] == SERIES_URL
    assert episode.metadata["title"] == "べじたぶるサンドイッチ　その1　つくしとわらび"
    # The reader, its last-page frame, then the work page.
    assert session.calls == [
        EPISODE_URL,
        "https://michikusacomics.jp/wp-content/uploads/data/11_vegetable/01/last.html",
        SERIES_URL,
    ]


def test_episode_is_dated_by_its_first_page_upload(fake_session, fake_response, uploaded):
    session = site(fake_session, fake_response, **{"/data/0001.ptimg.json": fake_response(b"", headers=uploaded)})
    assert Michikusa(session).episode(EPISODE_URL).published == date(2025, 8, 21)


def test_episode_takes_a_directory_url_and_canonicalises_it(fake_session, fake_response):
    session = site(fake_session, fake_response)
    episode = Michikusa(session).episode("https://michikusacomics.jp/wp-content/uploads/data/11_vegetable/01/")

    assert episode.url == EPISODE_URL
    assert session.calls[0] == EPISODE_URL


def test_last_episode_has_no_next(fake_session, fake_response):
    last_html = LAST_HTML.replace("11_vegetable/02", "11_vegetable/99")  # a stale button, not trusted
    session = site(
        fake_session,
        fake_response,
        **{
            "/11_vegetable/03/index.html": fake_response(text=EPISODE_HTML.replace("その1", "その3")),
            "/11_vegetable/03/last.html": fake_response(text=last_html),
        },
    )
    episode = Michikusa(session).episode(LAST_URL)

    assert episode.episode_title == "その３"
    assert (episode.prev_url, episode.next_url) == (NEXT_URL, None)


def test_unlisted_episode_falls_back_to_the_reader_title_and_the_frame_button(fake_session, fake_response):
    # A work page that does not (yet) list the episode: the reader's title is
    # split on the series name, and the frame's "next" button stands in.
    session = site(
        fake_session,
        fake_response,
        **{
            "/11_vegetable/00/index.html": fake_response(text=EPISODE_HTML.replace("その1", "その0")),
            "/11_vegetable/00/last.html": fake_response(text=LAST_HTML),
        },
    )
    episode = Michikusa(session).episode("https://michikusacomics.jp/wp-content/uploads/data/11_vegetable/00/")

    assert episode.series_title == "べじたぶるサンドイッチ"
    assert episode.episode_title == "その0　つくしとわらび"
    assert episode.next_url == NEXT_URL


def test_episode_without_a_recommend_frame_still_reads(fake_session, fake_response):
    html = EPISODE_HTML.replace(' data-binbsp-recommend="last.html[next]"', "")
    session = fake_session({"/11_vegetable/01/index.html": fake_response(text=html)})
    episode = Michikusa(session).episode(EPISODE_URL)

    assert episode.series_title == "べじたぶるサンドイッチ"
    assert episode.episode_title == "その1　つくしとわらび"
    assert episode.next_url is None
    assert episode.metadata["work_url"] is None
    assert len(episode.pages) == 2
    assert session.calls == [EPISODE_URL]


@pytest.mark.parametrize(
    "frame",
    [
        # The frame is gone, has nothing, or has no link to a work page.
        "missing",
        "<html><body><p>nothing here</p></body></html>",
        '<html><body><a href="https://michikusacomics.jp/">top</a><a href="https://example.com/product/x">x</a></body></html>',
    ],
)
def test_episode_without_a_usable_frame_has_no_next(fake_session, fake_response, frame):
    response = fake_response(status_code=404) if frame == "missing" else fake_response(text=frame)
    session = site(fake_session, fake_response, **{"/11_vegetable/01/last.html": response})
    episode = Michikusa(session).episode(EPISODE_URL)

    assert episode.series_title == "べじたぶるサンドイッチ"
    assert episode.episode_title == "その1　つくしとわらび"
    assert episode.next_url is None
    assert SERIES_URL not in session.calls


@pytest.mark.parametrize("status", [404, 500])
def test_episode_survives_a_broken_work_page(fake_session, fake_response, status):
    # The frame's own "next" button stands in for the missing listing.
    session = site(fake_session, fake_response, **{"/product/vegetable": fake_response(status_code=status)})
    episode = Michikusa(session).episode(EPISODE_URL)

    assert episode.series_title == "べじたぶるサンドイッチ"
    assert episode.episode_title == "その1　つくしとわらび"
    assert episode.next_url == NEXT_URL
    assert len(episode.pages) == 2


def test_locked_episode_has_no_pages(fake_session, fake_response):
    html = EPISODE_HTML.replace("data-ptimg", "data-nothing")
    session = site(fake_session, fake_response, **{"/11_vegetable/01/index.html": fake_response(text=html)})
    episode = Michikusa(session).episode(EPISODE_URL)

    assert episode.pages == ()
    assert episode.next_url == NEXT_URL
    assert episode.episode_title == "その１　つくしとわらび"


def test_expired_episode_is_gone(fake_session, fake_response):
    session = fake_session({"/11_vegetable/05/index.html": fake_response(status_code=404)})
    with pytest.raises(NotAnEpisodePageError, match="404"):
        Michikusa(session).episode("https://michikusacomics.jp/wp-content/uploads/data/11_vegetable/05/index.html")


def test_page_without_a_reader_is_not_an_episode(fake_session, fake_response):
    session = fake_session({"/11_vegetable/01/index.html": fake_response(text="<html><body>hi</body></html>")})
    with pytest.raises(NotAnEpisodePageError, match="no SpeedBinb reader"):
        Michikusa(session).episode(EPISODE_URL)


def test_episode_refuses_a_work_page_url(fake_session):
    with pytest.raises(UnsupportedUrlError):
        Michikusa(fake_session({})).episode(SERIES_URL)


def test_other_http_errors_propagate(fake_session, fake_response):
    session = fake_session({"/11_vegetable/01/index.html": fake_response(status_code=503)})
    with pytest.raises(Exception, match="503"):
        Michikusa(session).episode(EPISODE_URL)


# --- series ------------------------------------------------------------------------


def test_series_urls_lists_the_linked_episodes_oldest_first(fake_session, fake_response):
    session = site(fake_session, fake_response)
    urls = Michikusa(session).series_urls("https://michikusacomics.jp/product/vegetable/")

    assert urls == [EPISODE_URL, NEXT_URL, LAST_URL]
    assert all(Michikusa.suitable(url) for url in urls)
    assert session.calls == [SERIES_URL]


def test_series_urls_raises_on_an_empty_listing(fake_session, fake_response):
    session = fake_session({"/product/owatta": fake_response(text=EMPTY_SERIES_HTML)})
    with pytest.raises(NotAnEpisodePageError, match="no public episode"):
        Michikusa(session).series_urls("https://michikusacomics.jp/product/owatta")


def test_series_urls_raises_on_a_missing_work(fake_session, fake_response):
    session = fake_session({"/product/nothing": fake_response(status_code=404)})
    with pytest.raises(NotAnEpisodePageError, match="404"):
        Michikusa(session).series_urls("https://michikusacomics.jp/product/nothing")


def test_series_urls_refuses_an_episode_url(fake_session):
    with pytest.raises(UnsupportedUrlError):
        Michikusa(fake_session({})).series_urls(EPISODE_URL)


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
    extractor = Michikusa(session)
    episode = extractor.episode(EPISODE_URL)

    page = extractor.image(episode.pages[0], episode)

    assert page.size == (20, 20)
    assert page.getpixel((5, 5)) == (0, 0, 200)  # the tile the second transfer moved to (0, 0)
    assert page.getpixel((15, 15)) == (100, 100, 200)  # the tile the first transfer moved to (10, 10)
    # The JSON and the image were asked for with the episode as Referer.
    assert session.calls[-2:] == [
        "https://michikusacomics.jp/wp-content/uploads/data/11_vegetable/01/data/0001.ptimg.json",
        "https://michikusacomics.jp/wp-content/uploads/data/11_vegetable/01/data/0001.jpg",
    ]
    for headers in session.headers_seen[-2:]:
        assert headers["Referer"] == EPISODE_URL


# --- the real site --------------------------------------------------------------------

# One episode per known host, free to read without an account.
TEST_URLS: dict[str, str] = {
    "michikusacomics.jp": "https://michikusacomics.jp/wp-content/uploads/data/11_vegetable/01/index.html",
}


@pytest.mark.network
@pytest.mark.parametrize("host", TEST_URLS)
def test_site_download(tmp_path, host):
    result = Downloader(Michikusa(), tmp_path, only_first=True).download(TEST_URLS[host])
    assert result.status == "saved"
    assert (result.save_dir / "0.jpg").stat().st_size > 0
    assert result.episode.series_title == "べじたぶるサンドイッチ"
    assert result.episode.next_url == NEXT_URL


@pytest.mark.network
def test_work_page_lists_episodes():
    urls = Michikusa().series_urls(SERIES_URL)
    assert urls[0] == TEST_URLS["michikusacomics.jp"]
    assert all(Michikusa.suitable(url) for url in urls)


@pytest.mark.network
def test_expired_episode_is_gone_on_the_site():
    with pytest.raises(NotAnEpisodePageError):
        Michikusa().episode("https://michikusacomics.jp/wp-content/uploads/data/27_hutsukoi/05/index.html")
