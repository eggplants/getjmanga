from __future__ import annotations

import json
from io import BytesIO
from urllib.parse import parse_qs, urlparse

import pytest
from PIL import Image

from getjmanga.errors import GetjmangaError, NotAnEpisodePageError
from getjmanga.extractor import Extractor, Page
from getjmanga.viewers.speedbinb import (
    SERVER_TYPE_DIRECT,
    SERVER_TYPE_REST,
    SERVER_TYPE_SBC,
    Ptimg,
    Tile,
    Transfer,
    content_info,
    decode_table,
    descramble,
    descramble_ptimg,
    fetch_page,
    fetch_ptimg_page,
    page_list,
    parse_content,
    parse_pages,
    parse_ptimg,
    parse_scramble,
    pick_tables,
    split_title,
    viewer_key,
)

CONTENT_ID = "g5JBaom0KiedHfUAbnZwXURtl_001-1"
READER_URL = "https://reader.example/viewer/1"
INFO_URL = "https://reader.example/sws/bibGetCntntInfo"

# What the real viewer sent for CONTENT_ID, and the tables the site answered with.
REAL_KEY = "W_XEufUgVUT5QiqVpKIGohxVomVnQAHo"
REAL_STBL = (
    'pb"^p)yhFsJozY-n)E6hu]A[67%-u)U\\A4[fF@)@ayPE5A1[6ju!Q$iJ6jFpJCh")=a0}"IQi;D>2:j%$3YRiq_T"'
    ",n&TKe%q/eGCp?9_6[nJpK/gU4hk&)`q}Pwz+(3\\"
)
REAL_CTBL = "=8-8+4-DAABCCCCDFFDAGAEKmMchitw21SpxZTNkGn9-srbJfE7qlvu0QyHO_6gAj5aIF4D3RozdPe8CXWYLUBV"
REAL_PTBL = "=8-8-4-DFGGGGEEBEGBFEFAuHVQIJEoqW1bXzdhskawr2nj53xOARUGSBieYP9807gpvDtylN46-_ZTfMKFmCLc"

# A 2x2 grid, two pixels of padding, with the narrow column and the short row last
# on both sides: `BB` (short row per column) + `BB` (narrow column per row) + order.
IDENTITY_CTBL = "=2-2+2-BBBBABCD"
IDENTITY_PTBL = "=2-2-2-BBBBABCD"
SWAPPED_PTBL = "=2-2-2-BBBBBADC"

COLOURS = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0)]


def encode_table(content_id, key, value):
    """The inverse of `decode_table`, so a fake API can hand tables out."""
    seed = 0
    for index, char in enumerate(f"{content_id}:{key}"):
        seed += ord(char) << (index % 16)
    state = (seed & 0x7FFFFFFF) or 0x12345678
    out = []
    for char in json.dumps(value):
        state = ((state >> 1) ^ (0x48200004 if state & 1 else 0)) & 0xFFFFFFFF
        out.append(chr((ord(char) - 32 - state) % 94 + 32))
    return "".join(out)


def served_image(order):
    """A 400x400 served image of a 2x2 grid, tile n painted COLOURS[order[n]] inside its padding."""
    image = Image.new("RGB", (400, 400), (0, 0, 0))
    for index, colour_index in enumerate(order):
        column, row = index % 2, index // 2
        image.paste(Image.new("RGB", (196, 196), COLOURS[colour_index]), (2 + column * 200, 2 + row * 200))
    return image


def png(image):
    raw = BytesIO()
    image.save(raw, "PNG")
    return raw.getvalue()


def content_js(srcs=("pages/a.jpg", "pages/b.jpg"), image_class="default", *, jsonp=True):
    imgs = "".join(
        f'<t-img src="{src}" a="0" orgwidth="392" orgheight="392" id="P{index:04d}"><t-pb>'
        for index, src in enumerate(srcs)
    )
    ttx = f'<html><body><t-case screen.portrait="screen.portrait">{imgs}</t-case></body>'
    body = json.dumps({"result": 1, "ttx": ttx, "ImageClass": image_class})
    return f"DataGet_Content({body})" if jsonp else body


