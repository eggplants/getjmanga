from __future__ import annotations

import json
from http import HTTPStatus
from io import BytesIO
from urllib.parse import parse_qs, urlparse

import pytest
from httpx import HTTPStatusError
from PIL import Image

from getjmanga.downloader import Downloader
from getjmanga.errors import GetjmangaError, NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractors.cmoa import Cmoa, parse_listing_page

TITLE_ID = "104860"
TITLE_URL = "https://www.cmoa.jp/title/104860/"
VOLUME_URL = "https://www.cmoa.jp/title/104860/vol/1/"
NEXT_URL = "https://www.cmoa.jp/title/104860/vol/2/"
LAST_URL = "https://www.cmoa.jp/title/104860/vol/3/"
CONTENT_ID = "100001048600001"
BIB_ID = "0000104860_jp_0001"
SAMPLE_URL = f"https://www.cmoa.jp/reader/sample/title_id/{TITLE_ID}/content_id/{CONTENT_ID}/"
READER_URL = f"https://www.cmoa.jp/bib/speedreader/?cid={BIB_ID}&u0=1&rurl=https%3A%2F%2Fwww.cmoa.jp%2F"
INFO_URL = "https://www.cmoa.jp/bib/sws/bibGetCntntInfo.php"
SERVER = "https://free-binb-cmoa.akamaized.net/sbc"
TOKEN = "1PHuF6KVPnDhccBnXZxappY6kCI"

# A 2x2 grid, two pixels of padding, with the narrow column and the short row last
# on both sides: `BB` (short row per column) + `BB` (narrow column per row) + order.
IDENTITY_CTBL = "=2-2+2-BBBBABCD"
SWAPPED_PTBL = "=2-2-2-BBBBBADC"

COLOURS = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0)]


def lineup_item(href, title, content_id, *, sample=True):
    button = (
        f'<a href="/reader/sample/?title_id={TITLE_ID}&amp;content_id={content_id}" rel="nofollow">無料</a>'
        if sample
        else ""
    )
    return f"""
<li class="title_vol_easy_box_in_th">
  <div class="thum_box3"><a class="thum_img3_a" href="{href}"><img alt="{title}" src="x.jpg"/></a></div>
  <div class="easy_area_btn">
    <a _content_id="{content_id}" _title_id="{TITLE_ID}" class="cart_into_btn" href="javascript:void(0);">
      <img alt="会員登録して購入" src="buy.png"/></a>{button}
  </div>
  <div class="title_check_box"><h3><a href="{href}">
            {title}
        </a></h3></div>
  <div class="book_last_cam">今だけ無料で読める！</div>
</li>
"""


def listing_html(items, *, pages=("1", "2"), breadcrumb=True):
    """A title page in its easy display mode: the breadcrumb, the lineup, the pagination and the mode switch."""
    crumbs = (
        '<ul class="brCramb top header_bread_cramb_w"><li><a href="/">コミックシーモアTOP</a></li>'
        '<li><a href="/search/publisher/262/">講談社</a></li>'
        f'<li><a href="/title/{TITLE_ID}/">ダイヤのA act2</a></li><li>ダイヤのＡ　ａｃｔ２（１）</li></ul>'
        if breadcrumb
        else ""
    )
    pagination = "".join(f'<a href="/title/{TITLE_ID}/?order=up&amp;page={page}#buyarea">{page}</a>' for page in pages)
    return f"""
<html><head><title>ダイヤのＡ　ａｃｔ２（１）｜無料漫画（マンガ）ならコミックシーモア｜寺嶋裕二</title></head><body>
{crumbs}
<h1 class="titleName">ダイヤのＡ　ａｃｔ２（１）</h1>
<div class="title_details_author_name"><a href="/search/author/9533/">寺嶋裕二</a></div>
<a href="/title/{TITLE_ID}/?page=1&amp;order=up&amp;disp_mode=comp#buyarea">詳細</a>
<a href="/title/{TITLE_ID}/?page=1&amp;order=down#buyarea">最新刊から</a>
<ul class="title_vol_easy_box clearfix">{"".join(items)}</ul>
<div class="pagination">{pagination}</div>
<a href="/title/{TITLE_ID}/?page=9&amp;order=up&amp;disp_mode=comp#buyarea">詳細</a>
<ul class="title_vol_easy_box recommend">
  {lineup_item("/title/328808/vol/6/", "よその作品（６）", "100003288080006")}
</ul>
</body></html>
"""


