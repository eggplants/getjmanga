from __future__ import annotations

import json
import struct
import zlib
from datetime import date
from http import HTTPStatus
from io import BytesIO

import pytest
from PIL import Image

from getjmanga.downloader import Downloader
from getjmanga.errors import GetjmangaError, NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractors.wings import (
    Wings,
    caption_title,
    parse_book,
    split_title,
    stitch,
    swf_jpegs,
    tile_grid,
)

WORK_URL = "https://www.shinshokan.com/webwings/title80.html"
EPISODE_URL = "https://www.shinshokan.com/webwings/contents/title80-202607-0/"
NEXT_URL = "https://www.shinshokan.com/webwings/contents/title80-202607-1/"
LAST_URL = "https://www.shinshokan.com/webwings/contents/title80-202608/"
OLD_WORK_URL = "https://www.shinshokan.com/webwings/title20.html"
SMOOZY_URL = "https://www.shinshokan.com/webwings/contents/punichan/"
SLICED_URL = "https://www.shinshokan.com/webwings/contents/title75c/"

# A work page as the magazine writes it: the title, three viewer buttons (one
# of them commented out, one pointing at a store, one repeated) and the rest.
WORK_HTML = """
<html><head><title>新書館：コミック＆ノヴェル[ウェブマガジンウィングスサイト]</title></head><body>
<h1 class="logo magazine"><a class="webwings" href="./">ウェブマガジンウィングス</a></h1>
<section class="block detail">
  <div class="block_head mgb_0"><h3 class="title">リヨンでメルシー！ 〜気ままなフランス旅日記〜</h3></div>
  <div class="block_body">
    <p class="author">[作] 野宮レナ</p>
    <div class="float_wrap">
      <div class="to_viewer left">
        <p class="title">第0話を読む</p>
        <div class="btn_red">
          <a href="https://www.shinshokan.com/webwings/contents/title80-202607-0/"><span>新書館ビューア</span></a>
        </div><br />
        <p class="title">第1話はこちらから</p>
        <div class="btn_red">
          <a href="https://www.shinshokan.com/webwings/contents/title80-202607-1/"><span>新書館ビューワー</span></a>
        </div>
<!--
        <div class="btn_red">
          <a href="https://www.shinshokan.com/webwings/contents/punichan/index.html"><span>新書館ビューア</span></a>
        </div>
-->
      </div>
      <div class="to_viewer right">
        <p class="title">第2話はこちらから</p>
        <div class="btn_red">
          <a href="https://www.shinshokan.com/webwings/contents/title80-202608/"><span>新書館ビューワー</span></a>
        </div>
        <div class="btn_red">
          <a href="https://ebookjapan.yahoo.co.jp/books/966374/"><span>ebookjapan</span></a>
        </div>
        <div class="btn_red">
          <a href="https://www.shinshokan.com/webwings/contents/title80-202608/"><span>新書館ビューワー</span></a>
        </div>
      </div>
    </div>
    <section class="block comment"><div class="block_head"><h4 class="title">先生からのコメント</h4></div></section>
  </div>
</section>
</body></html>
"""

# A work page whose only viewer button opens the Flash-era viewer.
OLD_WORK_HTML = """
<html><body>
<section class="block detail">
  <div class="block_head mgb_0"><h3 class="title">プニちゃん</h3></div>
  <div class="to_viewer left">
    <p class="title">第一話を読む</p>
    <div class="btn_red"><a href="https://ebookjapan.yahoo.co.jp/books/531898/"><span>ebookjapan</span></a></div>
    <div class="btn_red">
      <a href="https://www.shinshokan.com/webwings/contents/punichan/index.html"><span>新書館ビューア</span></a>
    </div>
  </div>
  <div class="to_viewer right">
    <p class="title">最新話はこちらから</p>
    <div class="btn_non"><span>Yahoo!ブックストア</span></div>
  </div>
</section>
</body></html>
"""

# A work page selling everything elsewhere.
EMPTY_WORK_HTML = """
<html><body>
<section class="block detail">
  <div class="block_head mgb_0"><h3 class="title">コランタン号の航海</h3></div>
  <div class="to_viewer left"><p class="title">第一話を読む</p>
    <div class="btn_red"><a href="https://www.shinshokan.com/comic/tameshiyomi/61882-6/index.html"><span>新書館ビューア</span></a></div>
  </div>
</section>
</body></html>
"""

