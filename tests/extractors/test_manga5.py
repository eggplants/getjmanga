from __future__ import annotations

import json
from io import BytesIO

import pytest
from PIL import Image

from getjmanga.downloader import Downloader
from getjmanga.extractors import manga5
from getjmanga.extractors.boost import Pack
from getjmanga.extractors.common import NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractors.manga5 import (
    BASE_URL,
    LICENSE_URL,
    PATTERN_COUNT,
    Manga5,
    descramble,
    parse_viewer,
    shuffle_pattern,
    split_title,
    tile_slices,
)

SERIES_URL = f"{BASE_URL}/content/00850001"
EPISODE_URL = f"{BASE_URL}/product/00850001"
BOOK_URL = f"{BASE_URL}/product/00760002"
LOCKED_URL = f"{BASE_URL}/product/00760001"
ADS_URL = f"{BASE_URL}/ads_before_launching_viewer.html?id=00850001"
CID = "Dc5ioOPSWyYLV3c13WscfOF+riC+9no+FqlXXCuIa35+bT5lxYwe8jTZm56A1MMc/kwKLNTwc4k6z3wlxbt4A=="
VIEWER_URL = (
    f"{BASE_URL}/viewer.html?series=00850001&item=00850001"
    "&cid=Dc5ioOPSWyYLV3c13WscfOF%252BriC%252B9no%252BFqlXXCuIa35%252BbT5lxYwe8jTZm56A1MMc%252FkwKLNTwc4k6z3wlxbt4A"
    "%253D%253D&&com-access-no-history"
)
KOMA_BASE = "https://cdn.manga-5.com/contents/publus/LFS-00026-001/unscrambled_manga_brws/"
BOOK_BASE = "https://cdn.manga-5.com/contents/publus/TKB-00002-001_02/epub_brws_fixedlayout/"
AUTH_INFO = {"Policy": "eyJTdGF0ZW1lbnQi", "Signature": "CgMrXEK9~Jdor__", "Key-Pair-Id": "K20CQXK8BTM3VW"}
AUTH = "Policy=eyJTdGF0ZW1lbnQi&Signature=CgMrXEK9~Jdor__&Key-Pair-Id=K20CQXK8BTM3VW"
SERIES_TITLE = "あんバタ絵巻"
BOOK_TITLE = "レイトンブラザーズ・ミステリールーム 完全犯罪のパズル【単行本】"

ADS_HTML = f"""<html><body>
<div class="ad-launch-inner">
  <p class="l-mb24">
    <button type="button" onclick="window.location.replace('{VIEWER_URL}')" class="btn primary">作品を読む</button>
  </p>
  <p class="l-mb24"><a href="/content/00850001/1" class="btn">作品ページに戻る</a></p>
</div>
</body></html>"""

VIEWER_DATA = {
    "comment": {"to": "/comment/00850001", "target": "_top"},
    "twitter": {
        "image": "/manga5/img/common/icon-twitter.png",
        "to": "https://x.com/intent/tweet?text=%E3%81%82%E3%82%93%E3%83%90%E3%82%BF%E7%B5%B5%E5%B7%BB%20%E4%BD%9C%E5"
        "%93%81No.1%E3%82%92%E8%AA%AD%E3%81%BF%E3%81%BE%E3%81%97%E3%81%9F%EF%BC%81%20https%3A%2F%2Fmanga-5.com%2F"
        "content%2F00850001%20%23%E3%83%9E%E3%83%B3%E3%82%AC5",
        "target": "_blank",
    },
    "next": {"to": "/accounts/content?content_id=00850002&transition=viewer", "remote": True},
    "prev": None,
    "pta": "vertical",
    "title": "作品No.1 - あんバタ絵巻",
}


def viewer_html(data):
    return f"""<html><head>
<title>{data.get("title", "")} - マンガ5(マンガファイブ) presented by レベルファイブ</title>
</head><body>
<script type="text/javascript">
    window.__data = {json.dumps(data, ensure_ascii=False)}
</script>
<script src="/manga5/js/viewer-common-2021-05-17.js"></script>
</body></html>"""


