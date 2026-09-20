"""ACCESS's PUBLUS Reader for Browser, and the three modules of it that stand between a page and its image.

The content directory of an episode holds a `configuration_pack.json`
wrapped as `{"version": "1.0", "data": <base64>}`. The viewer (`NFBR.a6i.H6f`)
unwraps it with a home-grown cipher keyed by the string
`configuration_pack.json`: the first 96 bytes are three 32-byte keys, the
rest is the JSON, and both go through bit-permutation and RC4 passes before
the JSON can be parsed. The three keys as they come out at the end (`ct`,
`st`, `et` in the viewer) also seed the page file names (`NFBR.a6i.R3s`, when
`file-name-version` is set) and the tile shuffle (`NFBR.b8F`, an xorshift32
PRNG picked from a table of 486 parameter sets). `decode_pack()`,
`hashed_page_name()` and `descramble()` are ports of those three modules, and
`pages()` walks a pack the way the viewer lists its pages.

What is the site's -- how the content directory is found, the titles, the
login -- stays with each extractor.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin

from PIL import Image

from getjmanga.extractor import Page

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

#: The key the viewer unwraps the configuration pack with.
PACK_KEY = b"configuration_pack.json"
#: Bytes of the three keys at the head of the pack, and their length.
_KEY_LEN = 32
_HEADER_LEN = 3 * _KEY_LEN
_BLOCK = 32
_BYTE = 0xFF
_MASK32 = 0xFFFFFFFF
_TWO16 = 1 << 16
_TWO32 = 1 << 32

# --- the configuration pack (NFBR.a6i.H6f) -------------------------------------------


def _rc4_sbox(key: bytes) -> list[int]:
    """RC4's key schedule."""
    sbox = list(range(256))
    j = 0
    for i in range(256):
        j = (j + sbox[i] + key[i % len(key)]) & _BYTE
        sbox[i], sbox[j] = sbox[j], sbox[i]
    return sbox


def _rc4(data: bytes, key: bytes) -> bytes:
    """Plain RC4 over `data`."""
    out = bytearray(data)
    _rc4_xor(out, range(len(out)), key)
    return bytes(out)


def _rc4_xor(buf: bytearray, indices: range, key: bytes) -> None:
    """XOR the RC4 keystream of `key` onto `buf` at `indices`, in that order."""
    sbox = _rc4_sbox(key)
    i = j = 0
    for index in indices:
        i = (i + 1) & _BYTE
        j = (j + sbox[i]) & _BYTE
        sbox[i], sbox[j] = sbox[j], sbox[i]
        buf[index] ^= sbox[(sbox[i] + sbox[j]) & _BYTE]


def _swap_halves(block: list[int], end: int, half: int) -> None:
    """Swap the two halves of `block[end - 2 * half + 1 : end + 1]`."""
    for offset in range(half):
        block[end - offset], block[end - half - offset] = block[end - half - offset], block[end - offset]


def _permute(buf: bytearray, keys: list[bytes]) -> None:
    """The viewer's `I6V` pass: bit and byte shuffles of every 32-byte block, keyed by the key bytes."""
    total = xor = 0
    for key in keys:
        for byte in key[:_KEY_LEN]:
            total = (total + byte) & _BYTE
            xor ^= byte
    swap_bits, swap_pairs, swap_nibbles = ((total & bit) != bit for bit in (2, 4, 8))
    shift = xor >> 5
    size = len(buf)
    start = 0
    while start < size:
        partial = start + _BLOCK > size
        end = min(start + _BLOCK, size)
        length = end - start
        block = [0] * length
        running, mix = total, xor
        for k in range(length):
            byte = buf[start + k]
            if swap_bits:
                byte = ((byte & 0x55) << 1) | ((byte >> 1) & 0x55)
            if swap_pairs:
                byte = ((byte & 0x33) << 2) | ((byte >> 2) & 0x33)
            if swap_nibbles:
                byte = ((byte & 0x0F) << 4) | ((byte >> 4) & 0x0F)
            block[k] = byte
            running = (running + byte) & _BYTE
            mix ^= byte
        flags = [(running & bit) != bit for bit in (2, 4, 8, 16, 32)]
        for k in range(length):
            if (k & 1) != 1:
                continue
            if flags[0]:
                block[k], block[k - 1] = block[k - 1], block[k]
            for level, width in enumerate((2, 4, 8, 16), start=1):
                if (k & (2 * width - 1)) != 2 * width - 1:
                    break
                if flags[level]:
                    _swap_halves(block, k, width)
        rotate = mix >> 3
        rotate = rotate % length if partial else rotate & (_BLOCK - 1)
        for k in range(length):
            second = block[(length - rotate + k) % length]
            if shift == 0:
                buf[start + k] = second
            else:
                first = block[(length - rotate - 1 + k) % length]
                buf[start + k] = ((first << (8 - shift)) | (second >> shift)) & _BYTE
        start = end