# A FLIPPER U viewer page: the app mount and the script tags are all `episode()` looks at.
FLIPPER_HTML = """
<!DOCTYPE html><html lang="ja"><head><meta charset="UTF-8">
<title>リヨンでメルシー！ 〜気ままなフランス旅日記〜第0話</title>
<script type="text/javascript" src="flipper3js/tcoredirector.js"></script>
<script src="./html5/js/flipper.js?c=20260724220400" type="text/javascript" charset="utf-8"></script>
</head><body><!--version 5.0.10-1 -->
<div id="flipper-app" class="header-space" bookpath="./" imagepath="./"><div id="flipper-component"></div></div>
</body></html>
"""

# The SMOOZY (Flash) viewer page.
SMOOZY_HTML = """
<?xml version="1.0" encoding="utf-8"?>
<html><head><meta http-equiv="Content-Type" content="text/html; charset=utf-8" />
<script type="text/javascript" src="./js/swfobject.js"></script>
<title>プニちゃん／テクノサマタ</title></head>
<body><div id="flashcontent"><script type="text/javascript">
var so = new SWFObject('smoozy.swf', 'movie', '100%', '100%', '8', '#FFFFFF');
</script></div></body></html>
"""


def book_xml(
    title="リヨンでメルシー！ 〜気ままなフランス旅日記〜第0話",
    version="5.0.10",
    data="MRYYBJED762PW,MRYYBK7NWW7KI,MRYYBKTH0GN03",
    *,
    magnification=2,
    width=634,
    height=900,
    slice_width=400,
    slice_height=400,
    lock="",
    cdata=True,
):
    """A FLIPPER `book.xml`; FLIPPER U wraps its strings in CDATA, FLIPPER 3 does not."""

    def text(value):
        return f"<![CDATA[{value}]]>" if cdata else value

    return f"""<?xml version="1.0" encoding="utf-8"?>
<setting xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" type="Object">
  <flipperVersion type="String">{version}</flipperVersion>
  <keycode type="String">RbGmRgbK3LpEJU5ZGaQMZfUmM2BuYB/3R0TSOtTaAkoG6H6xnoH24ptOgvI9aLFZ=</keycode>
  <publishDate type="String">{text("2026/07/24 22:04:00")}</publishDate>
  <bookInformation type="Object">
    <bookID type="String">{text("UFL-8886EB42")}</bookID>
    <bookTitle type="String">{text(title)}</bookTitle>
    <bookComment type="String" />
    <total type="Number">{len(data.split(","))}</total>
    <maxMagnification type="Number">{magnification}</maxMagnification>
    <bookDirection type="String">l2r</bookDirection>
    <startPageSetting type="Number">1</startPageSetting>
    <allowPrint type="Boolean">false</allowPrint>
    <pageWidth type="Number">{width}</pageWidth>
    <pageHeight type="Number">{height}</pageHeight>
    <sliceWidth type="Number">{slice_width}</sliceWidth>
    <sliceHeight type="Number">{slice_height}</sliceHeight>
    <data type="String">{text(data)}</data>
    <label type="String">{text("1,2,3")}</label>
    <labelDisplay type="Boolean">false</labelDisplay>
  </bookInformation>
  <skinOption htmlSkinType="default">
    <visualIndex label="label" visible="false" init="close" />
    <url dispUrl="" visible="false" />
    <embedHtml url="../index.html" visible="true" />
    <contentPasswordHash>{lock}</contentPasswordHash>
  </skinOption>
  <html5setting><language>{text("ja")}</language></html5setting>
</setting>
"""


DATA_XML = """<?xml version="1.0" encoding="UTF-8"?>
<presentation>
  <chapter title="index">
    <slide duration="7" id="0" title="0" url="./swf/0.swf">
      <page page="0" title=""></page>
      <page page="1" title=""></page>
    </slide>
    <slide duration="7" id="1" title="1" url="./swf/1.swf">
      <page page="2" title=""></page>
      <page page="3" title=""></page>
    </slide>
  </chapter>
</presentation>
"""

BOOK_CONF = (
    "PAGEWIDTH=458&PAGEHEIGHT=650&PAGESHIFT=2&SHIFTPAGENUM=4&BACKPAGECOLOR=0X8A90BA&FLIPSPEED=1"
    "&DIRECTION=right&SHADOW=true&menu_bt=false&bgcolor=[0x7dbbcf]+&"
)