VIEWER_HTML = viewer_html(VIEWER_DATA)
BOOK_VIEWER_HTML = viewer_html(
    {
        **VIEWER_DATA,
        "next": None,
        "title": f"【試し読み】CHAPTER 1 - {BOOK_TITLE}",
        "twitter": {"to": "https://x.com/intent/tweet?text=x"},
    }
)
LOCKED_HTML = """<html><head><title>マンガ5</title>
<script>
        alert('閲覧するにはログインが必要です。');
    history.back();
</script>
</head></html>"""
MISSING_HTML = """<html><head><script>
        alert('作品情報が見つかりませんでした。');
    history.back();
</script></head></html>"""


def listing_html(title, items, *, has_next):
    rows = "\n".join(
        f'<a class="book-product-list-item" href="/product/{item_id}" data-id="{item_id}" '
        f'data-title="{item_title}" data-sub="閲覧期限：無期限" data-show-coin="false" data-coin="0"></a>'
        for item_id, item_title in items
    )
    disabled = "" if has_next else " disabled"
    return f"""<html><body>
<h1 class="comic-title">{title}</h1>
<div class="book-product-list">{rows}</div>
<ul class="pagination-list right">
  <li class="pagination-list-item to-next{disabled}"><a href="javascript:void(0);" title="次のページへ"></a></li>
</ul>
</body></html>"""


BOOK_LISTING_1 = listing_html(
    BOOK_TITLE, [("00760001", "CHAPTER 1"), ("00760002", "【試し読み】CHAPTER 1")], has_next=True
)
BOOK_LISTING_2 = listing_html(BOOK_TITLE, [("00760003", "CHAPTER 2")], has_next=False)
KOMA_LISTING = listing_html(SERIES_TITLE, [("00850001", "作品No.1"), ("00850002", "作品No.2")], has_next=False)

KOMA_LICENSE = {"status": "200", "url": KOMA_BASE, "cty": "6", "auth_info": AUTH_INFO}
BOOK_LICENSE = {"status": "200", "url": BOOK_BASE[:-1], "cty": 1, "auth_info": AUTH_INFO}
REFUSED_LICENSE = {"status": 401, "message": "アクセスIDが一致していません"}


def step(*images):
    return {"effectType": "show", "effectTargetImgs": list(images), "originalPicWidth": 760, "originalPicHeight": 1000}


CONTENT_JSON = [[step("picture/26(01)_001.jpg")], [], [step("picture/26(01)_002.jpg", "picture/26(01)_003.jpg")], []]


def book_page(no=0, width=810, height=1152, dummy=6):
    return {
        "Page": {
            "ContentArea": {"Height": height, "Width": width, "X": 0, "Y": 0},
            "LinkList": [],
            "No": no,
            "Rect": {"Height": height, "Width": width, "X": 0, "Y": 0},
            "Shrink": 1.0,
            "Size": {"Height": height, "Width": width},
            "DummyWidth": dummy,
            "DummyHeight": 0,
        }
    }


PLAIN_PACK = {
    "configuration": {
        "contents": [
            {"file": "item/xhtml/p-cover.xhtml", "index": 1, "type": "bmp"},
            {"file": "item/xhtml/p-001.xhtml", "index": 2, "type": "bmp"},
            {"file": "item/xhtml/p-nav.xhtml", "index": 3, "type": "bmp"},
        ],
        "json-format-version": "1.2.5",
        "page-progression-direction": "rtl",
    },
    "item/xhtml/p-cover.xhtml": {"FileLinkInfo": {"PageCount": 1, "PageLinkInfoList": [book_page()]}, "Linear": 1},
    "item/xhtml/p-001.xhtml": {"FileLinkInfo": {"PageCount": 1, "PageLinkInfoList": [book_page()]}, "Linear": 1},
    "item/xhtml/p-nav.xhtml": {"FileLinkInfo": {"PageCount": 1, "PageLinkInfoList": [book_page()]}, "Linear": 0},
}

# A wrapped pack as `decode_pack()` hands it over, cut down to what is read.
KEYED_KEYS = (bytes(range(32)), bytes(range(32, 64)), bytes(range(64, 96)))
KEYED_PACK = Pack(
    {
        "configuration": {"file-name-version": "1.0", "contents": [{"file": "item/xhtml/p-0001.xhtml", "index": 1}]},
        "item/xhtml/p-0001.xhtml": {
            "FileLinkInfo": {
                "PageLinkInfoList": [
                    {
                        "Page": {
                            "No": 0,
                            "Size": {"Width": 64, "Height": 48},
                            "BlockWidth": 32,
                            "BlockHeight": 32,
                            "NS": 2044018965,
                            "PS": 3200281383,
                            "RS": 1889258295,
                        }
                    }
                ]
            },
            "Linear": 1,
        },
    },
    KEYED_KEYS,
)


