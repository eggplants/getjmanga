from __future__ import annotations

import base64
import json
from io import BytesIO

import pytest
from PIL import Image

from getjmanga.downloader import Downloader
from getjmanga.extractors.boost import (
    BASE_URL,
    LICENSE_URL,
    LOGIN_URL,
    PACK_KEY,
    Boost,
    Pack,
    _rc4,
    _rc4_sbox,
    _rc4_xor,
    decode_pack,
    descramble,
    hashed_page_name,
    page_seeds,
    tile_slices,
)
from getjmanga.extractors.common import LoginError, NotAnEpisodePageError, UnsupportedUrlError

SERIES_URL = f"{BASE_URL}/content/01700001"
EPISODE_URL = f"{BASE_URL}/product/01700001"
LOCKED_URL = f"{BASE_URL}/product/01700008"
CID = "mmKAG70FqV0g7seJinMQLris+JpZmpM1/AqmwPgbFhjT99rfHGwIvLC75678vnLP"
VIEWER_URL = (
    f"{BASE_URL}/viewer/viewer.html"
    "?cid=mmKAG70FqV0g7seJinMQLris%2BJpZmpM1%2FAqmwPgbFhjT99rfHGwIvLC75678vnLP&com-access-no-history"
)
CONTENT_URL = "https://cdn.comic-boost.com/contents/publus/S0170_ch_001/"
SERIES_TITLE = "ツンリゼ～ツンデレ悪役令嬢リーゼロッテと実況の遠藤くんと解説の小林さん～"

# The three keys of a real pack, as `decode_pack()` hands them out, and what the
# viewer derived from them for its first page (read off the running viewer).
REAL_KEYS = (
    bytes.fromhex("13c05b261788af4f38f448e5203a156bde492fc04c50fbbcbdd8061103a884c6"),
    bytes.fromhex("1f754c3b8013a4c96214a0954cab58b6fcea3b3c797835087d4af22daa0c8360"),
    bytes.fromhex("433ca7ba26e0ad09f746d66a5be915ef3dcc947dd1246068e08935d64e21ce00"),
)
REAL_PAGE = {
    "BlockHeight": 32,
    "BlockWidth": 32,
    "ContentArea": {"Height": 1456, "Width": 1024, "X": 0, "Y": 0},
    "DummyHeight": 0,
    "DummyWidth": 0,
    "LinkList": [],
    "No": 0,
    "Rect": {"Height": 1456, "Width": 1024, "X": 0, "Y": 0},
    "Shrink": 1.0,
    "Size": {"Height": 1456, "Width": 1024},
    "NS": 2044018965,
    "PS": 3200281383,
    "RS": 1889258295,
}
REAL_PATTERN = 94
REAL_TRIPLE = (3919191801, 4251821103, 1527295330)
REAL_SEEDS = {"pattern": REAL_PATTERN, "seeds": list(REAL_TRIPLE), "block": [32, 32]}
REAL_NAME = "1037f2b7c52f49d0ac"


def page_info(no=0, **override):
    return {**REAL_PAGE, "No": no, **override}


# What `configuration_pack.json` says once unwrapped, cut down to what is read.
PACK_JSON = {
    "configuration": {
        "file-name-version": "1.0",
        "page-progression-direction": "rtl",
        "contents": [
            {"file": "OEBPS/text/p-0001.xhtml", "index": 1, "type": "jpeg"},
            {"file": "OEBPS/text/p-0002.xhtml", "index": 2, "type": "jpeg"},
            {"file": "OEBPS/text/p-0003.xhtml", "index": 3, "type": "jpeg"},
            {"file": "OEBPS/text/p-0004.xhtml", "index": 4, "type": "jpeg"},
        ],
    },
    "OEBPS/text/p-0001.xhtml": {
        "FileLinkInfo": {"PageCount": 1, "PageLinkInfoList": [{"Page": page_info()}]},
        "Linear": 1,
    },
    "OEBPS/text/p-0002.xhtml": {
        "FileLinkInfo": {"PageCount": 1, "PageLinkInfoList": [{"Page": page_info(NS=1, PS=2, RS=3)}]},
        "Linear": 1,
    },
    # A page without shuffle parameters is served as is.
    "OEBPS/text/p-0003.xhtml": {
        "FileLinkInfo": {"PageCount": 1, "PageLinkInfoList": [{"Page": {"No": 0, "Size": {"Width": 8, "Height": 8}}}]},
        "Linear": 1,
    },
    # A non-linear page (a cover the viewer skips) is skipped.
    "OEBPS/text/p-0004.xhtml": {
        "FileLinkInfo": {"PageCount": 1, "PageLinkInfoList": [{"Page": page_info()}]},
        "Linear": 0,
    },
}

