from __future__ import annotations

import json
from http import HTTPStatus
from io import BytesIO
from urllib.parse import parse_qs, urlparse

import pytest
from PIL import Image
from requests import HTTPError

from getjmanga.downloader import Downloader
from getjmanga.errors import GetjmangaError, NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractors.bloom import Bloom, parse_listing

WORK_URL = "https://bloom.homesha.co.jp/webcomic/nankahanabi/"
# How the reader links the work page: without the trailing slash.
WORK_LINK = "https://bloom.homesha.co.jp/webcomic/nankahanabi"
EPISODE_URL = "https://bloom.homesha.co.jp/cbs/c789/c89-24884/"
NEXT_URL = "https://bloom.homesha.co.jp/cbs/c789/c89-25752/"
LAST_URL = "https://bloom.homesha.co.jp/cbs/c789/c89-27089/"
READER_URL = EPISODE_URL + "speed_iv.php"
INFO_URL = "https://bloom.homesha.co.jp/cbs/~/bibGetCntntInfo"
SERVER = "https://bloom.homesha.co.jp/cbs/~v016860/sbc"
CONTENT_ID = "eynrKmjOv1TdhZ_c"
TOKEN = "k1LJ2WdSrhLVuTisfANCczl2K5M"

# A 2x2 grid, two pixels of padding, with the narrow column and the short row last
# on both sides: `BB` (short row per column) + `BB` (narrow column per row) + order.
IDENTITY_CTBL = "=2-2+2-BBBBABCD"
SWAPPED_PTBL = "=2-2-2-BBBBBADC"

COLOURS = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0)]

# The work page as the site writes it: the bracketed title, the author, the
# episode list newest first (the latest with its title in the parenthesis),
# and the volume section's trial links, which are not episodes.
WORK_HTML = """
<html><head><title>なんか、花火 | .Bloom(ドットブルーム)</title></head><body>
<div class="web__detail__data">
  <h1 class="web__detail__data__title">『なんか、花火』</h1>
  <h2 class="web__detail__data__name"><a href="https://bloom.homesha.co.jp/artist/#uenopoteto">上野 ポテト</a></h2>
</div>
<div class="web__detail__item">
  <ul>
    <li><a href="https://bloom.homesha.co.jp/cbs/c789/c89-27089/" target="_blank"><em><i class="icon-book-open"></i>
      最新話を読む</em>（ 第4話 &nbsp;前編 ／ 2026.07.03更新）</a></li>
    <li><a href="https://bloom.homesha.co.jp/cbs/c789/c89-26400/" target="_blank"><em>第3話を読む</em>
      （2026.04.10更新）</a></li>
    <li><a href="https://bloom.homesha.co.jp/cbs/c789/c89-25752/" target="_blank"><em>第2話を読む</em>
      （2026.01.16更新）</a></li>
    <li><a href="https://bloom.homesha.co.jp/cbs/c789/c89-25752/" target="_blank"><em>第2話（再掲）</em></a></li>
    <li><a href="https://bloom.homesha.co.jp/cbs/c789/c89-24884/" target="_blank"><em>第1話を読む</em>
      （2025.10.10更新）</a></li>
    <li><a href="https://bloom.homesha.co.jp/comics/nankahanabi">単行本</a></li>
  </ul>
</div>
<div class="web__detail__comics__detail__cover__sample">
  <a href="https://bloom.homesha.co.jp/cbs/c789/c85-14239/" target="_blank">試し読み</a>
</div>
</body></html>
"""

EMPTY_WORK_HTML = """
<html><body><h1 class="web__detail__data__title">『まだ始まらない』</h1>
<div class="web__detail__item"><ul></ul></div></body></html>
"""


def stub_html(title="なんか、花火　第1話", content_id=CONTENT_ID):
    """The reader directory's page: the title, the content id, and a script sending the browser to the reader."""
    cid = f'<input id="binb_cid" name="binb_cid" type="hidden" value="{content_id}" />' if content_id else ""
    return f"""
<html prefix="og: http://ogp.me/ns#"><head><title>{title}</title>
<meta property="og:url" content="https://r-cbs.mangafactory.jp/c789/c89-24884/" /></head>
<body><script>window.onload=function(){{location.replace("speed_iv.php");}};</script>
{cid}<input id="binb_type" name="binb_type" type="hidden" value="1" /></body></html>
"""