COLOURS = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0), (0, 255, 255), (255, 0, 255)]


def jpeg_bytes(colour, size=(8, 8)):
    raw = BytesIO()
    Image.new("RGB", size, colour).save(raw, "JPEG", quality=100)
    return raw.getvalue()


def swf_tag(code, payload):
    if len(payload) < 0x3F:
        return struct.pack("<H", (code << 6) | len(payload)) + payload
    return struct.pack("<HI", (code << 6) | 0x3F, len(payload)) + payload


def make_swf(jpegs, *, compressed=False, erroneous_header=True, jpeg3=False):
    """A SWF holding `jpegs` as DefineBitsJPEG2 (or JPEG3) tags, the way SMOOZY's spreads are made."""
    body = b"\x00" + struct.pack("<HH", 0x0C00, 1)  # an empty RECT, 12 fps, one frame
    body += swf_tag(9, b"\xff\xff\xff")  # SetBackgroundColor
    for index, jpeg in enumerate(jpegs):
        data = (b"\xff\xd9\xff\xd8" if erroneous_header else b"") + jpeg
        if jpeg3:
            body += swf_tag(35, struct.pack("<HI", index + 1, len(data)) + data + zlib.compress(b"\xff" * 64))
        else:
            body += swf_tag(21, struct.pack("<H", index + 1) + data)
        body += swf_tag(26, b"\x06" + struct.pack("<HH", index + 1, index + 1) + b"\x00")  # PlaceObject2
    body += swf_tag(1, b"")  # ShowFrame
    body += swf_tag(0, b"")  # End
    if compressed:
        return b"CWS\x08" + struct.pack("<I", len(body) + 8) + zlib.compress(body)
    return b"FWS\x08" + struct.pack("<I", len(body) + 8) + body


@pytest.fixture
def client(fake_session, fake_response):
    """A `Wings` on a scripted site; `extra` routes take precedence."""

    def build(extra=None):
        defaults = {
            "/webwings/title80.html": fake_response(text=WORK_HTML),
            "/webwings/title20.html": fake_response(text=OLD_WORK_HTML),
            "/webwings/title05.html": fake_response(text=EMPTY_WORK_HTML),
            "/webwings/title99.html": fake_response(status_code=HTTPStatus.NOT_FOUND),
            "/webwings/title75.html": fake_response(status_code=HTTPStatus.NOT_FOUND),
            "/contents/title80-202607-0/book.xml": fake_response(text=book_xml()),
            "/contents/title80-202607-0/": fake_response(text=FLIPPER_HTML),
            "/contents/title80-202608/book.xml": fake_response(
                text=book_xml(title="リヨンでメルシー！ 〜気ままなフランス旅日記〜", data="A,B"),
            ),
            "/contents/title80-202608/": fake_response(text=FLIPPER_HTML),
            "/contents/title75c/book.xml": fake_response(
                text=book_xml(
                    title="十次と亞一",
                    version="3.0",
                    data="1,2",
                    width=506,
                    height=720,
                    slice_width=506,
                    slice_height=480,
                    cdata=False,
                ),
            ),
            "/contents/title75c/": fake_response(text=FLIPPER_HTML),
            "/contents/punichan/xml/data.xml": fake_response(text=DATA_XML),
            "/contents/punichan/data/book.conf": fake_response(text=BOOK_CONF),
            "/contents/punichan/swf/0.swf": fake_response(make_swf([jpeg_bytes(COLOURS[0])]), content_type="x/swf"),
            "/contents/punichan/swf/1.swf": fake_response(
                make_swf([jpeg_bytes(COLOURS[1]), jpeg_bytes(COLOURS[2])], compressed=True),
                content_type="application/x-shockwave-flash",
            ),
            "/contents/punichan/": fake_response(content=SMOOZY_HTML.encode(), content_type="text/html"),
            "/contents/gone/": fake_response(status_code=HTTPStatus.NOT_FOUND),
            "/contents/noviewer/": fake_response(text="<html><body><p>準備中</p></body></html>"),
        }
        # Substring routes, first match wins: the test's own come first and replace a default of the same key.
        routes = dict(extra or {})
        for needle, response in defaults.items():
            routes.setdefault(needle, response)
        session = fake_session(routes)
        return Wings(session), session

    return build