class Reader(Extractor):
    """An extractor that only lends its session to the viewer."""

    NAME = "reader"
    HOSTS = ("reader.example",)

    def episode(self, url):
        raise NotImplementedError


class InfoResponse:
    """A `bibGetCntntInfo` answer that encrypts its tables with whatever `k` was sent.

    `tables` is True for the real ones, False for ones keyed so they do not
    decode, or the JSON value to encode in place of the lists.
    """

    def __init__(self, session, *, server_type=SERVER_TYPE_DIRECT, tables=True, body=None, **item):
        self.session = session
        self.server_type = server_type
        self.tables = tables
        self.body = body
        self.item = item
        self.url = None
        self.is_success = True

    def raise_for_status(self):
        pass

    def json(self):
        if self.body is not None:
            return self.body
        params = self.session.params_seen[-1]
        key = "another-key" if self.tables is False else params["k"]
        ctbl, ptbl = ([IDENTITY_CTBL], [SWAPPED_PTBL]) if self.tables in (True, False) else (self.tables, self.tables)
        item = {
            "ContentID": CONTENT_ID,
            "ContentsServer": "//cdn.example/sbc/",
            "ServerType": self.server_type,
            "ViewMode": 2,
            "p": "token",
            "stbl": "not read",
            "ctbl": encode_table(params["cid"], key, ctbl),
            "ptbl": encode_table(params["cid"], key, ptbl),
            **self.item,
        }
        return {"result": 1, "items": [item]}


@pytest.fixture
def reader(fake_session, fake_response):
    """A `Reader` on a fake site: the API, the three page lists and a served page."""

    def build(routes=None, **info):
        session = fake_session({})
        session.routes = {
            "bibGetCntntInfo": InfoResponse(session, **info),
            "content.js": fake_response(text=content_js()),
            "/sbc/content": fake_response(text=content_js(jsonp=False)),
            "sbcGetCntnt.php": fake_response(text=content_js()),
            "M_H.jpg": fake_response(png(served_image([1, 0, 3, 2])), content_type="image/png"),
            **(routes or {}),
        }
        return Reader(session), session

    return build


# --- the key and the tables ---------------------------------------------------------------


def test_viewer_key_interleaves_the_checksum_the_viewer_does():
    assert viewer_key(CONTENT_ID, REAL_KEY[::2]) == REAL_KEY


def test_viewer_key_is_random_without_a_nonce():
    first, second = viewer_key(CONTENT_ID), viewer_key(CONTENT_ID)
    assert len(first) == len(second) == 32
    assert first != second


def test_decode_table_reads_what_the_site_sent():
    assert decode_table(CONTENT_ID, REAL_KEY, REAL_STBL)[:8] == [3, 4, 6, 4, 7, 1, 2, 1]


def test_decode_table_raises_with_the_wrong_key():
    with pytest.raises(GetjmangaError, match="key"):
        decode_table(CONTENT_ID, "another-key", REAL_STBL)


def test_pick_tables_uses_the_file_name_only():
    ctbl = [f"c{index}" for index in range(8)]
    ptbl = [f"p{index}" for index in range(8)]
    assert pick_tables("pages/zC4iHmGp.jpg", ctbl, ptbl) == pick_tables("zC4iHmGp.jpg", ctbl, ptbl)
    # 'z','4','H','G','.','p' at even positions: 122+52+72+71+46+112 = 475 -> 3; odd: 67+105+109+112+106+103 = 602 -> 2.
    assert pick_tables("pages/zC4iHmGp.jpg", ctbl, ptbl) == ("c2", "p3")
    assert pick_tables("pages/zC4iHmGp.jpg", [], []) == ("", "")