def reader_html(*, viewer=True, work_link=WORK_LINK):
    content = (
        '<div id="content" class="pages" data-ptbinb="../../~/bibGetCntntInfo" data-binbsp-flags="autobookmark"'
        f' data-ptbinb-cid="{CONTENT_ID}"></div>'
        if viewer
        else ""
    )
    link = f'☆WEB.Bloom作品ページ：<a href="{work_link}" target="_blank">{work_link}</a>' if work_link else ""
    return f"""
<html><head><title>なんか、花火　第1話</title></head><body>
{content}
<div id="cst_left_info"><div class="cst_thisbookinfo">
  <div class="cst_title">なんか、花火　第1話</div>
  <div class="cst_author">上野ポテト</div>
  <div id="cst_description" style="color:#222222;">大学生の樹里は……。　{link}</div>
</div></div>
</body></html>
"""


def content_jsonp(srcs=("pages/a.jpg", "pages/b.jpg")):
    imgs = "".join(
        f'<t-img src="{src}" a="0" orgwidth="392" orgheight="392" id="P{index:04d}"><t-pb>'
        for index, src in enumerate(srcs)
    )
    ttx = f'<html><body><t-case screen.portrait="screen.portrait">{imgs}</t-case></body>'
    body = {"SBCVersion": "01.6512", "result": 1, "ttx": ttx, "ImageClass": "default", "SmlImageCnt": len(srcs)}
    return "DataGet_Content(" + json.dumps(body) + ")"


def encode_table(content_id, key, value):
    """The inverse of `gaugau.decode_table`, so a fake API can hand tables out."""
    seed = 0
    for index, char in enumerate(f"{content_id}:{key}"):
        seed += ord(char) << (index % 16)
    state = (seed & 0x7FFFFFFF) or 0x12345678
    out = []
    for char in json.dumps(value):
        state = ((state >> 1) ^ (0x48200004 if state & 1 else 0)) & 0xFFFFFFFF
        out.append(chr((ord(char) - 32 - state) % 94 + 32))
    return "".join(out)


class InfoResponse:
    """A `bibGetCntntInfo` answer that encrypts its tables with whatever `k` was sent."""

    def __init__(self, session, *, ctbl=(IDENTITY_CTBL,), ptbl=(SWAPPED_PTBL,), server_type=0, body=None, **item):
        self.session = session
        self.ctbl = list(ctbl)
        self.ptbl = list(ptbl)
        self.server_type = server_type
        self.body = body
        self.item = item
        self.url = None
        self.status_code = HTTPStatus.OK
        self.ok = True

    def raise_for_status(self):
        pass

    def json(self):
        if self.body is not None:
            return self.body
        key = self.session.params_seen[-1]["k"]
        item = {
            "ContentID": CONTENT_ID,
            "ContentsServer": "//bloom.homesha.co.jp/cbs/~v016860/sbc/",
            "ServerType": self.server_type,
            "Title": "なんか、花火　第1話",
            "Publisher": "集英社",
            "ViewMode": 1,
            "ShopURL": "",
            "p": TOKEN,
            "stbl": "not read",
            "ttbl": "not read",
            "ctbl": encode_table(CONTENT_ID, key, self.ctbl),
            "ptbl": encode_table(CONTENT_ID, key, self.ptbl),
            **self.item,
        }
        return {"result": 1, "result_message": "", "eurl": "errorpage.html", "items": [item]}


@pytest.fixture
def client(fake_session, fake_response):
    """A Bloom on a fake site: the work page, the reader directory, the reader, the API, the page list."""

    def build(routes=None, **info):
        session = fake_session({})
        # The reader page's route must come before the directory's: routes
        # match by substring. An override keeps the position of the route it replaces.
        session.routes = {
            "/c89-24884/speed_iv.php": fake_response(text=reader_html()),
            "/c89-24884/": fake_response(text=stub_html()),
            "bibGetCntntInfo": InfoResponse(session, **info),
            "sbcGetCntnt.php": fake_response(text=content_jsonp()),
            "/webcomic/nankahanabi/": fake_response(text=WORK_HTML),
        }
        session.routes.update(routes or {})
        return Bloom(session), session

    return build