def jpeg_bytes(image):
    raw = BytesIO()
    image.save(raw, "JPEG", quality=100)
    return raw.getvalue()


def striped(size, block=8):
    """A page whose tiles are all different, so a misplaced one shows."""
    image = Image.new("RGB", size)
    image.putdata(
        [
            ((x // block * 37) % 256, (y // block * 59) % 256, (x + y) % 256)
            for y in range(size[1])
            for x in range(size[0])
        ]
    )
    return image


def scramble(image, pattern):
    """What the CDN serves for a plain-pack page: the inverse of `descramble()`."""
    out = Image.new(image.mode, image.size)
    for piece in tile_slices(image.width, image.height, pattern):
        tile = image.crop((piece.dst_x, piece.dst_y, piece.dst_x + piece.width, piece.dst_y + piece.height))
        out.paste(tile, (piece.src_x, piece.src_y))
    return out


# A scrambled book page: 4x4 tiles of 64 plus a 44 wide edge strip and a 14 high one.
BOOK_SIZE = (300, 270)
BOOK_CLEAN = striped(BOOK_SIZE)
BOOK_PATTERN = shuffle_pattern("item/xhtml/p-cover.xhtml", "0")


@pytest.fixture
def client(fake_session, fake_response):
    """A `Manga5` over a session scripting a koma episode, a sample volume, a locked and a missing one."""

    def build(extra=None):
        routes = {
            "/product/00850001": fake_response(text=ADS_HTML, url=ADS_URL),
            "/product/00760002": fake_response(text=ADS_HTML, url=ADS_URL.replace("00850001", "00760002")),
            "/product/00870010": fake_response(text=ADS_HTML, url=ADS_URL.replace("00850001", "00870010")),
            "/product/00760001": fake_response(text=LOCKED_HTML),
            "/product/": fake_response(text=MISSING_HTML),
            "/viewer.html": fake_response(text=VIEWER_HTML),
            LICENSE_URL: fake_response(payload=KOMA_LICENSE),
            "content.json": fake_response(payload=CONTENT_JSON),
            "configuration_pack.json": fake_response(payload=PLAIN_PACK, text=json.dumps(PLAIN_PACK)),
            "/content/00760001": [fake_response(text=BOOK_LISTING_1), fake_response(text=BOOK_LISTING_2)],
            "/content/00850001": fake_response(text=KOMA_LISTING),
            "p-cover.xhtml/0.jpeg": fake_response(
                jpeg_bytes(scramble(BOOK_CLEAN, BOOK_PATTERN)), content_type="image/jpeg"
            ),
            "26(01)_001.jpg": fake_response(jpeg_bytes(Image.new("RGB", (8, 8), (1, 2, 3))), content_type="image/jpeg"),
        }
        # An override keeps the route's place in the match order; a new route goes first.
        routes = {**(extra or {}), **{key: value for key, value in routes.items() if key not in (extra or {})}}
        session = fake_session(routes)
        return Manga5(session), session

    return build


@pytest.fixture
def book_client(client, fake_response):
    """`client`, licensed for the sample volume: a plain configuration pack."""
    return client(
        {"/viewer.html": fake_response(text=BOOK_VIEWER_HTML), LICENSE_URL: fake_response(payload=BOOK_LICENSE)}
    )


# --- URLs ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        EPISODE_URL,
        f"{EPISODE_URL}/",
        f"{LOCKED_URL}?coin=740",
        "https://www.manga-5.com/product/00070001",
        SERIES_URL,
        f"{SERIES_URL}/",
        f"{SERIES_URL}/1",
        f"{SERIES_URL}?order=asc&p=2",
        "https://www.manga-5.com/content/00070001",
    ],
)
def test_suitable_accepts_episode_and_work_urls(url):
    assert Manga5.suitable(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://manga-5.com/product/00850001",
        "https://comic-boost.com/product/01700001",
        f"{BASE_URL}/product/0085001",
        f"{BASE_URL}/viewer.html?series=00850001&item=00850001&cid=x",
        f"{BASE_URL}/series",
        f"{BASE_URL}/",
    ],
)
def test_suitable_rejects_other_urls(url):
    assert not Manga5.suitable(url)