# --- URLs -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        EPISODE_URL,
        "https://www.shinshokan.com/webwings/contents/punichan/index.html",
        "https://www.shinshokan.com/webwings/contents/title77_1/",
        WORK_URL,
        "https://www.shinshokan.com/webwings/title05.html",
    ],
)
def test_suitable_accepts_viewer_directories_and_work_pages(url):
    assert Wings.suitable(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://www.shinshokan.com/webwings/contents/title80-202607-0/",
        "https://shinshokan.com/webwings/contents/title80-202607-0/",
        "https://www.shinshokan.com/webwings/",
        "https://www.shinshokan.com/webwings/contents/title80-202607-0/book.xml",
        "https://www.shinshokan.com/webwings/contents/",
        "https://www.shinshokan.com/comic/tameshiyomi/61882-6/index.html",
        "https://www.shinshokan.com/hakkaplus/",
        "https://www.shinshokan.com/webwings/img/detail/title80_main.jpg",
    ],
)
def test_suitable_rejects_other_pages(url):
    assert not Wings.suitable(url)


def test_is_series_only_for_work_pages():
    assert Wings.is_series(WORK_URL)
    assert not Wings.is_series(EPISODE_URL)


# --- parsing ----------------------------------------------------------------------------


def test_parse_book_reads_flipper_3():
    book = parse_book(book_xml(title="A &amp; B", version="3.0", data="1,2,3", cdata=False))
    assert book.title == "A & B"
    assert book.page_ids == ("1", "2", "3")
    assert book.sliced


def test_parse_book_rejects_a_book_without_pages():
    with pytest.raises(NotAnEpisodePageError, match="no page"):
        parse_book(book_xml(data=""))


def test_tile_grid_rounds_up():
    book = parse_book(book_xml(version="3.0", width=506, height=720, slice_width=506, slice_height=480))
    assert tile_grid(book, 2) == (2, 3)
    assert tile_grid(book, 1) == (1, 2)
    assert tile_grid(parse_book(book_xml(slice_width=0)), 2) == (1, 1)


@pytest.mark.parametrize(
    ("title", "series", "expected"),
    [
        ("リヨンでメルシー！第0話", "リヨンでメルシー！", "第0話"),
        ("リヨンでメルシー！　第0話", "リヨンでメルシー！", "第0話"),
        ("リヨンでメルシー！", "リヨンでメルシー！", ""),
        ("別の作品　第1話", "リヨンでメルシー！", ""),
        ("なにか", "", ""),
    ],
)
def test_split_title(title, series, expected):
    assert split_title(title, series) == expected


@pytest.mark.parametrize(
    ("caption", "expected"),
    [
        ("第0話を読む", "第0話"),
        ("第2話はこちらから", "第2話"),
        ("一～三話を読む", "一～三話"),
        ("23.5話はこちらから", "23.5話"),
        ("最新話はこちらから", ""),
        ("", ""),
    ],
)
def test_caption_title(caption, expected):
    assert caption_title(caption) == expected


# --- the SWF reader and the tile stitcher ---------------------------------------------


@pytest.mark.parametrize("compressed", [False, True])
@pytest.mark.parametrize("jpeg3", [False, True])
def test_swf_jpegs_reads_every_jpeg_in_order(compressed, jpeg3):
    jpegs = [jpeg_bytes(COLOURS[0]), jpeg_bytes(COLOURS[1], size=(300, 300))]
    out = swf_jpegs(make_swf(jpegs, compressed=compressed, jpeg3=jpeg3))
    assert out == jpegs
    assert [Image.open(BytesIO(jpeg)).size for jpeg in out] == [(8, 8), (300, 300)]


def test_swf_jpegs_keeps_a_clean_jpeg_as_is():
    jpeg = jpeg_bytes(COLOURS[2])
    assert swf_jpegs(make_swf([jpeg], erroneous_header=False)) == [jpeg]


def test_swf_jpegs_rejects_another_file():
    with pytest.raises(GetjmangaError, match="not a SWF"):
        swf_jpegs(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)