@dataclass(frozen=True)
class Pack:
    """A configuration pack once unwrapped."""

    #: The pack's JSON: `configuration` plus one entry per page file.
    content: dict[str, Any]
    #: The three keys the viewer calls `ct`, `st` and `et`, as they seed the rest.
    keys: tuple[bytes, bytes, bytes]

    @property
    def name_mask(self) -> bytes:
        """The bytes page file names are hashed with (`book.N0f`)."""
        return bytes(x ^ y ^ z for x, y, z in zip(*self.keys, strict=True))


def decode_pack(text: str, key: bytes = PACK_KEY) -> Pack:
    """Unwrap a `{"version", "data"}` configuration pack.

    Args:
        text: The pack as the CDN serves it.
        key: The string the viewer keys the cipher with; the pack's file name.

    Returns:
        The pack's JSON and the three keys.

    Raises:
        ValueError: The text is no wrapped pack.
    """
    envelope = json.loads(text)
    if not isinstance(envelope, dict) or "data" not in envelope:
        msg = "no data in the configuration pack."
        raise ValueError(msg)
    raw = base64.b64decode(envelope["data"])
    if len(raw) < _HEADER_LEN + _KEY_LEN:
        msg = "the configuration pack is too short."
        raise ValueError(msg)
    a = bytearray(raw[:_KEY_LEN])
    b = bytearray(raw[_KEY_LEN : 2 * _KEY_LEN])
    c = bytearray(raw[2 * _KEY_LEN : _HEADER_LEN])
    data = bytearray(raw[_HEADER_LEN:])
    size = len(data)

    _permute(data, [bytes(a), bytes(b), bytes(c)])
    sbox = _rc4_sbox(bytes(b) + key + bytes(c))
    for i in range(size):
        data[i] ^= sbox[i & _BYTE]
    _rc4_xor(data, range((size | 1) - 2, -1, -2), key + bytes(a) + bytes(b))
    _rc4_xor(data, range((size - 1) & -2, -1, -2), bytes(c) + key + bytes(a))
    slots = [a, b, c, data]
    for i in range(_KEY_LEN):
        pick = data[i] ^ a[i] ^ b[i] ^ c[i]
        for src, dst in (((pick & 12) >> 2, pick & 3), ((pick & 192) >> 6, (pick & 48) >> 4)):
            slots[src][i], slots[dst][i] = slots[dst][i], slots[src][i]
    c = bytearray(_rc4(bytes(c), bytes(b) + bytes(a) + key))
    b = bytearray(_rc4(bytes(b), bytes(a) + key + bytes(c)))
    a = bytearray(_rc4(bytes(a), key + bytes(c) + bytes(b)))
    _permute(c, [bytes(a), bytes(b)])
    _permute(b, [bytes(a), bytes(c)])
    _permute(a, [bytes(b), bytes(c)])
    plain = _rc4(bytes(data), bytes(c) + bytes(b) + key)
    content = json.loads(plain.decode("utf-8"))
    configuration = content.get("configuration") if isinstance(content, dict) else None
    if not configuration or not isinstance(configuration, dict):
        msg = "the configuration pack decoded to something else."
        raise ValueError(msg)
    return Pack(content, (bytes(a), bytes(b), bytes(c)))


# --- page file names (NFBR.a6i.R3s) --------------------------------------------------