# --- descrambling -------------------------------------------------------------------------


def test_parse_scramble_matches_the_viewer_on_a_real_page():
    # What the site's own speedbinb.js computed for this table pair on a 1190x1664 page.
    scramble = parse_scramble(REAL_CTBL, REAL_PTBL)
    assert scramble is not None
    assert scramble.page_size(1190, 1664) == (1126, 1600)
    assert scramble.transfers(1190, 1664)[:3] == [
        Tile(4, 4, 141, 200, 0, 1400),
        Tile(153, 4, 139, 200, 0, 1200),
        Tile(300, 4, 141, 200, 564, 1000),
    ]
    assert sorted(scramble.order) == list(range(64))


@pytest.mark.parametrize(
    ("ctbl", "ptbl"),
    [
        ("8-8-dcabcGcdbccFgd", "8-8-dGcFaGcGFceDbD"),  # the decoy format
        ("=2-2-2-BBBBABCD", "=2-2-2-BBBBABCD"),  # both destination-signed
        ("=2-2+2-BBBBABCD", "=2-3-2-BBBBBABCD"),  # grids disagree
        ("=2-2+2-BBBBABC", "=2-2-2-BBBBABCD"),  # too few indices
        ("=9-9+2-" + "A" * 99, "=9-9-2-" + "A" * 99),  # too big a grid
    ],
)
def test_parse_scramble_rejects_bad_tables(ctbl, ptbl):
    with pytest.raises(GetjmangaError):
        parse_scramble(ctbl, ptbl)


def test_descramble_strips_the_padding_and_keeps_an_identity_order():
    page = descramble(served_image([0, 1, 2, 3]), IDENTITY_CTBL, IDENTITY_PTBL)
    assert page.size == (392, 392)
    assert [page.getpixel((column * 196 + 5, row * 196 + 5)) for row in range(2) for column in range(2)] == COLOURS
    # The corners of every tile are content, not padding.
    assert page.getpixel((195, 195)) == COLOURS[0]
    assert page.getpixel((196, 196)) == COLOURS[3]


def test_descramble_moves_the_tiles_where_the_tables_say():
    # Served tile n holds page tile SWAPPED[n]: 0<->1 and 2<->3.
    page = descramble(served_image([1, 0, 3, 2]), IDENTITY_CTBL, SWAPPED_PTBL)
    assert [page.getpixel((column * 196 + 5, row * 196 + 5)) for row in range(2) for column in range(2)] == COLOURS


def test_descramble_leaves_a_small_image_alone():
    image = Image.new("RGB", (50, 50), COLOURS[2])
    assert descramble(image, IDENTITY_CTBL, SWAPPED_PTBL).tobytes() == image.tobytes()


def test_descramble_with_empty_tables_is_the_image_itself():
    image = Image.new("RGB", (400, 400))
    assert descramble(image, "", "") is image


# --- the page list --------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["nope(", 'DataGet_Content({"result":0,"ttx":""})', "{}"])
def test_parse_content_rejects_what_is_not_a_page_list(text):
    with pytest.raises(GetjmangaError):
        parse_content(text)


def test_parse_content_reads_plain_json_too():
    assert parse_content(content_js(jsonp=False))["result"] == 1


def test_parse_pages_reads_the_case_only():
    ttx = '<t-case><t-img src="a"><t-img src="b"></t-case><t-nocase><t-img src="ab"></t-nocase>'
    assert [page["src"] for page in parse_pages(ttx)] == ["a", "b"]


def test_parse_pages_without_a_case_reads_every_tag():
    assert [page["src"] for page in parse_pages('<t-img src="a"><t-img src="b">')] == ["a", "b"]


# --- the API ---------------------------------------------------------------------------------