def served_image(order):
    """A 400x400 served image of a 2x2 grid, tile n painted COLOURS[order[n]] inside its padding."""
    image = Image.new("RGB", (400, 400), (0, 0, 0))
    for index, colour_index in enumerate(order):
        column, row = index % 2, index // 2
        image.paste(Image.new("RGB", (196, 196), COLOURS[colour_index]), (2 + column * 200, 2 + row * 200))
    return image


def image_route(fake_response, order=(1, 0, 3, 2)):
    raw = BytesIO()
    served_image(order).save(raw, "PNG")
    return {"sbcGetImg.php": fake_response(raw.getvalue(), content_type="image/png")}


# --- URLs ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        EPISODE_URL,
        "https://bloom.homesha.co.jp/cbs/c789/c89-24884",
        READER_URL,
        "https://bloom.homesha.co.jp/cbs/c789/c89-24884/main_iv.php",
        "https://bloom.homesha.co.jp/cbs/c789/c85-14239/",
        WORK_URL,
        "https://bloom.homesha.co.jp/webcomic/meer-and-lev",
    ],
)
def test_suitable_accepts_episode_and_work_urls(url):
    assert Bloom.suitable(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://bloom.homesha.co.jp/cbs/c789/c89-24884/",
        "https://r-cbs.mangafactory.jp/c789/c89-24884/",
        "https://www.homesha.co.jp/cbs/c789/c89-24884/",
        "https://bloom.homesha.co.jp/",
        "https://bloom.homesha.co.jp/webcomic/",
        "https://bloom.homesha.co.jp/webcomic/feed/",
        "https://bloom.homesha.co.jp/comics/nankahanabi",
        "https://bloom.homesha.co.jp/cbs/c789/",
        "https://bloom.homesha.co.jp/cbs/~/bibGetCntntInfo",
        "https://bloom.homesha.co.jp/cbs/c789/c89-24884/js/parent_main.js",
    ],
)
def test_suitable_rejects_other_urls(url):
    assert not Bloom.suitable(url)


def test_is_series_is_true_for_a_work_page_only():
    assert Bloom.is_series(WORK_URL)
    assert Bloom.is_series("https://bloom.homesha.co.jp/webcomic/netsuai")
    assert not Bloom.is_series(EPISODE_URL)
    assert not Bloom.is_series("https://bloom.homesha.co.jp/webcomic/feed/")


# --- parsing --------------------------------------------------------------------------


def test_parse_listing_keeps_an_unbracketed_title():
    html = WORK_HTML.replace("『なんか、花火』", "なんか、花火")
    assert parse_listing(html, WORK_URL)[0] == "なんか、花火"


# --- episodes -------------------------------------------------------------------------


def test_episode_reads_the_titles_the_pages_and_the_next_episode(client):
    bloom, session = client()
    episode = bloom.episode(EPISODE_URL)

    assert episode.url == EPISODE_URL
    assert episode.series_title == "なんか、花火"
    assert episode.episode_title == "第1話"
    assert episode.next_url == NEXT_URL
    assert [page.width for page in episode.pages] == [392, 392]
    page = urlparse(episode.pages[0].url)
    assert f"{page.scheme}://{page.netloc}{page.path}" == f"{SERVER}/sbcGetImg.php"
    assert parse_qs(page.query) == {"cid": [CONTENT_ID], "src": ["pages/a.jpg"], "p": [TOKEN], "q": ["0"], "vm": ["1"]}
    assert parse_qs(urlparse(episode.pages[1].url).query)["src"] == ["pages/b.jpg"]
    assert episode.pages[0].extra == {"ctbl": IDENTITY_CTBL, "ptbl": SWAPPED_PTBL}
    assert episode.metadata["content_id"] == CONTENT_ID
    assert episode.metadata["work_url"] == WORK_URL
    assert episode.metadata["locked"] is False
    assert "ctbl" not in episode.metadata["info"]
    json.dumps(episode.metadata)
    # The stub first (it sets the cookie), the reader, the work page it links, the API, the page list.
    assert session.calls == [EPISODE_URL, READER_URL, WORK_URL, INFO_URL, f"{SERVER}/sbcGetCntnt.php"]