COLOPHON_HTML = f"""
<html><body><div class="colophon">
<div class="pos-center">
<a class=" primary next btn" href="/product/01700002" data-id="01700002" data-title="第2話" data-coin="0">
<span>次の話を読む</span></a>
</div>
<div class="pos-center"><a class="btn" href="/content/01700001"><span>作品詳細へ戻る</span></a></div>
<div class="js-share-btn-twitter colophon-btn-list-item twitter" data-title="{SERIES_TITLE}"
 data-title-sub="第1話" data-id="01700001" data-url="{SERIES_URL}"></div>
</div></body></html>
"""
LAST_COLOPHON_HTML = f"""
<html><body><div class="colophon">
<div class="pos-center"><a class=" btn" href="/product/01700009" data-id="01700009" data-title="第9話">
<span>前の話を読む</span></a></div>
<div class="pos-center"><a class="btn" href="/content/01700001"><span>作品詳細へ戻る</span></a></div>
<div class="js-share-btn-twitter" data-title="{SERIES_TITLE}" data-title-sub="第10話" data-id="01700010"></div>
</div></body></html>
"""
LOCKED_COLOPHON_HTML = COLOPHON_HTML.replace("第1話", "第8話").replace("01700002", "01700009").replace("第2話", "第9話")
EMPTY_COLOPHON_HTML = "<html><body><div class='colophon-outer'><div class='colophon'></div></div></body></html>"
LOCKED_PRODUCT_HTML = (
    "<html><head><script>alert('閲覧するにはログインが必要です。');history.back();</script></head></html>"
)
MISSING_PRODUCT_HTML = (
    "<html><head><script>alert('作品情報が見つかりませんでした。');history.back();</script></head></html>"
)

LICENSE = {"status": "200", "url": CONTENT_URL, "cti": f"第1話 - {SERIES_TITLE}", "lp": "", "cty": 1, "lpd": 1}


def listing_html(ids, *, last):
    items = "".join(
        f'<a id="product-{i}" class="  book-product-list-item" href="/product/{pid}" data-id="{pid}"'
        f' data-title="第{i}話"></a>'
        for i, pid in enumerate(ids, start=1)
    )
    pager = f'<li class="pagination-list-item to-next{" disabled" if last else ""}"><a href="#"></a></li>'
    return f'<html><body><div class="book-product-list">{items}</div><ul>{pager}</ul></body></html>'


LOGIN_REFUSED = """
<html><body><div class="text-warning-list">
<p class="text-warning">メールアドレスまたはパスワードが違います。(1002)</p>
</div></body></html>
"""


# --- an encoder, so packs can be made up without a 20 KB fixture -------------------------