def test_stitch_lays_the_tiles_out_row_by_row():
    tiles = [Image.new("RGB", (10, 8), colour) for colour in COLOURS]
    tiles[2] = tiles[2].crop((0, 0, 5, 8))  # the right column is narrower
    tiles[5] = tiles[5].crop((0, 0, 5, 8))
    page = stitch(tiles, 3, (25, 16))
    assert page.size == (25, 16)
    assert page.getpixel((2, 2)) == COLOURS[0]
    assert page.getpixel((12, 2)) == COLOURS[1]
    assert page.getpixel((22, 2)) == COLOURS[2]
    assert page.getpixel((2, 12)) == COLOURS[3]
    assert page.getpixel((22, 12)) == COLOURS[5]
    assert stitch([], 2, (4, 4)).size == (4, 4)


# --- episodes ---------------------------------------------------------------------------


def test_episode_reads_a_flipper_u_book(client):
    wings, session = client()
    episode = wings.episode(EPISODE_URL)

    assert episode.series_title == "リヨンでメルシー！ 〜気ままなフランス旅日記〜"
    assert (episode.writer, episode.publisher) == ("野宮レナ", "新書館")
    assert (episode.published, episode.number) == (date(2026, 7, 24), 1)
    assert episode.episode_title == "第0話"
    assert episode.next_url == NEXT_URL
    assert [page.url for page in episode.pages] == [
        f"{EPISODE_URL}pageMRYYBJED762PW/x2/x2.jpg",
        f"{EPISODE_URL}pageMRYYBK7NWW7KI/x2/x2.jpg",
        f"{EPISODE_URL}pageMRYYBKTH0GN03/x2/x2.jpg",
    ]
    assert (episode.pages[0].width, episode.pages[0].height) == (1268, 1800)
    assert episode.pages[0].extra == {"page_id": "MRYYBJED762PW", "scale": 2, "sliced": False, "columns": 4, "rows": 5}
    assert episode.metadata["viewer"] == "flipper"
    assert episode.metadata["work_url"] == WORK_URL
    assert episode.metadata["book"]["flipperVersion"] == "5.0.10"
    json.dumps(episode.metadata)
    # The viewer page, the work page its slug names, then book.xml.
    assert session.calls == [EPISODE_URL, WORK_URL, f"{EPISODE_URL}book.xml"]
    assert session.headers_seen[-1]["Referer"] == EPISODE_URL


def test_episode_takes_the_index_html_form_and_reads_the_work_once(client):
    wings, session = client()
    first = wings.episode("https://www.shinshokan.com/webwings/contents/title80-202607-0/index.html")
    last = wings.episode(LAST_URL)
    assert first.url == EPISODE_URL
    assert last.episode_title == "第2話"  # the caption, since the book's title adds nothing
    assert (last.prev_url, last.next_url) == (NEXT_URL, None)
    assert session.calls.count(WORK_URL) == 1


def test_episode_at_1x_uses_the_plain_page_file(client, fake_response):
    wings, _ = client({"/contents/title80-202607-0/book.xml": fake_response(text=book_xml(magnification=1))})
    episode = wings.episode(EPISODE_URL)
    assert episode.pages[0].url == f"{EPISODE_URL}pageMRYYBJED762PW/x1.jpg"
    assert (episode.pages[0].width, episode.pages[0].height) == (634, 900)


def test_episode_falls_back_to_the_slug_without_a_work_page(client, fake_response):
    wings, _ = client(
        {
            "/contents/title99-202601/book.xml": fake_response(text=book_xml(title="幻の作品　第1話")),
            "/contents/title99-202601/": fake_response(text=FLIPPER_HTML),
        },
    )
    episode = wings.episode("https://www.shinshokan.com/webwings/contents/title99-202601/")
    assert episode.series_title == "幻の作品　第1話"
    assert episode.episode_title == "title99-202601"
    assert episode.next_url is None
    assert episode.metadata["work_url"] is None


def test_episode_is_locked_behind_a_password(client, fake_response):
    wings, _ = client({"/contents/title80-202607-0/book.xml": fake_response(text=book_xml(lock="abc123"))})
    episode = wings.episode(EPISODE_URL)
    assert episode.pages == ()
    assert not episode.readable
    assert episode.next_url == NEXT_URL
    assert episode.metadata["locked"] is True