@pytest.mark.parametrize(
    ("url", "expected"), [(SERIES_URL, True), (f"{SERIES_URL}/3", True), (EPISODE_URL, False), (f"{BASE_URL}/", False)]
)
def test_is_series(url, expected):
    assert Manga5.is_series(url) is expected


# --- the unkeyed shuffle -------------------------------------------------------------


def test_shuffle_pattern_comes_from_the_page_path():
    assert shuffle_pattern("item/xhtml/p-003.xhtml", "0") == 1
    assert shuffle_pattern("item/xhtml/p-001.xhtml", "0") == 3
    assert all(1 <= shuffle_pattern(f"item/p-{i:03d}.xhtml", "0") <= PATTERN_COUNT for i in range(50))


@pytest.mark.parametrize("pattern", range(1, PATTERN_COUNT + 1))
@pytest.mark.parametrize("size", [(300, 270), (256, 256), (270, 300)])
def test_tile_slices_partition_the_page(size, pattern):
    for side in ("src", "dst"):
        covered = [0] * (size[0] * size[1])
        for piece in tile_slices(size[0], size[1], pattern):
            x0, y0 = getattr(piece, f"{side}_x"), getattr(piece, f"{side}_y")
            for y in range(y0, y0 + piece.height):
                for x in range(x0, x0 + piece.width):
                    covered[y * size[0] + x] += 1
        assert set(covered) == {1}, (side, size, pattern)


def test_tile_slices_of_a_page_narrower_than_two_tiles_are_empty():
    # The viewer's arithmetic falls apart (`NaN`) below two tiles a side; such a page is left as it is.
    assert tile_slices(60, 100, 1) == []
    assert tile_slices(100, 127, 1) == []
    assert descramble(Image.new("RGB", (60, 100)), 1).size == (60, 100)


def test_descramble_restores_a_synthetic_page():
    for pattern in range(1, PATTERN_COUNT + 1):
        shuffled = scramble(BOOK_CLEAN, pattern)
        assert shuffled.tobytes() != BOOK_CLEAN.tobytes()
        assert descramble(shuffled, pattern).tobytes() == BOOK_CLEAN.tobytes()


def test_descramble_cuts_the_dummy_strips_off():
    restored = descramble(scramble(BOOK_CLEAN, BOOK_PATTERN), BOOK_PATTERN, (294, 260))
    assert restored.size == (294, 260)
    assert restored.tobytes() == BOOK_CLEAN.crop((0, 0, 294, 260)).tobytes()
    # A content area the image cannot hold is ignored rather than padded.
    assert descramble(scramble(BOOK_CLEAN, BOOK_PATTERN), BOOK_PATTERN, (400, 260)).size == BOOK_SIZE


def test_tile_slices_of_a_real_layout():
    # 816x1152, as the sample volumes come: twelve 64 wide columns and a 48 wide strip, eighteen rows
    # and no strip below, so no corner piece either. The strip is put back at column 5 for pattern 1.
    pieces = tile_slices(816, 1152, 1)
    strip = [piece for piece in pieces if piece.width == 48]
    assert len(strip) == 18
    assert {piece.dst_x for piece in strip} == {5 * 64}
    assert all(piece.height == 64 for piece in pieces)
    assert sum(piece.width * piece.height for piece in pieces) == 816 * 1152


# --- the viewer page and the listing ---------------------------------------------------


@pytest.mark.parametrize(
    ("title", "hint", "expected"),
    [
        ("作品No.1 - あんバタ絵巻", "あんバタ絵巻 作品No.1を読みました！ #マンガ5", ("あんバタ絵巻", "作品No.1")),
        ("第1話 - ほのスト！ - 番外編", "ほのスト！ - 番外編 第1話を読みました！", ("ほのスト！ - 番外編", "第1話")),
        ("第1話 - 前編 - ほのスト！", "ほのスト！ 第1話 - 前編を読みました！", ("ほのスト！", "第1話 - 前編")),
        ("第1話 - 前編 - ほのスト！", "", ("前編 - ほのスト！", "第1話")),
        ("作品No.1", "", ("作品No.1", "作品No.1")),
    ],
)  # fmt: skip
def test_split_title(title, hint, expected):
    assert split_title(title, hint) == expected