def _unpermute(buf, keys):  # noqa: C901, PLR0912 (mirrors the branching it undoes)
    """Undo `_permute()`: the swaps are transpositions, the rotation is found by trying."""
    total = xor = 0
    for key in keys:
        for byte in key[:32]:
            total = (total + byte) & 0xFF
            xor ^= byte
    shift = xor >> 5
    size = len(buf)
    start = 0
    while start < size:
        partial = start + 32 > size
        end = min(start + 32, size)
        length = end - start
        out = list(buf[start:end])
        bits = "".join(format(b, "08b") for b in out)
        for rotate in range(length):
            turn = (8 * (length - rotate) - shift) % (8 * length)
            unrolled = bits[-turn:] + bits[:-turn] if turn else bits
            block = [int(unrolled[i : i + 8], 2) for i in range(0, 8 * length, 8)]
            mix = xor
            for b in block:
                mix ^= b
            expected = mix >> 3
            expected = expected % length if partial else expected & 31
            if expected == rotate:
                break
        else:
            raise AssertionError("no rotation fits")
        running = total
        for b in block:
            running = (running + b) & 0xFF
        flags = [(running & bit) != bit for bit in (2, 4, 8, 16, 32)]
        swaps = []
        for k in range(length):
            if (k & 1) != 1:
                continue
            if flags[0]:
                swaps.append((k, k - 1))
            for level, width in enumerate((2, 4, 8, 16), start=1):
                if (k & (2 * width - 1)) != 2 * width - 1:
                    break
                if flags[level]:
                    swaps.extend((k - o, k - width - o) for o in range(width))
        for i, j in reversed(swaps):
            block[i], block[j] = block[j], block[i]
        for k, value in enumerate(block):
            byte = value
            if (total & 8) != 8:
                byte = ((byte & 0x0F) << 4) | ((byte >> 4) & 0x0F)
            if (total & 4) != 4:
                byte = ((byte & 0x33) << 2) | ((byte >> 2) & 0x33)
            if (total & 2) != 2:
                byte = ((byte & 0x55) << 1) | ((byte >> 1) & 0x55)
            buf[start + k] = byte
        start = end


def encode_pack(content, keys, key=PACK_KEY):
    """Wrap `content` the way the CDN serves it, so that `decode_pack()` hands `keys` back."""
    a3, b3, c3 = (bytearray(k) for k in keys)
    data = bytearray(_rc4(json.dumps(content, ensure_ascii=False).encode(), bytes(c3) + bytes(b3) + key))
    _unpermute(a3, [bytes(b3), bytes(c3)])
    _unpermute(b3, [bytes(a3), bytes(c3)])
    _unpermute(c3, [bytes(a3), bytes(b3)])
    a1 = bytearray(_rc4(bytes(a3), key + bytes(c3) + bytes(b3)))
    b1 = bytearray(_rc4(bytes(b3), bytes(a1) + key + bytes(c3)))
    c1 = bytearray(_rc4(bytes(c3), bytes(b1) + bytes(a1) + key))
    slots = [a1, b1, c1, data]
    for i in range(32):
        pick = data[i] ^ a1[i] ^ b1[i] ^ c1[i]
        for src, dst in (((pick & 192) >> 6, (pick & 48) >> 4), ((pick & 12) >> 2, pick & 3)):
            slots[src][i], slots[dst][i] = slots[dst][i], slots[src][i]
    size = len(data)
    _rc4_xor(data, range((size - 1) & -2, -1, -2), bytes(c1) + key + bytes(a1))
    _rc4_xor(data, range((size | 1) - 2, -1, -2), key + bytes(a1) + bytes(b1))
    sbox = _rc4_sbox(bytes(b1) + key + bytes(c1))
    for i in range(size):
        data[i] ^= sbox[i & 0xFF]
    _unpermute(data, [bytes(a1), bytes(b1), bytes(c1)])
    raw = bytes(a1) + bytes(b1) + bytes(c1) + bytes(data)
    return json.dumps({"version": "1.0", "data": base64.b64encode(raw).decode()})


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


def scramble(image, pattern, seeds, block):
    """What the CDN serves: the inverse of `descramble()`."""
    out = Image.new(image.mode, image.size)
    for piece in tile_slices(image.width, image.height, block[0], block[1], pattern=pattern, seeds=seeds):
        tile = image.crop((piece.dst_x, piece.dst_y, piece.dst_x + piece.width, piece.dst_y + piece.height))
        out.paste(tile, (piece.src_x, piece.src_y))
    return out