_NAME_LIMBS = (1670739, 1282576, 2237221)
_NAME_MUL = 435
_LIMB21 = 0x1FFFFF
_LIMB22 = 0x3FFFFF
_MAX_PAGE_NO = 0xFFFFFFFFFFFFFFF


def hashed_page_name(file: str, page_no: str, mask: bytes) -> str:
    """Name a page file the way the viewer does when `file-name-version` is set.

    Args:
        file: The page's entry in `configuration.contents`, `OEBPS/text/p-0001.xhtml`.
        page_no: The page's `No` within that file, as a string.
        mask: `Pack.name_mask`.

    Returns:
        The file name without its extension: a length-prefixed hex page
        number and sixteen hex digits of a hash over the path.
    """
    try:
        number = int(page_no, 10)
    except ValueError:
        number = -1
    if 0 <= number <= _MAX_PAGE_NO:
        digits = format(number, "x")
        head = format(len(digits), "x") + digits
    else:
        head = "0" + page_no

    prefix = file + "/"
    units = [0, 59]
    for char in prefix + page_no:
        units.append(ord(char) >> 8)
        units.append(ord(char) & _BYTE)
    total = len(units)
    covered = 2 * len(page_no) + 2 * total
    rounds = 3
    while covered < 256:
        covered += total
        rounds += 1
    high, mid, low = _NAME_LIMBS
    at = 2 * (1 + len(prefix))  # the first round skips the directory part
    key_at = 0
    for _ in range(rounds):
        while at < total:
            low ^= units[at] ^ mask[key_at]
            at += 1
            key_at = (key_at + 1) % len(mask)
            low_mul = _NAME_MUL * low
            mid_mul = _NAME_MUL * mid + ((low & 7) << 18) + (low_mul >> 22)
            high_mul = _NAME_MUL * high + ((mid & 3) << 19) + ((low & 0x3FFFF8) >> 3) + (mid_mul >> 21)
            low = low_mul & _LIMB22
            mid = mid_mul & _LIMB21
            high = high_mul & _LIMB21
        at = 0
    digest = (
        high >> 13,
        (high >> 5) & _BYTE,
        ((high & 31) << 3) | (mid >> 18),
        (mid >> 10) & _BYTE,
        (mid >> 2) & _BYTE,
        ((mid & 3) << 6) | (low >> 16),
        (low >> 8) & _BYTE,
        low & _BYTE,
    )
    return head + "".join(format((value ^ mask[i]) & _BYTE, "02x") for i, value in enumerate(digest))


# --- the tile shuffle (NFBR.K8j and NFBR.b8F) ----------------------------------------

#: The xorshift32 parameter table the viewer picks a generator from.
_XORSHIFT_PARAMS: tuple[tuple[int, int, int], ...] = (
    (1, 3, 10), (1, 5, 16), (1, 5, 19), (1, 9, 29), (1, 11, 6), (1, 11, 16), (1, 19, 3), (1, 21, 20),
    (1, 27, 27), (2, 5, 15), (2, 5, 21), (2, 7, 7), (2, 7, 9), (2, 7, 25), (2, 9, 15), (2, 15, 17),
    (2, 15, 25), (2, 21, 9), (3, 1, 14), (3, 3, 26), (3, 3, 28), (3, 3, 29), (3, 5, 20), (3, 5, 22),
    (3, 5, 25), (3, 7, 29), (3, 13, 7), (3, 23, 25), (3, 25, 24), (3, 27, 11), (4, 3, 17), (4, 3, 27),
    (4, 5, 15), (5, 3, 21), (5, 7, 22), (5, 9, 7), (5, 9, 28), (5, 9, 31), (5, 13, 6), (5, 15, 17),
    (5, 17, 13), (5, 21, 12), (5, 27, 8), (5, 27, 21), (5, 27, 25), (5, 27, 28), (6, 1, 11), (6, 3, 17),
    (6, 17, 9), (6, 21, 7), (6, 21, 13), (7, 1, 9), (7, 1, 18), (7, 1, 25), (7, 13, 25), (7, 17, 21),
    (7, 25, 12), (7, 25, 20), (8, 7, 23), (8, 9, 23), (9, 5, 14), (9, 5, 25), (9, 11, 19), (9, 21, 16),
    (10, 9, 21), (10, 9, 25), (11, 7, 12), (11, 7, 16), (11, 17, 13), (11, 21, 13), (12, 9, 23), (13, 3, 17),
    (13, 3, 27), (13, 5, 19), (13, 17, 15), (14, 1, 15), (14, 13, 15), (15, 1, 29), (17, 15, 20), (17, 15, 23),
    (17, 15, 26),
)  # fmt: skip
_DEFAULT_SEED = 2463534242