def test_episode_calls_the_api_the_way_the_reader_does(client):
    bloom, session = client()
    bloom.episode(EPISODE_URL)

    info_index = session.calls.index(INFO_URL)
    params = session.params_seen[info_index]
    assert params["cid"] == CONTENT_ID
    assert len(params["k"]) == 32
    assert isinstance(params["dmytime"], int)
    assert session.headers_seen[info_index]["Referer"] == READER_URL

    list_index = session.calls.index(f"{SERVER}/sbcGetCntnt.php")
    assert session.params_seen[list_index]["cid"] == CONTENT_ID
    assert session.params_seen[list_index]["p"] == TOKEN
    assert session.params_seen[list_index]["vm"] == "1"
    assert session.headers_seen[list_index]["Referer"] == READER_URL


@pytest.mark.parametrize(
    "url",
    [
        "https://bloom.homesha.co.jp/cbs/c789/c89-24884",
        READER_URL,
        "https://bloom.homesha.co.jp/cbs/c789/c89-24884/main_iv.php",
    ],
)
def test_episode_canonicalises_the_url(client, url):
    bloom, session = client()
    assert bloom.episode(url).url == EPISODE_URL
    assert session.calls[0] == EPISODE_URL


def test_last_episode_has_no_next(client, fake_response):
    bloom, _ = client(
        {
            "/c89-27089/speed_iv.php": fake_response(text=reader_html()),
            "/c89-27089/": fake_response(text=stub_html(title="なんか、花火　第4話　前編")),
        },
    )
    episode = bloom.episode(LAST_URL)
    assert episode.episode_title == "第4話　前編"
    assert episode.next_url is None


def test_episode_the_work_page_does_not_list_has_no_next(client, fake_response):
    bloom, _ = client(
        {
            "/c89-99999/speed_iv.php": fake_response(text=reader_html()),
            "/c89-99999/": fake_response(text=stub_html(title="なんか、花火　番外編")),
        },
    )
    episode = bloom.episode("https://bloom.homesha.co.jp/cbs/c789/c89-99999/")
    assert episode.series_title == "なんか、花火"
    assert episode.episode_title == "番外編"
    assert episode.next_url is None


def test_volume_trial_without_a_work_link_is_titled_off_the_reader(client, fake_response):
    bloom, session = client(
        {
            "/c89-24884/speed_iv.php": fake_response(text=reader_html(work_link="")),
            "/c89-24884/": fake_response(text=stub_html(title="【試し読み】また明日会えるよ（奏島ゆこ）")),
        },
    )
    episode = bloom.episode(EPISODE_URL)
    assert episode.series_title == "【試し読み】また明日会えるよ（奏島ゆこ）"
    assert episode.episode_title == "【試し読み】また明日会えるよ（奏島ゆこ）"
    assert episode.next_url is None
    assert episode.metadata["work_url"] is None
    assert WORK_URL not in session.calls


def test_api_that_refuses_the_content_is_locked(client):
    bloom, _ = client(body={"result": 0, "result_message": "", "eurl": "errorpage.html", "items": []})
    episode = bloom.episode(EPISODE_URL)
    assert episode.pages == ()
    assert not episode.readable
    assert episode.series_title == "なんか、花火"
    assert episode.episode_title == "第1話"
    assert episode.next_url == NEXT_URL
    assert episode.metadata["locked"] is True
    json.dumps(episode.metadata)


def test_gone_episode_is_not_an_episode(client, fake_response):
    bloom, _ = client({"/c89-24884/": fake_response(text="Not Found", status_code=HTTPStatus.NOT_FOUND)})
    with pytest.raises(NotAnEpisodePageError, match="404"):
        bloom.episode(EPISODE_URL)


def test_other_http_errors_propagate(client, fake_response):
    bloom, _ = client({"/c89-24884/": fake_response(text="", status_code=HTTPStatus.SERVICE_UNAVAILABLE)})
    with pytest.raises(HTTPError):
        bloom.episode(EPISODE_URL)


def test_directory_without_a_content_id_is_not_an_episode(client, fake_response):
    bloom, _ = client({"/c89-24884/": fake_response(text=stub_html(content_id=""))})
    with pytest.raises(NotAnEpisodePageError, match="no reader content"):
        bloom.episode(EPISODE_URL)


def test_reader_without_a_viewer_is_not_an_episode(client, fake_response):
    bloom, _ = client({"/c89-24884/speed_iv.php": fake_response(text=reader_html(viewer=False))})
    with pytest.raises(NotAnEpisodePageError, match="no SpeedBinb reader"):
        bloom.episode(EPISODE_URL)