PACK_TEXT = encode_pack(PACK_JSON, REAL_KEYS)


@pytest.fixture
def client(fake_session, fake_response):
    """A `Boost` over a session scripting the site for episode 1, a locked and the last one."""

    def build(extra=None):
        clean = Image.new("RGB", (64, 48), (200, 30, 30))
        routes = {
            "/colophon/01700001": fake_response(text=COLOPHON_HTML),
            "/colophon/01700008": fake_response(text=LOCKED_COLOPHON_HTML),
            "/colophon/01700010": fake_response(text=LAST_COLOPHON_HTML),
            "/colophon/": fake_response(text=EMPTY_COLOPHON_HTML),
            "/product/01700001": fake_response(text="<html></html>", url=VIEWER_URL),
            "/product/01700010": fake_response(text="<html></html>", url=VIEWER_URL),
            "/product/01700008": fake_response(text=LOCKED_PRODUCT_HTML),
            "/product/": fake_response(text=MISSING_PRODUCT_HTML),
            LICENSE_URL: fake_response(payload=LICENSE),
            "configuration_pack.json": fake_response(text=PACK_TEXT),
            "p-0001.xhtml/": fake_response(
                jpeg_bytes(scramble(clean, REAL_PATTERN, REAL_TRIPLE, (32, 32))),
                content_type="image/jpeg",
            ),
            ".jpeg": fake_response(jpeg_bytes(Image.new("RGB", (8, 8), (1, 2, 3))), content_type="image/jpeg"),
        }
        routes.update(extra or {})  # an override keeps the route's place in the match order
        session = fake_session(routes)
        return Boost(session), session

    return build


# --- URLs ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        EPISODE_URL,
        "https://comic-boost.com/product/01700001/",
        "https://www.comic-boost.com/product/00290111",
        SERIES_URL,
        "https://comic-boost.com/content/00290001/",
        "https://www.comic-boost.com/content/00290001",
    ],
)
def test_suitable_accepts_episode_and_work_urls(url):
    assert Boost.suitable(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://comic-boost.com/product/01700001",
        "https://comic-boost.com/",
        "https://comic-boost.com/product/",
        "https://comic-boost.com/product/1700001",
        "https://comic-boost.com/colophon/01700001",
        "https://comic-boost.com/viewer/viewer.html?cid=abc",
        "https://comic-boost.com/genre/1",
        "https://manga-5.com/product/00850001",
    ],
)
def test_suitable_rejects_other_urls(url):
    assert not Boost.suitable(url)


@pytest.mark.parametrize(
    ("url", "expected"),
    [(EPISODE_URL, False), (SERIES_URL, True), ("https://www.comic-boost.com/content/00290001/", True)],
)
def test_is_series(url, expected):
    assert Boost.is_series(url) is expected


# --- the configuration pack ------------------------------------------------------


def test_decode_pack_round_trips_the_encoder():
    pack = decode_pack(PACK_TEXT)
    assert pack.content == PACK_JSON
    assert pack.keys == REAL_KEYS


@pytest.mark.parametrize("seed", [1, 6])
def test_decode_pack_survives_every_key_flavour(seed):
    # The permutation pass branches on the sum and xor of the key bytes; walk a few.
    keys = tuple(bytes((seed * 131 + i * (k + 7) * 53) & 0xFF for i in range(32)) for k in range(3))
    content = {"configuration": {"contents": []}, "pad": "x" * (seed * 13 + 5)}
    pack = decode_pack(encode_pack(content, keys))
    assert pack.content == content
    assert pack.keys == keys


def test_decode_pack_rejects_a_plain_json():
    with pytest.raises(ValueError, match="no data"):
        decode_pack('{"configuration": {}}')


def test_decode_pack_rejects_a_short_pack():
    with pytest.raises(ValueError, match="too short"):
        decode_pack(json.dumps({"version": "1.0", "data": base64.b64encode(b"x" * 10).decode()}))