def test_content_info_calls_the_api_the_way_the_viewer_does(reader):
    extractor, session = reader()
    content = content_info(extractor, INFO_URL, CONTENT_ID, referer=READER_URL, params={"u0": "1"})

    assert content is not None
    assert session.calls == [INFO_URL]
    params = session.params_seen[0]
    assert params["cid"] == CONTENT_ID
    assert params["k"] == viewer_key(CONTENT_ID, params["k"][::2]) == content.key
    assert isinstance(params["dmytime"], int)
    assert params["u0"] == "1"
    assert session.headers_seen[0]["Referer"] == READER_URL
    # The contents server is resolved against the endpoint and loses its slash.
    assert content.server == "https://cdn.example/sbc"
    assert content.server_type == SERVER_TYPE_DIRECT
    assert content.ctbl == [IDENTITY_CTBL]
    assert content.ptbl == [SWAPPED_PTBL]
    assert content.token == "token"
    assert content.view_mode == "2"
    assert "ctbl" not in content.info
    assert content.info["ContentID"] == CONTENT_ID


def test_content_info_is_none_when_the_api_refuses(reader):
    extractor, _ = reader(body={"result": 0, "items": []})
    assert content_info(extractor, INFO_URL, CONTENT_ID, referer=READER_URL) is None


@pytest.mark.parametrize("body", [[], {"result": 1, "items": [{"ContentID": CONTENT_ID}]}])
def test_content_info_raises_when_the_api_describes_nothing(reader, body):
    extractor, _ = reader(body=body)
    with pytest.raises(NotAnEpisodePageError, match="did not describe"):
        content_info(extractor, INFO_URL, CONTENT_ID, referer=READER_URL)


def test_content_info_refuses_a_backend_the_caller_cannot_serve(reader):
    extractor, _ = reader(server_type=SERVER_TYPE_REST)
    with pytest.raises(GetjmangaError, match="ServerType 2"):
        content_info(extractor, INFO_URL, CONTENT_ID, referer=READER_URL, server_types=frozenset({SERVER_TYPE_SBC}))


def test_content_info_refuses_tables_that_do_not_decode(reader):
    extractor, _ = reader(tables=False)
    with pytest.raises(GetjmangaError, match="key"):
        content_info(extractor, INFO_URL, CONTENT_ID, referer=READER_URL)


def test_content_info_refuses_tables_that_are_not_lists(reader):
    extractor, session = reader()
    session.routes["bibGetCntntInfo"] = InfoResponse(session, tables={})
    with pytest.raises(GetjmangaError, match="no scramble tables"):
        content_info(extractor, INFO_URL, CONTENT_ID, referer=READER_URL)


def test_page_list_stamps_every_request_with_the_content_date(reader):
    extractor, session = reader(server_type=SERVER_TYPE_REST, ContentDate="20260306154555")
    content = content_info(extractor, INFO_URL, CONTENT_ID, referer=READER_URL)
    assert content is not None
    book = page_list(extractor, content, referer=READER_URL)

    assert session.params_seen[-1] == {"dmytime": "20260306154555"}
    assert book.pages[0].url == "https://cdn.example/sbc/img/pages/a.jpg?dmytime=20260306154555"


def test_page_list_on_the_static_backend_reads_content_js(reader, fake_response):
    extractor, session = reader()
    content = content_info(extractor, INFO_URL, CONTENT_ID, referer=READER_URL)
    assert content is not None
    book = page_list(extractor, content, referer=READER_URL)

    assert session.calls[-1] == "https://cdn.example/sbc/content.js"
    assert "dmytime" in session.params_seen[-1]
    assert session.headers_seen[-1]["Referer"] == READER_URL
    assert [page.url for page in book.pages] == [
        "https://cdn.example/sbc/pages/a.jpg/M_H.jpg",
        "https://cdn.example/sbc/pages/b.jpg/M_H.jpg",
    ]
    assert book.pages[0].width == book.pages[0].height == 392
    assert book.pages[0].extra == {"ctbl": IDENTITY_CTBL, "ptbl": SWAPPED_PTBL}
    assert book.body["ImageClass"] == "default"

    session.routes["content.js"] = fake_response(text=content_js(image_class="singlequality"))
    assert page_list(extractor, content, referer=READER_URL).pages[0].url.endswith("/pages/a.jpg/M.jpg")