# Page one links the first volume as the title page itself; page two holds the last volume.
PAGE_ONE = listing_html(
    [
        lineup_item(f"/title/{TITLE_ID}/", "ダイヤのＡ　ａｃｔ２（１）", CONTENT_ID),
        lineup_item(f"/title/{TITLE_ID}/vol/2/", "ダイヤのＡ　ａｃｔ２（２）", "100001048600002"),
        lineup_item(f"/title/{TITLE_ID}/vol/2/", "ダイヤのＡ　ａｃｔ２（２）（再掲）", "100001048600002"),
    ],
)
PAGE_TWO = listing_html(
    [lineup_item(f"/title/{TITLE_ID}/vol/3/", "ダイヤのA act2（3）", "100001048600003", sample=False)],
    pages=("1",),
)
EMPTY_PAGE = listing_html([], pages=())

READER_HTML = """
<html><head><title>BinB Speed Reader</title></head><body>
<div id="content_base"><div id="content" class="pages" data-ptbinb="/bib/sws/bibGetCntntInfo.php"></div></div>
</body></html>
"""

ERROR_URL = "https://www.cmoa.jp/message/?error_cd=BV_00004"
ERROR_HTML = "<html><body><p>指定されたページは存在しません。</p></body></html>"


def content_jsonp(srcs=("pages/a.jpg", "pages/b.jpg")):
    imgs = "".join(
        f'<t-img src="{src}" a="0" orgwidth="392" orgheight="392" id="P{index:04d}"><t-pb>'
        for index, src in enumerate(srcs)
    )
    ttx = f'<html><body><t-case screen.portrait="screen.portrait">{imgs}</t-case></body>'
    body = {"SBCVersion": "01.6700", "result": 1, "ttx": ttx, "ImageClass": "singlequality", "SmlImageCnt": len(srcs)}
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
        self.is_success = True

    def raise_for_status(self):
        pass

    def json(self):
        if self.body is not None:
            return self.body
        params = self.session.params_seen[-1]
        item = {
            "ContentID": CONTENT_ID,
            "ContentsServer": "https://free-binb-cmoa.akamaized.net/sbc",
            "ServerType": self.server_type,
            "Title": "無料・試し読みページ ダイヤのA act2 1巻（週刊少年マガジン） ｜ 寺嶋裕二 ｜ コミックシーモア",
            "SubTitle": "ダイヤのＡ　ａｃｔ２（１）",
            "Publisher": "講談社",
            "ViewMode": 2,
            "ShopURL": "/title/104860/vol/1/",
            "p": TOKEN,
            "stbl": "not read",
            "ttbl": "not read",
            "ctbl": encode_table(params["cid"], params["k"], self.ctbl),
            "ptbl": encode_table(params["cid"], params["k"], self.ptbl),
            **self.item,
        }
        return {"result": 1, "ShopUserID": "", "eurl": "errorpage.php", "items": [item]}


@pytest.fixture
def client(fake_session, fake_response):
    """A Cmoa on a fake store: the title page in two lineup pages, the sample link, the API, the page list."""

    def build(routes=None, **info):
        session = fake_session({})
        session.routes = {
            "/reader/sample/": fake_response(text=READER_HTML, url=READER_URL),
            "/reader/browserviewer/": fake_response(text=READER_HTML, url=READER_URL),
            "/bib/speedreader/": fake_response(text=READER_HTML),
            "bibGetCntntInfo": InfoResponse(session, **info),
            "sbcGetCntnt.php": fake_response(text=content_jsonp()),
            TITLE_URL: [fake_response(text=PAGE_ONE), fake_response(text=PAGE_TWO)],
        }
        session.routes.update(routes or {})
        return Cmoa(session), session

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
        VOLUME_URL,
        "https://www.cmoa.jp/title/104860/vol/34",
        TITLE_URL,
        "https://www.cmoa.jp/title/1101088866",
        SAMPLE_URL,
        "https://www.cmoa.jp/reader/sample/title_id/104860/",
        "https://www.cmoa.jp/reader/sample/?title_id=104860&content_id=100001048600002",
        "https://www.cmoa.jp/reader/browserviewer/content_id/100001048600001/sample_flg/1/?ret_url=",
        "https://www.cmoa.jp/reader/browserviewer/content_id/100001048600001/",
        READER_URL,
        "https://www.cmoa.jp/bib/speedreader/?cid=0000104860_jp_0001&u0=0",
    ],
)
def test_suitable_accepts_title_volume_sample_viewer_and_reader_urls(url):
    assert Cmoa.suitable(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://www.cmoa.jp/title/104860/vol/1/",
        "https://cmoa.jp/title/104860/vol/1/",
        "https://sp.cmoa.jp/title/104860/",
        "https://www.cmoa.jp/",
        "https://www.cmoa.jp/title/",
        "https://www.cmoa.jp/title/104860/vol/",
        "https://www.cmoa.jp/freecontents/title/all/",
        "https://www.cmoa.jp/reader/sample/",
        "https://www.cmoa.jp/reader/sample/?ret_url=",
        "https://www.cmoa.jp/reader/browserviewer/",
        "https://www.cmoa.jp/bib/speedreader/",
        "https://www.cmoa.jp/bib/speedreader/?u0=1",
        "https://www.cmoa.jp/bib/sws/bibGetCntntInfo.php?cid=0000104860_jp_0001",
    ],
)
def test_suitable_rejects_other_urls(url):
    assert not Cmoa.suitable(url)


