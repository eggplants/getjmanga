from __future__ import annotations

import json
from http import HTTPStatus

import pytest
from httpx import HTTPStatusError

from getjmanga.errors import GetjmangaError, NotAnEpisodePageError
from getjmanga.extractor import Extractor
from getjmanga.viewers.yondemill import content_url, parse_content_page, read

CONTENT_URL = "https://www.yondemill.jp/contents/64823"
WORK_URL = "https://webcomic.ohtabooks.com/kishotenten/"
BINB_ID = "3b4a9926-e2ce-429c-8e09-6f331ccfcf1d_1728028949"
TOKEN = "1b0aaf3c3085435bb47a4d4396b83390"
READER_URL = f"https://binb.bricks.pub/contents/{BINB_ID}/speed_reader?u0={TOKEN}&u1=redirect"
INFO_URL = f"https://console.binb.bricks.pub/bibGetCntntInfo?u0={TOKEN}"
SERVER = f"https://s3-ap-northeast-1.amazonaws.com/binb.bricks.pub/output/{BINB_ID}/member_trial"

IDENTITY_CTBL = "=2-2+2-BBBBABCD"
SWAPPED_PTBL = "=2-2-2-BBBBBADC"


def content_html(title="起承転転　第1話", *, work_link=True, heading=True):
    """A content page: the title, the author, the label, the link back to the work page."""
    back = f'<div class="text-center mb-3"><a href="{WORK_URL}">作品TOPへ戻る</a></div>' if work_link else ""
    head = f'<div class="card-summary-header clearfix"><h1 class="card-title h4">{title}</h1></div>' if heading else ""
    return f"""
<html><head><title>{title} - YONDEMILL</title>
<script>var sales = '';
sales = 'OFF';read_right = 'no';layout_type = 'reflowable';reader_type = 'binb_infoview';</script>
</head><body>
<a class="navbar-brand" href="/">YONDEMILL</a>
<div class="card mt-3"><a class="button" href="/contents/64823?view=1">読む</a></div>
<div class="card card-primary"><div class="card-summary closed" id="description">
{head}
<div class="card-summary-block"><p>雁須磨子 著</p><p></p><p><a href="/labels/29">太田出版</a></p></div>
<div class="card-summary-block"><p>誰かの妻にもならず。</p></div>
</div></div>
{back}
<a href="/">YONDEMILL</a>
</body></html>
"""


def stub_html(reader_url=READER_URL):
    script = f"<script>location.href='{reader_url}';</script>" if reader_url else ""
    return f"<html><head><title>起承転転　第1話</title></head><body>{script}</body></html>"


def reader_html(*, viewer=True):
    content = (
        f'<div class="pages" id="content" data-ptbinb="{INFO_URL}" data-ptbinb-cid="{BINB_ID}"></div>' if viewer else ""
    )
    return f"<html><head><base href='/speedreader/'></head><body>{content}</body></html>"


def content_js(srcs=("pages/a.jpg", "pages/b.jpg")):
    imgs = "".join(f'<t-img src="{src}" orgwidth="392" orgheight="392">' for src in srcs)
    body = {"result": 1, "ttx": f"<t-case>{imgs}</t-case>", "ImageClass": "default", "AddressList": [[0, 1, 0, 1]]}
    return "DataGet_Content(" + json.dumps(body) + ")"


def encode_table(content_id, key, value):
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
    def __init__(self, session, *, server_type=1, body=None, **item):
        self.session = session
        self.server_type = server_type
        self.body = body
        self.item = item
        self.url = None
        self.is_success = True

    def raise_for_status(self):
        pass

    def json(self):
        if self.body is not None:
            return self.body
        key = self.session.params_seen[-1]["k"]
        item = {
            "ContentID": BINB_ID,
            "ContentsServer": SERVER + "/",
            "ServerType": self.server_type,
            "Title": "起承転転　第1話",
            "ViewMode": 3,
            "ctbl": encode_table(BINB_ID, key, [IDENTITY_CTBL]),
            "ptbl": encode_table(BINB_ID, key, [SWAPPED_PTBL]),
            **self.item,
        }
        return {"result": 1, "items": [item]}


class Reader(Extractor):
    NAME = "reader"
    HOSTS = ("www.yondemill.jp",)

    def episode(self, url):
        raise NotImplementedError


@pytest.fixture
def client(fake_session, fake_response):
    """A `Reader` on a fake YONDEMILL: the content page, its stub, the reader, the API, content.js."""

    def build(routes=None, **info):
        session = fake_session({})
        # The stub route must come before the content page's: routes match by substring.
        session.routes = {
            "/contents/64823?view=1": fake_response(text=stub_html()),
            "/contents/64823": fake_response(text=content_html()),
            "/speed_reader": fake_response(text=reader_html()),
            "bibGetCntntInfo": InfoResponse(session, **info),
            "content.js": fake_response(text=content_js()),
        }
        session.routes.update(routes or {})
        return Reader(session), session

    return build