def test_decode_pack_rejects_garbage_with_a_json_error():
    keys = (b"a" * 32, b"b" * 32, b"c" * 32)
    text = encode_pack({"configuration": {"contents": []}, "pad": "x" * 40}, keys)
    envelope = json.loads(text)
    raw = bytearray(base64.b64decode(envelope["data"]))
    raw[100] ^= 0xFF
    envelope["data"] = base64.b64encode(bytes(raw)).decode()
    with pytest.raises((ValueError, UnicodeDecodeError)):
        decode_pack(json.dumps(envelope))


# --- page file names -------------------------------------------------------------


def test_hashed_page_name_matches_the_viewer():
    mask = Pack({}, REAL_KEYS).name_mask
    assert hashed_page_name("OEBPS/text/p-0001.xhtml", "0", mask) == REAL_NAME


# --- the shuffle -------------------------------------------------------------------


def test_page_seeds_match_the_viewer():
    assert page_seeds("OEBPS/text/p-0001.xhtml", "0", REAL_PAGE, REAL_KEYS) == REAL_SEEDS


def test_page_seeds_are_empty_without_the_shuffle_parameters():
    assert page_seeds("OEBPS/text/p-0001.xhtml", "0", {"No": 0, "BlockWidth": 32}, REAL_KEYS) == {}


def test_tile_slices_match_the_viewer():
    slices = tile_slices(1024, 1456, 32, 32, pattern=REAL_PATTERN, seeds=REAL_TRIPLE)
    assert len(slices) == 1472
    # The viewer's first and last slice, with its src/dest read the other way round.
    first, last = slices[0], slices[-1]
    assert (first.src_x, first.src_y, first.dst_x, first.dst_y, first.width, first.height) == (384, 1360, 0, 0, 32, 32)
    assert (last.src_x, last.src_y, last.dst_x, last.dst_y, last.width, last.height) == (864, 1024, 992, 192, 32, 16)


@pytest.mark.parametrize(("size", "block"), [((1024, 1456), (32, 32)), ((100, 70), (32, 32)), ((64, 64), (16, 16))])
def test_tile_slices_partition_the_page(size, block):
    covered = bytearray(size[0] * size[1])
    for piece in tile_slices(size[0], size[1], block[0], block[1], pattern=94, seeds=(1, 2, 3)):
        assert 0 <= piece.src_x <= size[0] - piece.width
        assert 0 <= piece.src_y <= size[1] - piece.height
        for y in range(piece.dst_y, piece.dst_y + piece.height):
            for x in range(piece.dst_x, piece.dst_x + piece.width):
                covered[y * size[0] + x] += 1
    assert set(covered) == {1}


def test_descramble_restores_a_synthetic_page():
    clean = striped((100, 70))
    shuffled = scramble(clean, REAL_PATTERN, REAL_TRIPLE, (32, 32))
    assert shuffled.tobytes() != clean.tobytes()
    assert descramble(shuffled, REAL_PATTERN, REAL_TRIPLE, (32, 32)).tobytes() == clean.tobytes()


# --- the colophon ------------------------------------------------------------------


# --- episodes ----------------------------------------------------------------------