def test_episode_reads_a_smoozy_book_right_to_left(client):
    wings, session = client()
    wings.series_urls(OLD_WORK_URL)  # so the work page is known to list it
    episode = wings.episode("https://www.shinshokan.com/webwings/contents/punichan/index.html")

    assert episode.url == SMOOZY_URL
    assert episode.series_title == "プニちゃん"
    assert episode.episode_title == "第一話"
    # Spread 0 holds one page; spread 1 holds two, left page first, read right page first.
    assert [(page.url, page.extra["swf_index"]) for page in episode.pages] == [
        (f"{SMOOZY_URL}swf/0.swf", 0),
        (f"{SMOOZY_URL}swf/1.swf", 1),
        (f"{SMOOZY_URL}swf/1.swf", 0),
    ]
    assert (episode.pages[0].width, episode.pages[0].height) == (458, 650)
    assert episode.metadata["viewer"] == "smoozy"
    assert episode.metadata["conf"]["DIRECTION"] == "right"
    json.dumps(episode.metadata)
    # The SWFs were read while listing; the images come out of that read.
    calls_before = len(session.calls)
    image = wings.image(episode.pages[1], episode)
    assert image.convert("RGB").getpixel((3, 3)) == pytest.approx(COLOURS[2], abs=8)
    assert len(session.calls) == calls_before


def test_episode_reads_a_smoozy_book_without_a_work_page(client):
    wings, _ = client()
    episode = wings.episode(SMOOZY_URL)
    assert episode.series_title == "プニちゃん"  # the page title, author dropped
    assert episode.episode_title == "punichan"
    assert episode.next_url is None


def test_episode_reads_a_left_to_right_smoozy_book_in_file_order(client, fake_response):
    wings, _ = client(
        {
            "/contents/punichan/data/book.conf": fake_response(
                text=BOOK_CONF.replace("DIRECTION=right", "DIRECTION=left")
            )
        },
    )
    episode = wings.episode(SMOOZY_URL)
    assert [page.extra["swf_index"] for page in episode.pages] == [0, 0, 1]


def test_episode_rejects_a_gone_directory(client):
    wings, _ = client()
    with pytest.raises(NotAnEpisodePageError, match="404"):
        wings.episode("https://www.shinshokan.com/webwings/contents/gone/")


def test_episode_rejects_a_page_without_a_viewer(client):
    wings, _ = client()
    with pytest.raises(NotAnEpisodePageError, match="no viewer"):
        wings.episode("https://www.shinshokan.com/webwings/contents/noviewer/")


def test_episode_rejects_a_work_page(client):
    wings, _ = client()
    with pytest.raises(UnsupportedUrlError):
        wings.episode(WORK_URL)


# --- series -----------------------------------------------------------------------------


def test_series_urls_lists_the_episodes_in_page_order(client):
    wings, session = client()
    urls = wings.series_urls(WORK_URL)
    assert urls == [EPISODE_URL, NEXT_URL, LAST_URL]
    assert all(Wings.suitable(url) for url in urls)
    assert wings.series_urls(WORK_URL) == urls
    assert session.calls == [WORK_URL]


def test_series_urls_rejects_an_empty_or_gone_work(client):
    wings, _ = client()
    with pytest.raises(NotAnEpisodePageError, match="lists no episode"):
        wings.series_urls("https://www.shinshokan.com/webwings/title05.html")
    with pytest.raises(NotAnEpisodePageError, match="404"):
        wings.series_urls("https://www.shinshokan.com/webwings/title99.html")
    with pytest.raises(UnsupportedUrlError):
        wings.series_urls(EPISODE_URL)


# --- downloading ------------------------------------------------------------------------


def test_download_writes_a_flipper_u_page(client, fake_response, tmp_path):
    wings, session = client(
        {"/x2/x2.jpg": fake_response(jpeg_bytes(COLOURS[0], size=(1268, 1800)), content_type="image/jpeg")},
    )
    result = Downloader(wings, tmp_path, save_metadata=True).download(EPISODE_URL)

    assert result.status == "saved"
    assert (
        result.save_dir == tmp_path / "www.shinshokan.com" / "リヨンでメルシー！ 〜気ままなフランス旅日記〜" / "第0話"
    )
    assert sorted(path.name for path in result.save_dir.iterdir()) == ["0.jpg", "1.jpg", "2.jpg", "metadata.json"]
    page = Image.open(result.save_dir / "0.jpg")
    assert page.size == (1268, 1800)
    assert page.convert("RGB").getpixel((5, 5)) == pytest.approx(COLOURS[0], abs=8)
    json.loads((result.save_dir / "metadata.json").read_text(encoding="utf-8"))
    assert session.headers_seen[-1]["Referer"] == EPISODE_URL