def test_page_list_on_the_rest_backend_reads_content(reader):
    extractor, session = reader(server_type=SERVER_TYPE_REST)
    content = content_info(extractor, INFO_URL, CONTENT_ID, referer=READER_URL)
    assert content is not None
    book = page_list(extractor, content, referer=READER_URL, params={"u0": "1"})

    assert session.calls[-1] == "https://cdn.example/sbc/content"
    assert session.params_seen[-1] == {"u0": "1"}
    assert [page.url for page in book.pages] == [
        "https://cdn.example/sbc/img/pages/a.jpg?u0=1",
        "https://cdn.example/sbc/img/pages/b.jpg?u0=1",
    ]
    assert page_list(extractor, content, referer=READER_URL).pages[0].url == "https://cdn.example/sbc/img/pages/a.jpg"


def test_page_list_on_the_sbc_backend_sends_the_token(reader):
    extractor, session = reader(server_type=SERVER_TYPE_SBC)
    content = content_info(extractor, INFO_URL, CONTENT_ID, referer=READER_URL, params={"u0": "1"})
    assert content is not None
    book = page_list(extractor, content, referer=READER_URL, params={"u0": "1"})

    assert session.calls[-1] == "https://cdn.example/sbc/sbcGetCntnt.php"
    params = session.params_seen[-1]
    assert (params["cid"], params["p"], params["vm"], params["u0"]) == (CONTENT_ID, "token", "2", "1")
    assert "dmytime" in params
    page = urlparse(book.pages[0].url)
    assert f"{page.scheme}://{page.netloc}{page.path}" == "https://cdn.example/sbc/sbcGetImg.php"
    assert parse_qs(page.query) == {
        "cid": [CONTENT_ID],
        "src": ["pages/a.jpg"],
        "p": ["token"],
        "q": ["0"],
        "vm": ["2"],
        "u0": ["1"],
    }


def test_fetch_page_descrambles_with_the_referer_given(reader):
    extractor, session = reader()
    page = Page(url="https://cdn.example/sbc/pages/a.jpg/M_H.jpg", extra={"ctbl": IDENTITY_CTBL, "ptbl": SWAPPED_PTBL})
    image = fetch_page(extractor, page, referer=READER_URL)

    assert image.size == (392, 392)
    assert [image.getpixel((column * 196 + 5, row * 196 + 5)) for row in range(2) for column in range(2)] == COLOURS
    assert session.calls == [page.url]
    assert session.headers_seen[-1]["Referer"] == READER_URL


# --- titles ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("title", "series", "expected"),
    [
        ("OLと人魚　OLと人魚", "", ("OLと人魚", "OLと人魚")),
        ("かくりよ骨董収集録  １話「骨董屋」", "", ("かくりよ骨董収集録", "１話「骨董屋」")),
        ("人魚喰らわば　第一話　山椒魚", "", ("人魚喰らわば", "第一話　山椒魚")),
        # A single plain space is not a separator on its own, only once the series is known.
        ("かくりよ骨董収集録 ２話「犬筥」", "", ("かくりよ骨董収集録 ２話「犬筥」", "かくりよ骨董収集録 ２話「犬筥」")),
        ("かくりよ骨董収集録 ２話「犬筥」", "かくりよ骨董収集録", ("かくりよ骨董収集録", "２話「犬筥」")),
        # The known series has to be followed by a gap, not just be a prefix.
        ("Solo", "S", ("S", "Solo")),
        ("Solo", "Solo", ("Solo", "Solo")),
        ("A　B", "Z", ("Z", "B")),
        ("", "", ("", "")),
    ],
)
def test_split_title(title, series, expected):
    assert split_title(title, series) == expected