def _shl(x: int, n: int) -> int:
    return (x << n) & _MASK32


def _xs0(x: int, a: int, b: int, c: int) -> int:
    x ^= _shl(x, a)
    x ^= x >> b
    return x ^ _shl(x, c)


def _xs1(x: int, a: int, b: int, c: int) -> int:
    x ^= _shl(x, c)
    x ^= x >> b
    return x ^ _shl(x, a)


def _xs2(x: int, a: int, b: int, c: int) -> int:
    x ^= x >> a
    x ^= _shl(x, b)
    return x ^ (x >> c)


def _xs3(x: int, a: int, b: int, c: int) -> int:
    x ^= x >> c
    x ^= _shl(x, b)
    return x ^ (x >> a)


def _xs4(x: int, a: int, b: int, c: int) -> int:
    x ^= _shl(x, a)
    x ^= _shl(x, c)
    return x ^ (x >> b)


def _xs5(x: int, a: int, b: int, c: int) -> int:
    x ^= x >> a
    x ^= x >> c
    return x ^ _shl(x, b)


_XORSHIFT_ORDERS: tuple[Callable[[int, int, int, int], int], ...] = (_xs0, _xs1, _xs2, _xs3, _xs4, _xs5)
#: How many distinct generators the table and the six shift orders make.
PATTERN_COUNT = len(_XORSHIFT_PARAMS) * len(_XORSHIFT_ORDERS)