def test_download_stitches_flipper_3_tiles(client, fake_response, tmp_path):
    # 1012x1440 at 2x, cut into 506x480 tiles: two columns, three rows.
    routes = {
        f"/contents/title75c/page{page_id}/x2/{number}.jpg": fake_response(
            jpeg_bytes(COLOURS[number - 1], size=(506, 480)),
            content_type="image/jpeg",
        )
        for page_id in ("1", "2")
        for number in range(1, 7)
    }
    wings, session = client(routes)
    result = Downloader(wings, tmp_path, only_first=True).download(SLICED_URL)

    assert result.status == "saved"
    assert result.episode.series_title == "十次と亞一"
    assert result.episode.episode_title == "title75c"  # no work page knows this slug
    page = Image.open(result.save_dir / "0.jpg").convert("RGB")
    assert page.size == (1012, 1440)
    assert page.getpixel((10, 10)) == pytest.approx(COLOURS[0], abs=8)
    assert page.getpixel((600, 10)) == pytest.approx(COLOURS[1], abs=8)
    assert page.getpixel((10, 500)) == pytest.approx(COLOURS[2], abs=8)
    assert page.getpixel((600, 1400)) == pytest.approx(COLOURS[5], abs=8)
    assert f"{SLICED_URL}page1/x2/x2.jpg" not in session.calls


def test_download_falls_back_to_tiles_when_the_whole_page_is_missing(client, fake_response, tmp_path):
    routes = {
        "/x2/x2.jpg": fake_response(status_code=HTTPStatus.NOT_FOUND),
        **{
            f"/contents/title80-202607-0/pageMRYYBJED762PW/x2/{number}.jpg": fake_response(
                jpeg_bytes(COLOURS[number % 6], size=(400, 400)),
                content_type="image/jpeg",
            )
            for number in range(1, 21)
        },
    }
    wings, _ = client(routes)
    result = Downloader(wings, tmp_path, only_first=True).download(EPISODE_URL)
    assert result.status == "saved"
    page = Image.open(result.save_dir / "0.jpg").convert("RGB")
    assert page.size == (1268, 1800)
    assert page.getpixel((410, 10)) == pytest.approx(COLOURS[2], abs=8)  # tile 2, top row


# --- logging in -------------------------------------------------------------------------


# --- the real site ----------------------------------------------------------------------

# One episode per known host, free to read without an account: the pilot
# episode of a travel essay comic, on the FLIPPER U viewer.
TEST_URLS: dict[str, str] = {
    "www.shinshokan.com": EPISODE_URL,
}


@pytest.mark.network
@pytest.mark.parametrize("host", TEST_URLS)
def test_site_download(tmp_path, host):
    result = Downloader(Wings(), tmp_path, only_first=True).download(TEST_URLS[host])
    assert result.status == "saved"
    assert result.episode.series_title == "リヨンでメルシー！ 〜気ままなフランス旅日記〜"
    assert result.episode.episode_title == "第0話"
    assert result.episode.next_url == NEXT_URL
    assert Image.open(result.save_dir / "0.jpg").size == (1268, 1800)


@pytest.mark.network
def test_site_work_page_lists_episodes():
    urls = Wings().series_urls(WORK_URL)
    assert urls[0] == EPISODE_URL
    assert all(Wings.suitable(url) for url in urls)


@pytest.mark.network
def test_site_smoozy_book_is_readable(tmp_path):
    result = Downloader(Wings(), tmp_path, only_first=True).download(SMOOZY_URL)
    assert result.status == "saved"
    assert result.episode.series_title == "プニちゃん"
    assert len(result.episode.pages) == 10
    assert Image.open(result.save_dir / "0.jpg").size == (458, 650)


@pytest.mark.network
def test_site_flipper_3_tiles_are_stitched(tmp_path):
    result = Downloader(Wings(), tmp_path, only_first=True).download(SLICED_URL)
    assert result.status == "saved"
    assert result.episode.pages[0].extra["sliced"] is True
    assert Image.open(result.save_dir / "0.jpg").size == (1012, 1440)