def test_parse_viewer_falls_back_to_the_title_tag():
    html = "<html><head><title>第2話 - 何か - マンガ5(マンガファイブ) presented by レベルファイブ</title></head></html>"
    data = parse_viewer(html)
    assert data is not None
    assert (data.series_title, data.episode_title, data.next_url) == ("何か", "第2話", None)
    assert parse_viewer("<html><body><script>window.__data = {oops</script></body></html>") is None


# --- pages ---------------------------------------------------------------------------


# --- episodes ------------------------------------------------------------------------


def test_episode_reads_a_koma_episode(client):
    extractor, session = client()
    episode = extractor.episode(EPISODE_URL)

    assert episode.url == EPISODE_URL
    assert episode.series_title == SERIES_TITLE
    assert episode.episode_title == "作品No.1"
    assert episode.next_url == f"{BASE_URL}/product/00850002"
    assert [page.url for page in episode.pages] == [
        f"{KOMA_BASE}picture/26(01)_001.jpg?{AUTH}",
        f"{KOMA_BASE}picture/26(01)_002.jpg?{AUTH}",
        f"{KOMA_BASE}picture/26(01)_003.jpg?{AUTH}",
    ]
    assert episode.metadata["license"] == KOMA_LICENSE
    assert episode.metadata["content"] == [CONTENT_JSON[0], CONTENT_JSON[2]]
    json.dumps(episode.metadata)

    # The product page led to the interstitial, then to the viewer, then to the license with the decoded cid.
    assert session.calls[:3] == [EPISODE_URL, VIEWER_URL, LICENSE_URL]
    assert session.headers_seen[1]["Referer"] == ADS_URL
    assert session.params_seen[2] == {"cid": CID}
    assert session.calls[3] == f"{KOMA_BASE}content.json"
    assert session.params_seen[3] == AUTH_INFO


def test_episode_normalises_the_host_the_query_and_the_trailing_slash(client):
    extractor, _ = client()
    assert extractor.episode("https://www.manga-5.com/product/00850001/?coin=0").url == EPISODE_URL


def test_episode_reads_a_plain_pack(book_client):
    extractor, session = book_client
    episode = extractor.episode(BOOK_URL)

    assert episode.series_title == BOOK_TITLE
    assert episode.episode_title == "【試し読み】CHAPTER 1"
    assert episode.next_url is None
    assert [page.url for page in episode.pages] == [
        f"{BOOK_BASE}item/xhtml/p-cover.xhtml/0.jpeg?{AUTH}",
        f"{BOOK_BASE}item/xhtml/p-001.xhtml/0.jpeg?{AUTH}",
    ]
    assert episode.pages[0].extra == {"pattern": BOOK_PATTERN, "size": [810, 1152]}
    assert episode.metadata["configuration"] == PLAIN_PACK["configuration"]
    # The license's directory lacked its trailing slash; the pack was still asked for under it.
    assert session.calls[3] == f"{BOOK_BASE}configuration_pack.json"


def test_episode_hands_a_wrapped_pack_to_the_boost_port(book_client, fake_response, monkeypatch):
    extractor, session = book_client
    wrapped = {"version": "1.0", "data": "bm90IHJlYWxseQ=="}
    session.routes["configuration_pack.json"] = fake_response(payload=wrapped, text=json.dumps(wrapped))
    monkeypatch.setattr(manga5, "decode_pack", lambda text: KEYED_PACK if json.loads(text) == wrapped else None)

    episode = extractor.episode(BOOK_URL)
    assert len(episode.pages) == 1
    (page,) = episode.pages
    assert page.url.startswith(f"{BOOK_BASE}item/xhtml/p-0001.xhtml/")
    assert page.url.endswith(f".jpeg?{AUTH}")
    assert "0.jpeg" not in page.url  # hashed, as `file-name-version` asks
    assert set(page.extra) == {"pattern", "seeds", "block"}
    assert (page.width, page.height) == (64, 48)
    assert episode.metadata["configuration"] == KEYED_PACK.content["configuration"]
    json.dumps(episode.metadata)