def test_api_that_does_not_describe_the_content_is_not_an_episode(client):
    bloom, _ = client(body={"result": 1, "items": [{"ContentID": CONTENT_ID}]})
    with pytest.raises(NotAnEpisodePageError, match="did not describe"):
        bloom.episode(EPISODE_URL)


def test_api_on_another_backend_is_unsupported(client):
    bloom, _ = client(server_type=1)
    with pytest.raises(GetjmangaError, match="ServerType 1"):
        bloom.episode(EPISODE_URL)


def test_episode_refuses_a_work_url(client):
    bloom, session = client()
    with pytest.raises(UnsupportedUrlError):
        bloom.episode(WORK_URL)
    assert session.calls == []


# --- series ---------------------------------------------------------------------------


@pytest.mark.parametrize("url", [WORK_URL, "https://bloom.homesha.co.jp/webcomic/nankahanabi"])
def test_series_urls_lists_the_episodes_oldest_first(client, url):
    bloom, session = client()
    assert bloom.series_urls(url) == [
        EPISODE_URL,
        NEXT_URL,
        "https://bloom.homesha.co.jp/cbs/c789/c89-26400/",
        LAST_URL,
    ]
    assert session.calls == [WORK_URL]


def test_series_urls_raises_on_an_empty_listing(client, fake_response):
    bloom, _ = client({"/webcomic/nankahanabi/": fake_response(text=EMPTY_WORK_HTML)})
    with pytest.raises(NotAnEpisodePageError, match="lists no episode"):
        bloom.series_urls(WORK_URL)


def test_series_urls_raises_on_a_missing_work(client, fake_response):
    bloom, _ = client({"/webcomic/nankahanabi/": fake_response(text="", status_code=HTTPStatus.NOT_FOUND)})
    with pytest.raises(NotAnEpisodePageError, match="404"):
        bloom.series_urls(WORK_URL)


def test_series_urls_refuses_a_url_of_the_wrong_shape(client):
    bloom, session = client()
    with pytest.raises(UnsupportedUrlError):
        bloom.series_urls(EPISODE_URL)
    assert session.calls == []


# --- images and downloading -----------------------------------------------------------


def test_image_fetches_the_page_with_the_reader_as_referer(client, fake_response):
    bloom, session = client(image_route(fake_response))
    episode = bloom.episode(EPISODE_URL)

    page = bloom.image(episode.pages[0], episode)

    assert page.size == (392, 392)
    assert page.getpixel((5, 5)) == COLOURS[0]
    assert session.calls[-1] == episode.pages[0].url
    assert session.headers_seen[-1]["Referer"] == READER_URL


# --- logging in -----------------------------------------------------------------------


# --- the real site --------------------------------------------------------------------

# One URL per known host, free to read without an account: the first episode
# of なんか、花火.
TEST_URLS: dict[str, str] = {
    "bloom.homesha.co.jp": EPISODE_URL,
}


@pytest.mark.network
@pytest.mark.parametrize("host", TEST_URLS)
def test_site_download(tmp_path, host):
    result = Downloader(Bloom(), tmp_path, only_first=True).download(TEST_URLS[host])
    assert result.status == "saved"
    assert result.episode.series_title == "なんか、花火"
    assert result.episode.episode_title == "第1話"
    assert result.episode.next_url == NEXT_URL
    assert (result.save_dir / "0.jpg").stat().st_size > 0
    # The tables put the 8x8 grid back and drop its padding: 908x1264 served, 844x1200 page.
    assert Image.open(result.save_dir / "0.jpg").size == (844, 1200)


@pytest.mark.network
def test_work_page_lists_episodes_oldest_first():
    bloom = Bloom()
    assert bloom.is_series(WORK_URL)
    urls = bloom.series_urls(WORK_URL)
    assert urls[0] == EPISODE_URL
    assert all(Bloom.suitable(url) for url in urls)


@pytest.mark.network
def test_gone_episode_is_not_an_episode_on_the_site():
    with pytest.raises(NotAnEpisodePageError, match="404"):
        Bloom().episode("https://bloom.homesha.co.jp/cbs/c789/c89-21000/")