def test_is_series_is_true_for_a_title_page_only():
    assert Cmoa.is_series(TITLE_URL)
    assert Cmoa.is_series("https://www.cmoa.jp/title/104860")
    assert not Cmoa.is_series(VOLUME_URL)
    assert not Cmoa.is_series(SAMPLE_URL)
    assert not Cmoa.is_series("https://www.cmoa.jp/title/")


# --- parsing --------------------------------------------------------------------------


def test_parse_listing_page_falls_back_to_the_heading_without_a_breadcrumb():
    html = listing_html(
        [lineup_item(f"/title/{TITLE_ID}/vol/2/", "ダイヤのＡ　ａｃｔ２（２）", "100001048600002")], breadcrumb=False
    )
    page = parse_listing_page(html, TITLE_ID)
    assert page.title == "ダイヤのＡ　ａｃｔ２（１）"
    assert [volume.url for volume in page.volumes] == [NEXT_URL]
    assert page.last == 2
    assert (page.writer, page.publisher) == ("寺嶋裕二", "")


# --- episodes -------------------------------------------------------------------------


def test_episode_reads_the_titles_the_pages_and_the_next_volume(client):
    cmoa, session = client()
    episode = cmoa.episode(VOLUME_URL)

    assert episode.url == VOLUME_URL
    assert episode.series_title == "ダイヤのA act2"
    assert episode.episode_title == "ダイヤのＡ　ａｃｔ２（１）"
    assert (episode.writer, episode.publisher) == ("寺嶋裕二", "講談社")
    assert (episode.prev_url, episode.next_url) == (None, NEXT_URL)
    assert [page.width for page in episode.pages] == [392, 392]
    page = urlparse(episode.pages[0].url)
    assert f"{page.scheme}://{page.netloc}{page.path}" == f"{SERVER}/sbcGetImg.php"
    assert parse_qs(page.query) == {
        "cid": [BIB_ID],
        "src": ["pages/a.jpg"],
        "p": [TOKEN],
        "q": ["0"],
        "vm": ["2"],
        "u0": ["1"],
    }
    assert parse_qs(urlparse(episode.pages[1].url).query)["src"] == ["pages/b.jpg"]
    assert episode.pages[0].extra == {"ctbl": IDENTITY_CTBL, "ptbl": SWAPPED_PTBL}
    assert episode.metadata["title_id"] == TITLE_ID
    assert episode.metadata["content_id"] == CONTENT_ID
    assert episode.metadata["bib_id"] == BIB_ID
    assert episode.metadata["reader_url"] == READER_URL
    assert episode.metadata["shop_url"] == VOLUME_URL
    assert episode.metadata["locked"] is False
    assert "ctbl" not in episode.metadata["info"]
    json.dumps(episode.metadata)
    # The lineup (two pages), the sample link that lands on the reader, the API, the page list.
    assert session.calls == [TITLE_URL, TITLE_URL, SAMPLE_URL, INFO_URL, f"{SERVER}/sbcGetCntnt.php"]


def test_episode_calls_the_api_the_way_the_reader_does(client):
    cmoa, session = client()
    cmoa.episode(VOLUME_URL)

    info_index = session.calls.index(INFO_URL)
    params = session.params_seen[info_index]
    assert params["cid"] == BIB_ID
    assert len(params["k"]) == 32
    assert isinstance(params["dmytime"], int)
    assert params["u0"] == "1"
    assert "rurl" not in params
    assert session.headers_seen[info_index]["Referer"] == READER_URL

    list_index = session.calls.index(f"{SERVER}/sbcGetCntnt.php")
    assert session.params_seen[list_index]["cid"] == BIB_ID
    assert session.params_seen[list_index]["p"] == TOKEN
    assert session.params_seen[list_index]["vm"] == "2"
    assert session.params_seen[list_index]["u0"] == "1"
    assert session.headers_seen[list_index]["Referer"] == READER_URL