def test_locked_episode_is_named_off_the_listing(client):
    extractor, session = client()
    episode = extractor.episode(f"{LOCKED_URL}?coin=740")

    assert not episode.readable
    assert episode.url == LOCKED_URL
    assert episode.series_title == BOOK_TITLE
    assert episode.episode_title == "CHAPTER 1"
    assert episode.next_url == BOOK_URL
    assert episode.metadata == {"listing": {"content_id": "00760001", "found": True}}
    # Only the listing page holding the episode and its successor was needed.
    assert session.calls == [LOCKED_URL, f"{BASE_URL}/content/00760001"]
    assert session.params_seen[1] == {"order": "asc", "p": 1}


def test_locked_episode_walks_on_to_the_next_listing_page(client, fake_response):
    extractor, session = client(
        {"/product/00760002": fake_response(text=LOCKED_HTML)},
    )
    episode = extractor.episode(BOOK_URL)

    assert not episode.readable
    assert episode.episode_title == "【試し読み】CHAPTER 1"
    assert episode.next_url == f"{BASE_URL}/product/00760003"
    assert session.params_seen[1:] == [{"order": "asc", "p": 1}, {"order": "asc", "p": 2}]


def test_locked_episode_missing_from_the_listing_keeps_its_id(client, fake_response):
    extractor, _ = client({"/product/00760009": fake_response(text=LOCKED_HTML)})
    episode = extractor.episode(f"{BASE_URL}/product/00760009")

    assert not episode.readable
    assert episode.series_title == BOOK_TITLE
    assert episode.episode_title == "00760009"
    assert episode.next_url is None
    assert episode.metadata["listing"]["found"] is False


def test_unknown_episode_is_not_an_episode_page(client):
    extractor, _ = client()
    with pytest.raises(NotAnEpisodePageError, match="99990001"):
        extractor.episode(f"{BASE_URL}/product/99990001")


def test_refused_license_means_locked_but_keeps_the_viewer_titles(client, fake_response):
    extractor, _ = client({LICENSE_URL: fake_response(payload=REFUSED_LICENSE)})
    episode = extractor.episode(EPISODE_URL)

    assert not episode.readable
    assert episode.series_title == SERIES_TITLE
    assert episode.episode_title == "作品No.1"
    assert episode.next_url == f"{BASE_URL}/product/00850002"
    assert episode.metadata == {"viewer": VIEWER_DATA, "license": REFUSED_LICENSE}


def test_viewer_url_without_a_cid_means_locked(client, fake_response):
    ads = ADS_HTML.replace(VIEWER_URL, f"{BASE_URL}/viewer.html?series=00850001&item=00850001")
    extractor, session = client({"/product/00850001": fake_response(text=ads, url=ADS_URL)})
    episode = extractor.episode(EPISODE_URL)

    assert not episode.readable
    assert episode.episode_title == "作品No.1"
    assert LICENSE_URL not in session.calls


def test_interstitial_without_a_read_button_means_locked(client, fake_response):
    extractor, _ = client({"/product/00850001": fake_response(text="<html><body>ads</body></html>", url=ADS_URL)})
    episode = extractor.episode(EPISODE_URL)
    assert not episode.readable
    assert episode.series_title == SERIES_TITLE


def test_product_page_redirecting_straight_to_the_viewer_is_taken(client, fake_response):
    extractor, session = client({"/product/00850001": fake_response(text=VIEWER_HTML, url=VIEWER_URL)})
    episode = extractor.episode(EPISODE_URL)
    assert episode.readable
    assert session.calls[:2] == [EPISODE_URL, LICENSE_URL]


def test_viewer_page_without_data_is_not_an_episode_page(client, fake_response):
    extractor, _ = client({"/viewer.html": fake_response(text="<html><body></body></html>")})
    with pytest.raises(NotAnEpisodePageError, match="viewer"):
        extractor.episode(EPISODE_URL)


def test_episode_rejects_a_work_url(client):
    extractor, _ = client()
    with pytest.raises(UnsupportedUrlError):
        extractor.episode(SERIES_URL)


# --- series --------------------------------------------------------------------------


