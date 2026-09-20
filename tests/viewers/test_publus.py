from __future__ import annotations

import base64
import json
from io import BytesIO
from typing import Any

import pytest
from PIL import Image

from getjmanga.viewers.publus import (
    PACK_KEY,
    Pack,
    _rc4,
    _rc4_sbox,
    _rc4_xor,
    decode_pack,
    descramble,
    hashed_page_name,
    page_seeds,
    pages,
    tile_slices,
)

CONTENT_URL = "https://cdn.comic-boost.com/contents/publus/S0170_ch_001/"

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


# --- the page walk ---------------------------------------------------------------


def test_pages_names_every_linear_page_and_seeds_the_shuffled_ones():
    listed = pages(Pack(PACK_JSON, REAL_KEYS), CONTENT_URL)
    mask = Pack({}, REAL_KEYS).name_mask
    assert [page.url for page in listed] == [
        f"{CONTENT_URL}OEBPS/text/p-0001.xhtml/{REAL_NAME}.jpeg",
        f"{CONTENT_URL}OEBPS/text/p-0002.xhtml/{hashed_page_name('OEBPS/text/p-0002.xhtml', '0', mask)}.jpeg",
        f"{CONTENT_URL}OEBPS/text/p-0003.xhtml/{hashed_page_name('OEBPS/text/p-0003.xhtml', '0', mask)}.jpeg",
    ]
    assert (listed[0].width, listed[0].height) == (1024, 1456)
    assert listed[0].extra == REAL_SEEDS
    assert listed[2].extra == {}


def test_pages_use_the_page_number_as_the_file_name_without_a_name_version():
    content: dict[str, Any] = dict(PACK_JSON)
    content["configuration"] = {**PACK_JSON["configuration"], "file-name-version": None}
    listed = pages(Pack(content, REAL_KEYS), CONTENT_URL)
    assert listed[0].url == f"{CONTENT_URL}OEBPS/text/p-0001.xhtml/0.jpeg"