@pytest.mark.parametrize(
    "url",
    [
        "https://www.cmoa.jp/title/104860/vol/1",
        SAMPLE_URL,
        "https://www.cmoa.jp/reader/sample/?title_id=104860&content_id=100001048600001",
        "https://www.cmoa.jp/reader/browserviewer/content_id/100001048600001/sample_flg/1/",
        READER_URL,
    ],
)
def test_episode_canonicalises_the_url_to_the_volume_page(client, url):
    cmoa, _ = client()
    episode = cmoa.episode(url)
    assert episode.url == VOLUME_URL
    assert episode.series_title == "ダイヤのA act2"
    assert episode.next_url == NEXT_URL


def test_reader_url_is_placed_by_the_api_alone(client):
    cmoa, session = client()
    cmoa.episode(READER_URL)
    # The reader, the API; only then does `ShopURL` say which title to read the lineup of.
    assert session.calls[:2] == [READER_URL, INFO_URL]
    assert session.calls[2:4] == [TITLE_URL, TITLE_URL]


def test_last_volume_has_no_next(client):
    cmoa, _ = client(ContentID="100001048600003", SubTitle="ダイヤのA act2（3）", ShopURL="/title/104860/vol/3/")
    episode = cmoa.episode(LAST_URL)
    assert episode.url == LAST_URL
    assert episode.episode_title == "ダイヤのA act2（3）"
    assert (episode.prev_url, episode.next_url) == (NEXT_URL, None)


def test_volume_the_lineup_does_not_list_is_asked_for_by_its_derived_id(client):
    cmoa, session = client(
        ContentID="100001048600099", SubTitle="ダイヤのA act2（99）", ShopURL="/title/104860/vol/99/"
    )
    episode = cmoa.episode("https://www.cmoa.jp/title/104860/vol/99/")
    assert session.calls[2] == "https://www.cmoa.jp/reader/sample/title_id/104860/content_id/100001048600099/"
    assert episode.url == "https://www.cmoa.jp/title/104860/vol/99/"
    assert episode.series_title == "ダイヤのA act2"
    assert episode.next_url is None


def test_api_that_refuses_the_content_is_locked(client):
    cmoa, _ = client(body={"result": -100, "ShopUserID": "", "eurl": "errorpage.php", "items": [{"ContentID": ""}]})
    episode = cmoa.episode(VOLUME_URL)
    assert episode.pages == ()
    assert not episode.readable
    assert episode.url == VOLUME_URL
    assert episode.series_title == "ダイヤのA act2"
    assert episode.episode_title == "ダイヤのＡ　ａｃｔ２（１）"
    assert episode.next_url == NEXT_URL
    assert episode.metadata["locked"] is True
    json.dumps(episode.metadata)


def test_refused_reader_url_is_titled_by_its_id(client):
    cmoa, _ = client(body={"result": -120, "eurl": "errorpage.php", "items": [{"ContentID": ""}]})
    episode = cmoa.episode("https://www.cmoa.jp/bib/speedreader/?cid=0000999999_jp_0001&u0=1")
    assert not episode.readable
    assert episode.series_title == "0000999999_jp_0001"
    assert episode.episode_title == "0000999999_jp_0001"
    assert episode.next_url is None


def test_store_error_page_is_not_an_episode(client, fake_response):
    cmoa, _ = client({"/reader/sample/": fake_response(text=ERROR_HTML, url=ERROR_URL)})
    with pytest.raises(NotAnEpisodePageError, match="leads to no reader"):
        cmoa.episode(VOLUME_URL)


def test_gone_entry_is_not_an_episode(client, fake_response):
    cmoa, _ = client({"/reader/sample/": fake_response(text="Not Found", status_code=HTTPStatus.NOT_FOUND)})
    with pytest.raises(NotAnEpisodePageError, match="404"):
        cmoa.episode(VOLUME_URL)


def test_other_http_errors_propagate(client, fake_response):
    cmoa, _ = client({"/reader/sample/": fake_response(text="", status_code=HTTPStatus.SERVICE_UNAVAILABLE)})
    with pytest.raises(HTTPStatusError):
        cmoa.episode(VOLUME_URL)


def test_api_on_another_backend_is_unsupported(client):
    cmoa, _ = client(server_type=1)
    with pytest.raises(GetjmangaError, match="ServerType 1"):
        cmoa.episode(VOLUME_URL)