def test_series_urls_walks_the_pages_oldest_first(client):
    extractor, session = client()
    urls = extractor.series_urls(f"{BASE_URL}/content/00760001/2")

    assert urls == [LOCKED_URL, BOOK_URL, f"{BASE_URL}/product/00760003"]
    assert session.calls == [f"{BASE_URL}/content/00760001"] * 2
    assert session.params_seen == [{"order": "asc", "p": 1}, {"order": "asc", "p": 2}]


def test_series_urls_stops_when_a_page_brings_nothing_new(fake_session, fake_response):
    repeating = listing_html(BOOK_TITLE, [("00760001", "CHAPTER 1")], has_next=True)
    session = fake_session({"/content/00760001": fake_response(text=repeating)})
    assert Manga5(session).series_urls(f"{BASE_URL}/content/00760001") == [LOCKED_URL]
    assert len(session.calls) == 2


def test_series_urls_rejects_an_episode_url(client):
    extractor, _ = client()
    with pytest.raises(UnsupportedUrlError):
        extractor.series_urls(EPISODE_URL)


def test_series_urls_raises_on_an_empty_listing(fake_session, fake_response):
    session = fake_session({"/content/00760001": fake_response(text=listing_html(BOOK_TITLE, [], has_next=False))})
    with pytest.raises(NotAnEpisodePageError, match="no episode"):
        Manga5(session).series_urls(f"{BASE_URL}/content/00760001")


# --- images and downloads ------------------------------------------------------------


def test_image_leaves_a_koma_page_alone(client):
    extractor, session = client()
    episode = extractor.episode(EPISODE_URL)

    image = extractor.image(episode.pages[0], episode)
    assert image.size == (8, 8)
    assert all(abs(a - b) < 8 for a, b in zip(image.convert("RGB").getpixel((0, 0)), (1, 2, 3), strict=True))
    assert session.headers_seen[-1]["Referer"] == EPISODE_URL


def test_image_unshuffles_a_plain_pack_page_and_crops_it(book_client):
    extractor, _ = book_client
    episode = extractor.episode(BOOK_URL)
    page = episode.pages[0]
    assert page.extra["pattern"] == BOOK_PATTERN

    # The fixture's page says 810x1152, wider than the 300x270 fake: the crop is skipped, the shuffle undone.
    image = extractor.image(page, episode)
    assert image.size == BOOK_SIZE
    clean = BOOK_CLEAN.convert("RGB")
    for xy in ((3, 3), (150, 135), (297, 3), (3, 267), (297, 267)):
        assert all(
            abs(a - b) < 12 for a, b in zip(image.convert("RGB").getpixel(xy), clean.getpixel(xy), strict=True)
        ), xy


# --- the real site -------------------------------------------------------------------

# One free episode per known host: a koma (`content.json`) episode and a PUBLUS pack one.
TEST_URLS: dict[str, str] = {
    "manga-5.com": "https://manga-5.com/product/00850001",
    "www.manga-5.com": "https://www.manga-5.com/product/00870001",
}


@pytest.mark.network
@pytest.mark.parametrize("host", TEST_URLS)
def test_site_download(tmp_path, host):
    result = Downloader(Manga5(), tmp_path, only_first=True).download(TEST_URLS[host])
    assert result.status == "saved"
    assert (result.save_dir / "0.jpg").stat().st_size > 0


@pytest.mark.network
def test_site_sample_volume_is_a_plain_pack(tmp_path):
    result = Downloader(Manga5(), tmp_path, only_first=True).download(BOOK_URL)
    assert result.status == "saved"
    assert result.episode.pages[0].extra == {"pattern": 1, "size": [810, 1152]}
    assert Image.open(result.save_dir / "0.jpg").size == (810, 1152)


@pytest.mark.network
def test_site_locked_episode_has_no_pages():
    episode = Manga5().episode(LOCKED_URL)
    assert not episode.readable
    assert episode.series_title == BOOK_TITLE
    assert episode.episode_title == "CHAPTER 1"
    assert episode.next_url == BOOK_URL


@pytest.mark.network
def test_site_series_lists_episodes():
    urls = Manga5().series_urls(f"{BASE_URL}/content/00870001")
    assert urls[0] == f"{BASE_URL}/product/00870001"
    assert len(urls) >= 10
    assert all(Manga5.suitable(url) for url in urls)