# --- URLs and pages -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (CONTENT_URL, CONTENT_URL),
        ("https://yondemill.jp/contents/64823?view=1&u0=1", CONTENT_URL),
        ("https://www.yondemill.jp/contents/64823/", CONTENT_URL),
        ("https://www.yondemill.jp/ebooks/64823", None),
        ("https://www.yondemill.jp/contents/", None),
    ],
)
def test_content_url_canonicalises_a_content_link(url, expected):
    assert content_url(url) == expected


def test_parse_content_page_reads_the_titles_the_author_the_label_and_the_links():
    content = parse_content_page(content_html(), CONTENT_URL)
    assert content.url == CONTENT_URL
    assert content.title == "起承転転　第1話"
    assert content.author == "雁須磨子 著"
    assert content.label == "太田出版"
    assert content.flags == {
        "sales": "OFF",
        "read_right": "no",
        "layout_type": "reflowable",
        "reader_type": "binb_infoview",
    }
    # Absolute, in order, deduplicated.
    assert content.links == (
        "https://www.yondemill.jp/",
        "https://www.yondemill.jp/contents/64823?view=1",
        "https://www.yondemill.jp/labels/29",
        WORK_URL,
    )


def test_parse_content_page_refuses_a_page_without_a_content():
    with pytest.raises(NotAnEpisodePageError, match="no content"):
        parse_content_page(content_html(heading=False), CONTENT_URL)


# --- reading ------------------------------------------------------------------------------


def test_read_opens_the_reader_and_lists_the_pages(client):
    reader, session = client()
    reading = read(reader, CONTENT_URL)

    assert reading.content_id == "64823"
    assert reading.content.title == "起承転転　第1話"
    assert not reading.locked
    assert reading.opened is not None
    assert reading.opened.reader_url == READER_URL
    assert reading.opened.binb_id == BINB_ID
    assert reading.opened.info.server == SERVER
    assert reading.opened.book.body["AddressList"] == [[0, 1, 0, 1]]
    assert [page.url for page in reading.pages] == [f"{SERVER}/pages/a.jpg/M_H.jpg", f"{SERVER}/pages/b.jpg/M_H.jpg"]
    # The content page, the stub (with the content as Referer), the reader, the API, content.js.
    assert session.calls == [CONTENT_URL, f"{CONTENT_URL}?view=1", READER_URL, INFO_URL, f"{SERVER}/content.js"]
    assert session.headers_seen[1]["Referer"] == CONTENT_URL
    assert session.headers_seen[2]["Referer"] == CONTENT_URL
    assert session.headers_seen[3]["Referer"] == READER_URL
    assert session.params_seen[3]["cid"] == BINB_ID


def test_read_is_locked_when_the_stub_opens_no_reader(client, fake_response):
    reader, session = client({"/contents/64823?view=1": fake_response(text=stub_html(reader_url=""))})
    reading = read(reader, CONTENT_URL)

    assert reading.locked
    assert reading.opened is None
    assert reading.pages == ()
    assert reading.content.title == "起承転転　第1話"
    assert not any("speed_reader" in call for call in session.calls)


def test_read_raises_on_a_taken_down_content(client, fake_response):
    reader, _ = client({"/contents/64823": fake_response(text="404", status_code=HTTPStatus.NOT_FOUND)})
    with pytest.raises(NotAnEpisodePageError, match="404"):
        read(reader, CONTENT_URL)


def test_read_propagates_other_http_errors(client, fake_response):
    reader, _ = client({"/contents/64823": fake_response(text="", status_code=HTTPStatus.SERVICE_UNAVAILABLE)})
    with pytest.raises(HTTPStatusError):
        read(reader, CONTENT_URL)


def test_read_raises_when_the_reader_has_no_viewer(client, fake_response):
    reader, _ = client({"/speed_reader": fake_response(text=reader_html(viewer=False))})
    with pytest.raises(NotAnEpisodePageError, match="no SpeedBinb viewer"):
        read(reader, CONTENT_URL)


def test_read_raises_when_the_api_refuses_the_content(client):
    reader, _ = client(body={"result": 0, "items": []})
    with pytest.raises(NotAnEpisodePageError, match="did not describe"):
        read(reader, CONTENT_URL)


def test_read_refuses_a_content_on_another_backend(client):
    reader, _ = client(server_type=0)
    with pytest.raises(GetjmangaError, match="ServerType 0"):
        read(reader, CONTENT_URL)