def test_episode_refuses_a_title_page(client):
    cmoa, session = client()
    with pytest.raises(UnsupportedUrlError, match="not a volume page"):
        cmoa.episode(TITLE_URL)
    assert session.calls == []


# --- series ---------------------------------------------------------------------------


@pytest.mark.parametrize("url", [TITLE_URL, "https://www.cmoa.jp/title/104860"])
def test_series_urls_lists_the_volumes_oldest_first(client, url):
    cmoa, session = client()
    assert cmoa.series_urls(url) == [VOLUME_URL, NEXT_URL, LAST_URL]
    # Two lineup pages; the switch to the detailed display mode names more, and is not a page.
    assert session.calls == [TITLE_URL, TITLE_URL]
    assert [params["page"] for params in session.params_seen] == [1, 2]
    assert session.params_seen[0]["disp_mode"] == "easy"


def test_series_urls_stops_at_a_page_that_lists_nothing_new(client, fake_response):
    cmoa, session = client(
        {
            TITLE_URL: [
                fake_response(
                    text=listing_html(
                        [lineup_item(f"/title/{TITLE_ID}/vol/2/", "２", "100001048600002")], pages=("1", "2", "3")
                    )
                )
            ]
        }
    )
    assert cmoa.series_urls(TITLE_URL) == [NEXT_URL]
    assert session.calls == [TITLE_URL, TITLE_URL]


def test_series_urls_raises_on_an_empty_listing(client, fake_response):
    cmoa, _ = client({TITLE_URL: fake_response(text=EMPTY_PAGE)})
    with pytest.raises(NotAnEpisodePageError, match="lists no volume"):
        cmoa.series_urls(TITLE_URL)


def test_series_urls_raises_on_a_missing_title(client, fake_response):
    cmoa, _ = client({TITLE_URL: fake_response(text="", status_code=HTTPStatus.NOT_FOUND)})
    with pytest.raises(NotAnEpisodePageError, match="404"):
        cmoa.series_urls(TITLE_URL)


def test_series_urls_refuses_a_url_of_the_wrong_shape(client):
    cmoa, session = client()
    with pytest.raises(UnsupportedUrlError):
        cmoa.series_urls(VOLUME_URL)
    assert session.calls == []


def test_lineup_is_read_once_per_title(client):
    cmoa, session = client()
    cmoa.series_urls(TITLE_URL)
    cmoa.episode(VOLUME_URL)
    assert session.calls.count(TITLE_URL) == 2


# --- images and downloading -----------------------------------------------------------


def test_image_fetches_the_page_with_the_reader_as_referer(client, fake_response):
    cmoa, session = client(image_route(fake_response))
    episode = cmoa.episode(VOLUME_URL)

    page = cmoa.image(episode.pages[0], episode)

    assert page.size == (392, 392)
    assert page.getpixel((5, 5)) == COLOURS[0]
    assert session.calls[-1] == episode.pages[0].url
    assert session.headers_seen[-1]["Referer"] == READER_URL


# --- the real site --------------------------------------------------------------------

# One URL per known host, free to read without an account: the sample of the
# first volume of ダイヤのA act2, which the store keeps whether or not the
# volume is on a free campaign.
TEST_URLS: dict[str, str] = {
    "www.cmoa.jp": VOLUME_URL,
}


@pytest.mark.network
@pytest.mark.parametrize("host", TEST_URLS)
def test_site_download(tmp_path, host):
    result = Downloader(Cmoa(), tmp_path, only_first=True).download(TEST_URLS[host])
    assert result.status == "saved"
    assert result.episode.series_title == "ダイヤのA act2"
    assert result.episode.episode_title == "ダイヤのＡ　ａｃｔ２（１）"
    assert result.episode.next_url == NEXT_URL
    assert (result.save_dir / "0.jpg").stat().st_size > 0
    # The tables put the 8x8 grid back and drop its padding: the page is served at its size.
    assert Image.open(result.save_dir / "0.jpg").size == (1070, 1600)


@pytest.mark.network
def test_title_page_lists_volumes_oldest_first():
    cmoa = Cmoa()
    assert cmoa.is_series(TITLE_URL)
    urls = cmoa.series_urls(TITLE_URL)
    assert urls[:2] == [VOLUME_URL, NEXT_URL]
    assert len(urls) >= 34
    assert all(Cmoa.suitable(url) for url in urls)


@pytest.mark.network
def test_missing_content_is_not_an_episode_on_the_site():
    with pytest.raises(NotAnEpisodePageError, match="leads to no reader"):
        Cmoa().episode("https://www.cmoa.jp/reader/sample/title_id/104860/content_id/999/")