def test_episode_reads_the_titles_the_pages_and_the_next_episode(client):
    boost, session = client()
    episode = boost.episode(EPISODE_URL)

    assert episode.url == EPISODE_URL
    assert episode.series_title == SERIES_TITLE
    assert episode.episode_title == "第1話"
    assert episode.next_url == f"{BASE_URL}/product/01700002"
    assert [page.url for page in episode.pages] == [
        f"{CONTENT_URL}OEBPS/text/p-0001.xhtml/{REAL_NAME}.jpeg",
        f"{CONTENT_URL}OEBPS/text/p-0002.xhtml/10"
        + hashed_page_name("OEBPS/text/p-0002.xhtml", "0", Pack({}, REAL_KEYS).name_mask)[2:]
        + ".jpeg",
        f"{CONTENT_URL}OEBPS/text/p-0003.xhtml/10"
        + hashed_page_name("OEBPS/text/p-0003.xhtml", "0", Pack({}, REAL_KEYS).name_mask)[2:]
        + ".jpeg",
    ]
    assert episode.pages[0].extra == REAL_SEEDS
    assert episode.pages[0].width == 1024
    assert episode.pages[0].height == 1456
    assert episode.pages[2].extra == {}
    assert episode.metadata["license"] == LICENSE
    assert episode.metadata["configuration"] == PACK_JSON["configuration"]
    assert episode.metadata["colophon"]["data-title-sub"] == "第1話"
    json.dumps(episode.metadata)

    # The colophon, the product page, the license call with its cid, then the pack.
    assert session.calls[:2] == [f"{BASE_URL}/colophon/01700001", EPISODE_URL]
    assert session.calls[2] == LICENSE_URL
    assert session.params_seen[2] == {"cid": CID}
    assert session.headers_seen[2]["Referer"] == VIEWER_URL
    assert session.calls[3] == f"{CONTENT_URL}configuration_pack.json"


def test_episode_normalises_the_host_and_the_trailing_slash(client):
    boost, session = client()
    episode = boost.episode("https://www.comic-boost.com/product/01700001/")
    assert episode.url == EPISODE_URL
    assert session.calls[1] == EPISODE_URL


def test_locked_episode_has_no_pages_but_keeps_its_titles(client):
    boost, session = client()
    episode = boost.episode(LOCKED_URL)

    assert episode.pages == ()
    assert not episode.readable
    assert episode.series_title == SERIES_TITLE
    assert episode.episode_title == "第8話"
    assert episode.next_url == f"{BASE_URL}/product/01700009"
    assert LICENSE_URL not in session.calls


def test_last_episode_has_no_next(client):
    boost, _ = client()
    episode = boost.episode(f"{BASE_URL}/product/01700010")
    assert episode.readable
    assert episode.episode_title == "第10話"
    assert episode.next_url is None


def test_unknown_episode_is_not_an_episode_page(client):
    boost, _ = client()
    with pytest.raises(NotAnEpisodePageError, match="no episode 01709999"):
        boost.episode(f"{BASE_URL}/product/01709999")


def test_readable_episode_without_a_colophon_falls_back_to_the_license_title(client, fake_response):
    boost, _ = client({"/colophon/01700001": fake_response(text=EMPTY_COLOPHON_HTML)})
    episode = boost.episode(EPISODE_URL)
    assert episode.readable
    assert episode.series_title == SERIES_TITLE
    assert episode.episode_title == "第1話"
    assert episode.next_url is None


def test_refused_license_means_locked(client, fake_response):
    boost, _ = client({LICENSE_URL: fake_response(payload={"status": 401})})
    episode = boost.episode(EPISODE_URL)
    assert not episode.readable
    assert episode.episode_title == "第1話"
    assert episode.metadata["license"] == {"status": 401}


def test_episode_rejects_a_work_url(client):
    boost, _ = client()
    with pytest.raises(UnsupportedUrlError):
        boost.episode(SERIES_URL)


# --- series ------------------------------------------------------------------------


def test_series_urls_walks_the_pages_oldest_first(fake_session, fake_response):
    session = fake_session(
        {
            "/content/": [
                fake_response(text=listing_html(["01700001", "01700002"], last=False)),
                fake_response(text=listing_html(["01700002", "01700003"], last=True)),
            ],
        }
    )
    urls = Boost(session).series_urls("https://www.comic-boost.com/content/01700001/")
    assert urls == [f"{BASE_URL}/product/0170000{i}" for i in (1, 2, 3)]
    assert all(Boost.suitable(url) for url in urls)
    assert session.calls == [SERIES_URL, SERIES_URL]
    assert session.params_seen == [{"order": "asc", "p": 1}, {"order": "asc", "p": 2}]