class _Xorshift:
    """The viewer's `NFBR.K8j`: xorshift32 with a selectable parameter set and shift order."""

    def __init__(self) -> None:
        self.state = _DEFAULT_SEED
        self.params = _XORSHIFT_PARAMS[74]
        self.step = _XORSHIFT_ORDERS[0]

    def select(self, variant: int) -> None:
        """Pick generator number `variant` of `PATTERN_COUNT` and reset the state."""
        self.state = _DEFAULT_SEED
        order = variant % len(_XORSHIFT_ORDERS)
        self.params = _XORSHIFT_PARAMS[((variant - order) // len(_XORSHIFT_ORDERS)) % len(_XORSHIFT_PARAMS)]
        self.step = _XORSHIFT_ORDERS[order]

    def seed(self, value: int) -> None:
        """Seed the state; zero falls back to the default seed."""
        self.state = (value & _MASK32) or _DEFAULT_SEED

    def below(self, n: int) -> int:
        """A number in `[0, n)`, drawn without modulo bias."""
        if n <= 1:
            return 0
        limit = _MASK32 - n
        while True:
            self.state = self.step(self.state, *self.params)
            value = self.state - 1
            rest = value % n
            if value - rest <= limit:
                return rest


def _int32(value: int) -> int:
    """JavaScript's ToInt32."""
    value &= _MASK32
    return value - _TWO32 if value & 0x80000000 else value


def _shuffle(rng: _Xorshift, n: int) -> list[int]:
    """An inside-out Fisher-Yates permutation of `range(n)`."""
    out = [0] * n
    for i in range(n):
        j = rng.below(i + 1)
        out[i] = out[j]
        out[j] = i
    return out


def _pick(rng: _Xorshift, n: int) -> int:
    return rng.below(n + 1) if n < 4 else rng.below(n - 1) + 1


def _pick_other(rng: _Xorshift, taken: int, n: int) -> int:
    if n <= 0:
        return 0
    value = rng.below(n)
    return value if value < taken else value + 1


def _fill(
    rng: _Xorshift,
    *,
    cols: dict[int, int],
    rows: dict[int, int],
    col_gap: int,
    row_gap: int,
    width: int,
    height: int,
) -> None:
    """The viewer's `yLILi`: draw the two index tables the edge strips are placed with.

    The tables are sparse in the viewer, and comparisons against a missing
    entry come out false there, which is what the `.get()` guards keep.
    """
    left, down, free_cols, free_rows = width, height, col_gap, row_gap
    row_at = col_at = 0

    def above(value: int, bound: int | None) -> bool:
        return bound is not None and value >= bound

    def within(value: int, bound: int | None) -> bool:
        return bound is not None and value <= bound

    while left + down > 0:
        draw = rng.below(left + down)
        if draw < left:
            if draw < free_cols:
                lo = col_at
                while lo > 0 and not above(row_at, cols.get(lo - 1)):
                    lo -= 1
                hi = col_at + down
                while hi < height and not above(row_at, cols.get(hi)):
                    hi += 1
                rows[row_at] = rng.below(hi - lo) + lo
                row_at += 1
                free_cols -= 1
            else:
                lo = col_at
                while lo > 0 and not within(row_at + left, cols.get(lo - 1)):
                    lo -= 1
                hi = col_at + down
                while hi < height and not within(row_at + left, cols.get(hi)):
                    hi += 1
                rows[row_at + left - 1] = rng.below(hi - lo) + lo
            left -= 1
        else:
            if draw - left < free_rows:
                lo = row_at
                while lo > 0 and not above(col_at, rows.get(lo - 1)):
                    lo -= 1
                hi = row_at + left
                while hi < width and not above(col_at, rows.get(hi)):
                    hi += 1
                cols[col_at] = rng.below(hi - lo) + lo
                col_at += 1
                free_rows -= 1
            else:
                lo = row_at
                while lo > 0 and not within(col_at + down, rows.get(lo - 1)):
                    lo -= 1
                hi = row_at + left
                while hi < width and not within(col_at + down, rows.get(hi)):
                    hi += 1
                cols[col_at + down - 1] = rng.below(hi - lo) + lo
            down -= 1


def _tile_pairs(pattern: int, sx: int, sy: int, sz: int) -> list[int]:
    """The viewer's `NFBR.b8F.a3f`: the source/destination cell pairs of every tile.

    The arguments are the JavaScript numbers the viewer passes: each carries a
    tile count or the pattern in its upper half and a seed in its lower 32 bits.
    """
    rng = _Xorshift()
    seed_all = _int32(sx) ^ _int32(sy) ^ _int32(sz)
    hi_p, hi_x, hi_y, hi_z = pattern // _TWO16, sx // _TWO16, sy // _TWO16, sz // _TWO16
    mixed = (_int32(hi_x) ^ _int32(hi_y) ^ _int32(hi_z)) & _MASK32
    picker = _int32(hi_p) ^ _int32(hi_z)
    seed_x = _int32(pattern) ^ _int32(sx)
    seed_y = _int32(pattern) ^ _int32(sy)
    seed_z = _int32(pattern) ^ _int32(sz)

    rng.select(mixed >> 16)
    rng.seed(seed_all)
    salt = _int32(rng.below(_TWO16) | (rng.below(_TWO16) << 16))
    tweak = rng.below(512)
    width = (hi_x & _MASK32) >> 16
    height = (hi_y & _MASK32) >> 16
    seed_x = (seed_x ^ salt) & _MASK32
    seed_y = (seed_y ^ salt) & _MASK32
    seed_z = (seed_z ^ salt) & _MASK32

    rng.select(((picker & _MASK32) >> 16) ^ tweak)
    rng.seed(seed_x)
    order = _shuffle(rng, width * height)
    rng.seed(seed_y)
    col_gap = _pick(rng, width)
    row_gap = _pick(rng, height)
    col_gap2 = _pick_other(rng, col_gap, width)
    row_gap2 = _pick_other(rng, row_gap, height)
    rng.seed(seed_z)
    cols1: dict[int, int] = {}
    rows1: dict[int, int] = {}
    _fill(rng, cols=cols1, rows=rows1, col_gap=col_gap, row_gap=row_gap, width=width, height=height)
    col_order = _shuffle(rng, width)
    row_order = _shuffle(rng, height)
    cols2: dict[int, int] = {}
    rows2: dict[int, int] = {}
    _fill(rng, cols=cols2, rows=rows2, col_gap=col_gap2, row_gap=row_gap2, width=width, height=height)

    def past(value: int, bound: int | None, step: int) -> int:
        return value if bound is not None and value < bound else value + step

    span_x, span_y = width + 1, height + 1
    cells_x, cells_y = span_x << 1, span_y << 1
    out: list[int] = []
    for x in range(width):
        for y in range(height):
            cell = order[x + y * width]
            cx = cell % width
            cy = (cell - cx) // width
            out.append(past(cy, rows2.get(cx), span_y) * cells_x + past(x, cols1.get(y), span_x))
            out.append(past(cx, cols2.get(cy), span_x) * cells_y + past(y, rows1.get(x), span_y))
    out.append(row_gap2 * cells_x + col_gap)
    out.append(col_gap2 * cells_y + row_gap)
    for x in range(width):
        y = rows1[x]
        cx = col_order[x]
        cy = rows2[cx]
        out.append(cy * cells_x + past(x, col_gap, span_x))
        out.append(past(cx, col_gap2, span_x) * cells_y + y)
    for y in range(height):
        x = cols1[y]
        cy = row_order[y]
        cx = cols2[cy]
        out.append(past(cy, row_gap2, span_y) * cells_x + x)
        out.append(cx * cells_y + past(y, row_gap, span_y))
    return out


@dataclass(frozen=True)
class Slice:
    """One rectangle of a scrambled page and where it belongs."""

    src_x: int
    src_y: int
    dst_x: int
    dst_y: int
    width: int
    height: int


def tile_slices(
    width: int,
    height: int,
    block_width: int,
    block_height: int,
    *,
    pattern: int,
    seeds: tuple[int, int, int],
) -> list[Slice]:
    """Where every rectangle of a scrambled page came from.

    Args:
        width: The scrambled image's width.
        height: The scrambled image's height.
        block_width: `BlockWidth` of the page.
        block_height: `BlockHeight` of the page.
        pattern: The page's generator number, `0 <= pattern < PATTERN_COUNT`.
        seeds: The page's three 32-bit seeds.

    Returns:
        The full tiles first, then the corner and the two edge strips.
    """
    nx, ny = width // block_width, height // block_height
    rx, ry = width % block_width, height % block_height
    cells_x, cells_y = (nx + 1) << 1, (ny + 1) << 1
    edge_x, edge_y = (nx + 1) * block_width - rx, (ny + 1) * block_height - ry
    rng = _Xorshift()
    rng.select(pattern ^ nx ^ ny)
    rng.seed(seeds[0] ^ seeds[1] ^ seeds[2])
    salt = rng.below(_TWO16)
    salt += rng.below(_TWO16) * _TWO16
    salt += rng.below(512) * _TWO32
    pairs = _tile_pairs(
        salt,
        nx * _TWO32 + seeds[0],
        ny * _TWO32 + seeds[1],
        pattern * _TWO32 + seeds[2],
    )

    out: list[Slice] = []

    def take(start: int, end: int, w: int, h: int) -> None:
        if w == 0 or h == 0:
            return
        for i in range(start, end, 2):
            src, dst = pairs[i], pairs[i + 1]
            sx, dy = src % cells_x, dst % cells_y
            dx, sy = (dst - dy) // cells_y, (src - sx) // cells_x
            out.append(
                Slice(
                    # The viewer names these the other way round and draws
                    # from its "dest" to its "src"; here src is the scrambled image.
                    dx * block_width - (edge_x if dx > nx else 0),
                    sy * block_height - (edge_y if sy > ny else 0),
                    sx * block_width - (edge_x if sx > nx else 0),
                    dy * block_height - (edge_y if dy > ny else 0),
                    w,
                    h,
                )
            )

    bounds = [0, nx * ny * 2]
    take(bounds[0], bounds[1], block_width, block_height)
    bounds = [bounds[1], bounds[1] + 2]
    take(bounds[0], bounds[1], rx, ry)
    bounds = [bounds[1], bounds[1] + nx * 2]
    take(bounds[0], bounds[1], block_width, ry)
    bounds = [bounds[1], bounds[1] + ny * 2]
    take(bounds[0], bounds[1], rx, block_height)
    return out


def page_seeds(file: str, page_no: str, info: Mapping[str, Any], keys: tuple[bytes, bytes, bytes]) -> dict[str, Any]:
    """The shuffle parameters of one page, or nothing when it is not scrambled.

    Args:
        file: The page's entry in `configuration.contents`.
        page_no: The page's `No`, as a string.
        info: The page's `Page` object of the pack.
        keys: `Pack.keys`.

    Returns:
        What `descramble()` wants: `pattern`, `seeds` and `block`; an
        empty dict for a page without `NS`, `PS`, `RS` and block sizes.
    """
    wanted = ("BlockWidth", "BlockHeight", "NS", "PS", "RS")
    if not all(isinstance(info.get(name), int) for name in wanted):
        return {}
    total = 0x2F + sum(ord(char) for char in file) + sum(ord(char) for char in page_no)
    total += sum(sum(key) for key in keys)
    spread = total & _BYTE
    spread |= spread << 8
    spread |= spread << 16

    def digest(key: bytes) -> int:
        value = 0
        for i in range(0, min(len(key) & -4, _KEY_LEN), 4):
            value ^= (key[i] << 24) | (key[i + 1] << 16) | (key[i + 2] << 8) | key[i + 3]
        return value

    return {
        "pattern": total % PATTERN_COUNT,
        "seeds": [
            (spread ^ digest(keys[0]) ^ int(info["NS"])) & _MASK32,
            (spread ^ digest(keys[1]) ^ int(info["PS"])) & _MASK32,
            (spread ^ digest(keys[2]) ^ int(info["RS"])) & _MASK32,
        ],
        "block": [int(info["BlockWidth"]), int(info["BlockHeight"])],
    }


def descramble(image: Image.Image, pattern: int, seeds: tuple[int, int, int], block: tuple[int, int]) -> Image.Image:
    """Put a scrambled page back together.

    Args:
        image: The page as the CDN serves it.
        pattern: `pattern` of `page_seeds()`.
        seeds: `seeds` of `page_seeds()`.
        block: `block` of `page_seeds()`, the tile width and height.

    Returns:
        A new image of the same size.
    """
    out = Image.new(image.mode, image.size)
    for piece in tile_slices(image.width, image.height, block[0], block[1], pattern=pattern, seeds=seeds):
        tile = image.crop((piece.src_x, piece.src_y, piece.src_x + piece.width, piece.src_y + piece.height))
        out.paste(tile, (piece.dst_x, piece.dst_y))
    return out


def pages(pack: Pack, content_url: str) -> list[Page]:
    """The page images of a pack, in reading order.

    Args:
        pack: The pack, unwrapped.
        content_url: The content directory the pack came from, which the page files sit under.

    Returns:
        One `Page` per page, its `extra` holding what `descramble()` wants when it is scrambled.
    """
    configuration = pack.content["configuration"]
    hashed = isinstance(configuration.get("file-name-version"), str)
    mask = pack.name_mask
    out: list[Page] = []
    for entry in configuration.get("contents") or []:
        file = str(entry.get("file") or "")
        described = pack.content.get(file)
        if not file or not isinstance(described, dict) or described.get("Linear") == 0:
            continue
        link_info = described.get("FileLinkInfo") or {}
        for link in link_info.get("PageLinkInfoList") or []:
            info = link.get("Page") if isinstance(link, dict) else None
            if not isinstance(info, dict):
                continue
            page_no = str(info.get("No", 0))
            name = hashed_page_name(file, page_no, mask) if hashed else page_no
            size = info.get("Size") or {}
            out.append(
                Page(
                    url=urljoin(content_url, f"{file}/{name}.jpeg"),
                    width=int(size.get("Width") or 0),
                    height=int(size.get("Height") or 0),
                    extra=page_seeds(file, page_no, info, pack.keys),
                )
            )
    return out