# --- the static export ------------------------------------------------------------------------

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
PTIMG_URL = "https://x/data/0001.ptimg.json"


def scrambled_resource() -> Image.Image:
    """The resource `PTIMG` describes: four solid tiles, each where a transfer expects it."""
    image = Image.new("RGB", (24, 24), "white")
    for transfer in parse_ptimg(PTIMG, PTIMG_URL).transfers:
        colour = (transfer.dest_x * 10, transfer.dest_y * 10, 200)
        tile = Image.new("RGB", (transfer.width, transfer.height), colour)
        image.paste(tile, (transfer.x, transfer.y))
    return image


def test_parse_ptimg_names_the_resources_next_to_the_json():
    ptimg = parse_ptimg(PTIMG, PTIMG_URL)
    assert ptimg.resources == {"i": "https://x/data/0001.jpg"}
    assert (ptimg.width, ptimg.height) == (20, 20)
    assert ptimg.transfers[0] == Transfer("i", 2, 2, 10, 10, 10, 10)


@pytest.mark.parametrize(
    "data",
    [
        {"ptimg-version": 2, "resources": PTIMG["resources"], "views": PTIMG["views"]},
        {"ptimg-version": 1, "resources": {}, "views": PTIMG["views"]},
        {"ptimg-version": 1, "resources": PTIMG["resources"], "views": []},
        {"ptimg-version": 1, "resources": PTIMG["resources"], "views": [{"width": 1, "height": 1, "coords": []}]},
        {"ptimg-version": 1, "resources": PTIMG["resources"], "views": [{"width": 1, "height": 1, "coords": ["x"]}]},
        {
            "ptimg-version": 1,
            "resources": PTIMG["resources"],
            "views": [{"width": 1, "height": 1, "coords": ["j:0,0+1,1>0,0"]}],
        },
    ],
)
def test_parse_ptimg_rejects_what_the_reader_would(data):
    with pytest.raises(GetjmangaError):
        parse_ptimg(data, "https://x/0001.ptimg.json")


def test_descramble_ptimg_puts_the_tiles_where_the_transfers_say():
    ptimg = parse_ptimg(PTIMG, PTIMG_URL)
    page = descramble_ptimg(ptimg, {"i": scrambled_resource()})

    assert page.size == (20, 20)
    for transfer in ptimg.transfers:
        assert page.getpixel((transfer.dest_x, transfer.dest_y)) == (transfer.dest_x * 10, transfer.dest_y * 10, 200)
        assert page.getpixel((transfer.dest_x + 9, transfer.dest_y + 9)) == (
            transfer.dest_x * 10,
            transfer.dest_y * 10,
            200,
        )


def test_descramble_ptimg_leaves_uncovered_canvas_white_and_keeps_grayscale():
    ptimg = Ptimg(
        resources={"i": "https://x/0001.jpg"},
        width=4,
        height=4,
        transfers=(Transfer("i", 0, 0, 2, 2, 2, 2),),
    )
    page = descramble_ptimg(ptimg, {"i": Image.new("L", (2, 2), 0)})

    assert page.mode == "L"
    assert page.getpixel((0, 0)) == 255
    assert page.getpixel((3, 3)) == 0


def test_fetch_ptimg_page_reads_the_json_then_the_resource(fake_session, fake_response):
    session = fake_session(
        {
            "/data/0001.ptimg.json": fake_response(payload=PTIMG, content_type="application/json"),
            "/data/0001.jpg": fake_response(png(scrambled_resource()), content_type="image/png"),
        },
    )
    page = fetch_ptimg_page(Reader(session), PTIMG_URL, referer=READER_URL)

    assert page.size == (20, 20)
    assert page.getpixel((5, 5)) == (0, 0, 200)
    assert session.calls == [PTIMG_URL, "https://x/data/0001.jpg"]
    assert all(headers["Referer"] == READER_URL for headers in session.headers_seen)