def test_series_urls_stops_when_a_page_brings_nothing_new(fake_session, fake_response):
    session = fake_session({"/content/": fake_response(text=listing_html(["01700001"], last=False))})
    assert Boost(session).series_urls(SERIES_URL) == [EPISODE_URL]
    assert len(session.calls) == 2


def test_series_urls_rejects_an_episode_url(client):
    boost, _ = client()
    with pytest.raises(UnsupportedUrlError):
        boost.series_urls(EPISODE_URL)


def test_series_urls_raises_on_an_empty_listing(fake_session, fake_response):
    session = fake_session({"/content/": fake_response(text=listing_html([], last=True))})
    with pytest.raises(NotAnEpisodePageError, match="lists no episode"):
        Boost(session).series_urls(SERIES_URL)


# --- images ------------------------------------------------------------------------


def test_image_descrambles_a_page_and_leaves_a_plain_one_alone(client):
    boost, session = client()
    episode = boost.episode(EPISODE_URL)

    page = boost.image(episode.pages[0], episode)
    assert page.size == (64, 48)
    assert all(abs(a - b) < 8 for a, b in zip(page.convert("RGB").getpixel((5, 40)), (200, 30, 30), strict=True))
    assert session.headers_seen[-1]["Referer"] == EPISODE_URL

    plain = boost.image(episode.pages[2], episode)
    assert plain.size == (8, 8)
    assert all(abs(a - b) < 8 for a, b in zip(plain.convert("RGB").getpixel((0, 0)), (1, 2, 3), strict=True))


# --- logging in ----------------------------------------------------------------------


def test_login_posts_the_form_and_accepts_a_redirect_elsewhere(fake_session, fake_response):
    session = fake_session({LOGIN_URL: fake_response(text="<html><body>マイページ</body></html>", url=f"{BASE_URL}/")})
    Boost(session).login(EPISODE_URL, "someone@example.com", "hunter2")

    url, body = session.posts[0]
    assert url == LOGIN_URL
    assert body == {"account[email]": "someone@example.com", "account[password]": "hunter2"}


def test_login_raises_with_the_site_reason(fake_session, fake_response):
    session = fake_session({LOGIN_URL: fake_response(text=LOGIN_REFUSED, url=f"{LOGIN_URL}?msgid=1002")})
    with pytest.raises(LoginError, match=r"違います。\(1002\)"):
        Boost(session).login(EPISODE_URL, "someone@example.com", "wrong")


def test_login_raises_when_bounced_back_without_a_reason(fake_session, fake_response):
    session = fake_session({LOGIN_URL: fake_response(text="<html></html>", url=LOGIN_URL)})
    with pytest.raises(LoginError, match="no reason given"):
        Boost(session).login(EPISODE_URL, "someone@example.com", "wrong")


# --- the real site -------------------------------------------------------------------

# One free episode per known host: the first episode of a long-running series.
TEST_URLS: dict[str, str] = {
    "comic-boost.com": "https://comic-boost.com/product/01700001",
    "www.comic-boost.com": "https://www.comic-boost.com/product/00290001",
}


@pytest.mark.network
@pytest.mark.parametrize("host", TEST_URLS)
def test_site_download(tmp_path, host):
    result = Downloader(Boost(), tmp_path, only_first=True).download(TEST_URLS[host])
    assert result.status == "saved"
    assert (result.save_dir / "0.jpg").stat().st_size > 0


@pytest.mark.network
def test_site_locked_episode_has_no_pages():
    episode = Boost().episode(LOCKED_URL)
    assert not episode.readable
    assert episode.episode_title == "第8話"
    assert episode.next_url == f"{BASE_URL}/product/01700009"


@pytest.mark.network
def test_site_series_lists_episodes():
    urls = Boost().series_urls(SERIES_URL)
    assert urls[0] == EPISODE_URL
    assert len(urls) >= 10
    assert all(Boost.suitable(url) for url in urls)
