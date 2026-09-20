"""Voyager's SpeedBinb, the reader behind a good number of Japanese publishers' sites.

Its protocol is the same everywhere; what is specific to a site is the page
around it -- the element that names the API endpoint and the content id,
the episode listing, the titles -- which each extractor reads for itself
before handing over to `content_info()`, `page_list()` and `fetch_page()`.

The viewer's dance, as read out of `speedbinb.js`:

1. GET `bibGetCntntInfo` with the content id, a client-made key `k` and a
   timestamp. The answer carries the contents server and four tables encrypted
   with `cid:k`. `k` is a random string interleaved with a checksum of the
   content id, and the server hands out *decoy* tables unless that checksum
   is right -- so `viewer_key()` builds it exactly the way the viewer does.
2. Fetch the page list from the contents server. Where it is depends on the
   `ServerType` of the answer: `content.js`, a JSONP blob, when the content is
   static files (`SERVER_TYPE_DIRECT`); `content`, plain JSON, behind the Rest
   API (`SERVER_TYPE_REST`); `sbcGetCntnt.php` with the content id and a token
   on the `sbc` backend (`SERVER_TYPE_SBC`). Its `ttx` field is the page list
   as `<t-img>` tags.
3. Fetch each page -- `<src>/M_H.jpg`, `img/<src>` or `sbcGetImg.php?src=` --
   and put its tiles back: the page is cut into an `n x m` grid, every tile is
   padded on all sides and the tiles are shuffled with a permutation derived
   from the two tables the file name picks.

The reader also comes as a static export, "PtBinb", where the page HTML lists
one `<div data-ptimg="data/NNNN.ptimg.json">` per page and each of those JSON
files names a scrambled JPEG next to it plus the rectangles to copy out of it;
`parse_ptimg()`, `descramble_ptimg()` and `fetch_ptimg_page()` read that form.
"""

from __future__ import annotations

import json
import math
import re
import secrets
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode, urljoin

from PIL import Image

from getjmanga.errors import GetjmangaError, NotAnEpisodePageError
from getjmanga.extractor import Page

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    from getjmanga.extractor import Extractor

#: `ServerType` of a `bibGetCntntInfo` item: pages through `sbcGetImg.php`,
#: static files on the contents server, or the Rest API's `img/<src>`.
SERVER_TYPE_SBC = 0
SERVER_TYPE_DIRECT = 1
SERVER_TYPE_REST = 2
SERVER_TYPES = frozenset({SERVER_TYPE_SBC, SERVER_TYPE_DIRECT, SERVER_TYPE_REST})

#: The fields of a `bibGetCntntInfo` item that hold the encrypted tables, left out of metadata.
TABLE_FIELDS = frozenset({"stbl", "ttbl", "ctbl", "ptbl"})

#: The alphabet SpeedBinb's key and its scramble tables are written in.
_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
_KEY_NONCE_LENGTH = 16

#: Feedback taps of the shift register the scramble tables are encrypted with.
_LFSR_TAPS = 0x48200004

_JSONP = re.compile(r"^\s*[A-Za-z0-9_-]+\((?P<body>.*)\)\s*;?\s*$", re.DOTALL)
_T_CASE = re.compile(r"<t-case\b[^>]*>(?P<body>.*?)</t-case>", re.DOTALL | re.IGNORECASE)
_T_IMG = re.compile(r"<t-img\b(?P<attrs>[^>]*)>", re.IGNORECASE)
_ATTR = re.compile(r'(?P<key>[\w.-]+)\s*=\s*"(?P<value>[^"]*)"')

# A scramble table: `=<columns>-<rows>+<padding>-<indices>` for the page side (`ctbl`)
# and `=<columns>-<rows>-<padding>-<indices>` for the served side (`ptbl`).
_TABLE = re.compile(r"^=(?P<columns>\d+)-(?P<rows>\d+)(?P<sign>[-+])(?P<padding>\d+)-(?P<indices>[-_0-9A-Za-z]+)$")
_MAX_SIDE = 8
_MAX_TILES = 64
_MIN_SIDE = 64
_MIN_AREA_SIDE = 320


@dataclass(frozen=True)
class Tile:
    """One tile to copy from the served image into the page."""

    xsrc: int
    ysrc: int
    width: int
    height: int
    xdest: int
    ydest: int


# ---------------------------------------------------------------------------
# The key and the table cipher
# ---------------------------------------------------------------------------


def viewer_key(content_id: str, nonce: str | None = None) -> str:
    """Build the `k` the viewer sends to `bibGetCntntInfo`.

    Sixteen random characters, each followed by one character of a running
    checksum over the nonce and the content id. The server only hands the
    real scramble tables to a key carrying that checksum.

    Args:
        content_id: The `data-ptbinb-cid` of the episode.
        nonce: The random half of the key, for reproducible tests.

    Returns:
        The 32-character key.
    """
    if nonce is None:
        nonce = "".join(secrets.choice(_ALPHABET) for _ in range(_KEY_NONCE_LENGTH))
    repeated = content_id * math.ceil(_KEY_NONCE_LENGTH / len(content_id))
    head, tail = repeated[:_KEY_NONCE_LENGTH], repeated[-_KEY_NONCE_LENGTH:]
    key = []
    checksum = [0, 0, 0]
    for index, char in enumerate(nonce):
        checksum[0] ^= ord(char)
        checksum[1] ^= ord(head[index])
        checksum[2] ^= ord(tail[index])
        key.append(char + _ALPHABET[sum(checksum) & (len(_ALPHABET) - 1)])
    return "".join(key)


def decode_table(content_id: str, key: str, encoded: str) -> Any:  # noqa: ANN401 (whatever JSON the server put in)
    """Decrypt one of the `stbl`/`ttbl`/`ctbl`/`ptbl` fields of `bibGetCntntInfo`.

    A linear-feedback shift register seeded from `cid:k` shifts every printable
    character of the field; the plaintext is JSON.

    Args:
        content_id: The content id the field was fetched for.
        key: The `k` sent along with the request.
        encoded: The field as the API returned it.

    Returns:
        The decoded JSON value -- a list of table strings for `ctbl` and `ptbl`.

    Raises:
        GetjmangaError: The field does not decode to JSON, so the key is wrong.
    """
    seed = 0
    for index, char in enumerate(f"{content_id}:{key}"):
        seed += ord(char) << (index % 16)
    seed &= 0x7FFFFFFF
    state = seed or 0x12345678

    plain = []
    for char in encoded:
        state = ((state >> 1) ^ (_LFSR_TAPS if state & 1 else 0)) & 0xFFFFFFFF
        plain.append(chr((ord(char) - 32 + state) % 94 + 32))
    try:
        return json.loads("".join(plain))
    except ValueError as error:
        msg = "the scramble table does not decode; the viewer key was rejected."
        raise GetjmangaError(msg) from error


def pick_tables(src: str, ctbl: list[str], ptbl: list[str]) -> tuple[str, str]:
    """Choose the table pair a page image was scrambled with.

    The file name's character codes, summed at even and odd positions, index
    the two lists.

    Args:
        src: The page's `src` as `content.js` lists it, e.g. `pages/abc.jpg`.
        ctbl: The decoded `ctbl` list.
        ptbl: The decoded `ptbl` list.

    Returns:
        The page-side (`ctbl`) and served-side (`ptbl`) table strings; two
        empty strings when the lists are empty, meaning nothing is scrambled.
    """
    if not ctbl or not ptbl:
        return "", ""
    name = src.rsplit("/", 1)[-1]
    sums = [0, 0]
    for index, char in enumerate(name):
        sums[index % 2] += ord(char)
    return ctbl[sums[1] % len(ctbl)], ptbl[sums[0] % len(ptbl)]


# ---------------------------------------------------------------------------
# Descrambling
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Scramble:
    """The grid and the permutation a pair of tables describes.

    Every row of the grid has one tile narrower than the rest and every
    column one tile shorter, and the served image and the page each place
    those differently -- hence the four "short" lists.
    """

    columns: int
    rows: int
    padding: int
    #: Per tile of the served image (row-major), the tile of the page it is.
    order: tuple[int, ...]
    #: Per row of the served image, the column holding the narrow tile.
    served_narrow_column: tuple[int, ...]
    #: Per column of the served image, the row holding the short tile.
    served_short_row: tuple[int, ...]
    #: Per row of the page, the column holding the narrow tile.
    page_narrow_column: tuple[int, ...]
    #: Per column of the page, the row holding the short tile.
    page_short_row: tuple[int, ...]

    def applies(self, width: int, height: int) -> bool:
        """Whether an image this big is scrambled at all; small ones are served as-is."""
        pad_x, pad_y = 2 * self.columns * self.padding, 2 * self.rows * self.padding
        return (
            width >= _MIN_SIDE + pad_x
            and height >= _MIN_SIDE + pad_y
            and width * height >= (_MIN_AREA_SIDE + pad_x) * (_MIN_AREA_SIDE + pad_y)
        )

    def page_size(self, width: int, height: int) -> tuple[int, int]:
        """The page's size once the tile padding is gone."""
        if not self.applies(width, height):
            return width, height
        return width - 2 * self.columns * self.padding, height - 2 * self.rows * self.padding

    def transfers(self, width: int, height: int) -> list[Tile]:
        """The tiles to copy, for a served image of `width` x `height`."""
        if not self.applies(width, height):
            return [Tile(0, 0, width, height, 0, 0)]
        page_width, page_height = self.page_size(width, height)
        tile_width = math.ceil(page_width / self.columns)
        narrow_width = page_width - (self.columns - 1) * tile_width
        tile_height = math.ceil(page_height / self.rows)
        short_height = page_height - (self.rows - 1) * tile_height

        transfers = []
        for served_index, page_index in enumerate(self.order):
            column, row = served_index % self.columns, served_index // self.columns
            xsrc = self.padding + column * (tile_width + 2 * self.padding)
            if self.served_narrow_column[row] < column:
                xsrc += narrow_width - tile_width
            ysrc = self.padding + row * (tile_height + 2 * self.padding)
            if self.served_short_row[column] < row:
                ysrc += short_height - tile_height

            page_column, page_row = page_index % self.columns, page_index // self.columns
            xdest = page_column * tile_width
            if self.page_narrow_column[page_row] < page_column:
                xdest += narrow_width - tile_width
            ydest = page_row * tile_height
            if self.page_short_row[page_column] < page_row:
                ydest += short_height - tile_height

            transfers.append(
                Tile(
                    xsrc,
                    ysrc,
                    narrow_width if self.served_narrow_column[row] == column else tile_width,
                    short_height if self.served_short_row[column] == row else tile_height,
                    xdest,
                    ydest,
                ),
            )
        return transfers


def parse_scramble(ctbl: str, ptbl: str) -> Scramble | None:
    """Read a table pair into a `Scramble`.

    Each table is `=<columns>-<rows><sign><padding>-<indices>`: `ctbl` (sign
    `+`) describes the page, `ptbl` (sign `-`) the served image, and the
    indices are one character per column, per row and per tile.

    Args:
        ctbl: The page-side table, `=8-8+4-...`.
        ptbl: The served-side table, `=8-8-4-...`.

    Returns:
        The scramble, or None when both tables are empty (an unscrambled page).

    Raises:
        GetjmangaError: The tables are not a matching pair in that format. The
            digit-only format (`8-8-...`) is what the server hands a client
            whose key it rejected, and is not read here.
    """
    if not ctbl and not ptbl:
        return None
    page, served = _TABLE.match(ctbl), _TABLE.match(ptbl)
    if (
        page is None
        or served is None
        or page["sign"] != "+"
        or served["sign"] != "-"
        or (page["columns"], page["rows"], page["padding"]) != (served["columns"], served["rows"], served["padding"])
    ):
        msg = f"unsupported scramble tables {ctbl!r} / {ptbl!r}."
        raise GetjmangaError(msg)

    columns, rows, padding = int(page["columns"]), int(page["rows"]), int(page["padding"])
    if columns > _MAX_SIDE or rows > _MAX_SIDE or columns * rows > _MAX_TILES:
        msg = f"scramble grid {columns}x{rows} is larger than the viewer allows."
        raise GetjmangaError(msg)
    expected = columns + rows + columns * rows
    if len(page["indices"]) != expected or len(served["indices"]) != expected:
        msg = f"scramble tables {ctbl!r} / {ptbl!r} do not describe {columns}x{rows} tiles."
        raise GetjmangaError(msg)

    page_short_row, page_narrow_column, page_order = _split_indices(page["indices"], columns, rows)
    served_short_row, served_narrow_column, served_order = _split_indices(served["indices"], columns, rows)
    return Scramble(
        columns=columns,
        rows=rows,
        padding=padding,
        order=tuple(page_order[served_order[index]] for index in range(columns * rows)),
        served_narrow_column=tuple(served_narrow_column),
        served_short_row=tuple(served_short_row),
        page_narrow_column=tuple(page_narrow_column),
        page_short_row=tuple(page_short_row),
    )


def _split_indices(indices: str, columns: int, rows: int) -> tuple[list[int], list[int], list[int]]:
    """Split a table's index string into its per-column, per-row and per-tile parts."""
    values = [_ALPHABET.index(char) for char in indices]
    return values[:columns], values[columns : columns + rows], values[columns + rows :]


def descramble(image: Image.Image, ctbl: str, ptbl: str) -> Image.Image:
    """Put a served page back together.

    Args:
        image: The page exactly as the contents server serves it.
        ctbl: The page-side table `pick_tables` chose for it.
        ptbl: The served-side table `pick_tables` chose for it.

    Returns:
        A new image of the page's real size, tiles in reading order. The image
        itself when the tables say it is not scrambled.
    """
    scramble = parse_scramble(ctbl, ptbl)
    if scramble is None:
        return image
    out = Image.new(image.mode, scramble.page_size(*image.size))
    for tile in scramble.transfers(*image.size):
        out.paste(
            image.crop((tile.xsrc, tile.ysrc, tile.xsrc + tile.width, tile.ysrc + tile.height)),
            (tile.xdest, tile.ydest),
        )
    return out


# ---------------------------------------------------------------------------
# content.js
# ---------------------------------------------------------------------------


def parse_content(text: str) -> dict[str, Any]:
    """Unwrap the JSONP of `content.js`.

    Args:
        text: The body of `content.js`, `DataGet_Content({...})`.

    Returns:
        The JSON object inside.

    Raises:
        GetjmangaError: The body is not the viewer's JSONP, or says it failed.
    """
    match = _JSONP.match(text)
    try:
        body = json.loads(match["body"] if match else text)
    except ValueError as error:
        msg = "content.js is not the viewer's JSONP."
        raise GetjmangaError(msg) from error
    if not isinstance(body, dict) or body.get("result") != 1 or not isinstance(body.get("ttx"), str):
        msg = f"content.js did not describe the pages: {str(body)[:200]}"
        raise GetjmangaError(msg)
    return body


def parse_pages(ttx: str) -> Iterator[dict[str, str]]:
    """List the `<t-img>` tags of a `ttx` document, in reading order.

    The document holds the pages twice -- once as single pages in a
    `<t-case>`, once as spreads in a `<t-nocase>` -- so only the first
    `<t-case>` is read when there is one.

    Args:
        ttx: The `ttx` field of `content.js`.

    Yields:
        The attributes of each `<t-img>` (`src`, `orgwidth`, `orgheight`, `id`).
    """
    case = _T_CASE.search(ttx)
    for match in _T_IMG.finditer(case["body"] if case else ttx):
        attrs = {attr["key"].lower(): attr["value"] for attr in _ATTR.finditer(match["attrs"])}
        if attrs.get("src"):
            yield attrs


# ---------------------------------------------------------------------------
# The reader's API
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Content:
    """What `bibGetCntntInfo` said about one content, its tables decoded."""

    #: The `cid` the content was asked for by.
    content_id: str
    #: The `k` it was asked for with; the tables only decode with it.
    key: str
    #: The item as the API returned it, tables and all.
    item: dict[str, Any]
    #: `ContentsServer`, absolute, without a trailing slash.
    server: str
    server_type: int
    ctbl: list[str]
    ptbl: list[str]

    @property
    def info(self) -> dict[str, Any]:
        """The item without its encrypted tables, for `Episode.metadata`."""
        return {name: value for name, value in self.item.items() if name not in TABLE_FIELDS}

    @property
    def token(self) -> str:
        """`p`, the token the `sbc` backend wants on every request."""
        return str(self.item.get("p") or "")

    @property
    def view_mode(self) -> str:
        """`ViewMode`, which the `sbc` backend wants as `vm`."""
        return str(self.item.get("ViewMode") or 1)

    @property
    def content_date(self) -> str:
        """`ContentDate`, which the viewer sends as `dmytime` in place of the clock when it has one."""
        return str(self.item.get("ContentDate") or "")


@dataclass(frozen=True)
class Book:
    """The page list of a content."""

    pages: tuple[Page, ...]
    #: The page list as the contents server returned it, `ttx` and all.
    body: dict[str, Any]


def now_ms() -> int:
    """The cache-busting timestamp the viewer sends as `dmytime`."""
    return int(time.time() * 1000)


def content_info(
    extractor: Extractor,
    info_url: str,
    content_id: str,
    *,
    referer: str,
    params: Mapping[str, str] | None = None,
    server_types: frozenset[int] = SERVER_TYPES,
) -> Content | None:
    """Call `bibGetCntntInfo` the way the viewer does and decode what it says.

    Args:
        extractor: Whose session and headers to use.
        info_url: The endpoint, off the reader's `data-ptbinb`.
        content_id: The content id, off the reader's `data-ptbinb-cid` or its URL.
        referer: The reader URL, sent as the Referer.
        params: Query parameters the reader carries and the viewer forwards.
        server_types: The `ServerType` values the caller can serve pages from.

    Returns:
        The content, or None when the API refused it (`result` other than 1):
        a purchase, a wait or a login is wanted.

    Raises:
        NotAnEpisodePageError: The answer does not describe a content at all.
        GetjmangaError: The content is on a backend not in `server_types`, or
            its tables do not decode.
    """
    key = viewer_key(content_id)
    res = extractor._get(
        info_url,
        params={"cid": content_id, "k": key, "dmytime": now_ms(), **(params or {})},
        headers={**extractor.HEADERS, "Referer": referer},
    )
    body = res.json()
    if not isinstance(body, dict):
        msg = f"{info_url} did not describe {content_id}: {str(body)[:200]}"
        raise NotAnEpisodePageError(msg)
    items = body.get("items")
    if body.get("result") != 1 or not items:
        return None
    item = items[0]
    if not isinstance(item, dict) or not item.get("ContentsServer"):
        msg = f"{info_url} did not describe {content_id}: {str(body)[:200]}"
        raise NotAnEpisodePageError(msg)
    server_type = int(item.get("ServerType", 0))
    if server_type not in server_types:
        msg = f"{content_id} is on a SpeedBinb ServerType {item.get('ServerType')} backend, which is not supported."
        raise GetjmangaError(msg)
    ctbl = decode_table(content_id, key, str(item.get("ctbl", "")))
    ptbl = decode_table(content_id, key, str(item.get("ptbl", "")))
    if not isinstance(ctbl, list) or not isinstance(ptbl, list):
        msg = f"{info_url} carried no scramble tables for {content_id}."
        raise GetjmangaError(msg)
    return Content(
        content_id=content_id,
        key=key,
        item=item,
        server=urljoin(info_url, str(item["ContentsServer"])).rstrip("/"),
        server_type=server_type,
        ctbl=ctbl,
        ptbl=ptbl,
    )


def page_list(
    extractor: Extractor,
    content: Content,
    *,
    referer: str,
    params: Mapping[str, str] | None = None,
) -> Book:
    """Fetch a content's page list and name every page image.

    Args:
        extractor: Whose session and headers to use.
        content: What `content_info()` returned.
        referer: The reader URL, sent as the Referer.
        params: Query parameters the reader carries and the viewer forwards.

    Returns:
        The pages in reading order, each carrying the tables it was scrambled
        with, and the page list as returned.

    Raises:
        GetjmangaError: The contents server did not answer with a page list.
    """
    extra = dict(params or {})
    stamp = content.content_date
    if content.server_type == SERVER_TYPE_DIRECT:
        list_url, query = f"{content.server}/content.js", {"dmytime": stamp or now_ms(), **extra}
    elif content.server_type == SERVER_TYPE_REST:
        list_url, query = f"{content.server}/content", {**({"dmytime": stamp} if stamp else {}), **extra}
    else:
        list_url = f"{content.server}/sbcGetCntnt.php"
        query = {"cid": content.content_id, "p": content.token, "vm": content.view_mode, "dmytime": stamp or now_ms()}
        query.update(extra)
    res = extractor._get(list_url, params=query or None, headers={**extractor.HEADERS, "Referer": referer})
    body = parse_content(res.text)
    file_name = "M.jpg" if body.get("ImageClass") == "singlequality" else "M_H.jpg"
    # The image URLs carry the stamp when there is one, and the forwarded parameters off the static backend.
    stamped = {"dmytime": stamp} if stamp else {}
    pages = []
    for attrs in parse_pages(body["ttx"]):
        src = attrs["src"]
        if content.server_type == SERVER_TYPE_DIRECT:
            url = f"{content.server}/{src}/{file_name}" + (f"?{urlencode(stamped)}" if stamped else "")
        elif content.server_type == SERVER_TYPE_REST:
            image_query = {**stamped, **extra}
            url = f"{content.server}/img/{src}" + (f"?{urlencode(image_query)}" if image_query else "")
        else:
            image_query = {"cid": content.content_id, "src": src, "p": content.token, "q": "0", "vm": content.view_mode}
            url = f"{content.server}/sbcGetImg.php?{urlencode({**image_query, **stamped, **extra})}"
        page_table, served_table = pick_tables(src, content.ctbl, content.ptbl)
        pages.append(
            Page(
                url=url,
                width=int(attrs.get("orgwidth") or 0),
                height=int(attrs.get("orgheight") or 0),
                extra={"ctbl": page_table, "ptbl": served_table},
            ),
        )
    return Book(pages=tuple(pages), body=body)


def fetch_page(extractor: Extractor, page: Page, *, referer: str) -> Image.Image:
    """Fetch one page `page_list()` named and put its tiles back where they belong.

    Args:
        extractor: Whose session and headers to use.
        page: The page to fetch.
        referer: What to send as the Referer.

    Returns:
        The page in reading order, padding gone.
    """
    image = extractor._fetch_image(page.url, headers={**extractor.HEADERS, "Referer": referer})
    return descramble(image, str(page.extra.get("ctbl", "")), str(page.extra.get("ptbl", "")))


# ---------------------------------------------------------------------------
# The static export ("PtBinb")
# ---------------------------------------------------------------------------

# The reader's `<title>` is `"<series>　<episode>"`, the two joined by an
# ideographic space -- or, on a hand-edited page, by a run of plain ones.
_TITLE_SEPARATOR = re.compile(r"　|[ \t]{2,}")

# Whatever separates the series from the episode once the series is known:
# whitespace, or a colon, slash, bar or dash in either width.
_TITLE_GAP = re.compile(r"^[\s　:/|\-\uff1a\uff0f\uff5c\uff0d]+")

# One entry of a ptimg `coords` list: `"<resource>:<x>,<y>+<w>,<h>><dx>,<dy>"`,
# a `w` x `h` rectangle at (`x`, `y`) of the resource, to land at (`dx`, `dy`).
_COORD = re.compile(r"^(?P<res>[^:]+):(?P<x>\d+),(?P<y>\d+)\+(?P<w>\d+),(?P<h>\d+)>(?P<dx>\d+),(?P<dy>\d+)$")


@dataclass(frozen=True)
class Transfer:
    """One rectangle to copy out of a scrambled resource into the page."""

    resource: str
    x: int
    y: int
    width: int
    height: int
    dest_x: int
    dest_y: int


@dataclass(frozen=True)
class Ptimg:
    """What one `NNNN.ptimg.json` says: the resources to fetch and how to lay them out."""

    #: Resource key -> absolute image URL.
    resources: dict[str, str]
    width: int
    height: int
    transfers: tuple[Transfer, ...]


def parse_ptimg(data: Mapping[str, Any], url: str) -> Ptimg:
    """Read a page's `ptimg.json`.

    Args:
        data: The decoded JSON.
        url: Where the JSON came from; its resources are named relative to it.

    Returns:
        The resources to fetch and the transfers to apply, as the reader would.

    Raises:
        GetjmangaError: The JSON is not a version-1 ptimg the reader would accept.
    """
    if data.get("ptimg-version") != 1:
        msg = f"{url} is not a ptimg-version 1 file: {data.get('ptimg-version')!r}."
        raise GetjmangaError(msg)
    resources = {
        str(key): urljoin(url, str(resource["src"]))
        for key, resource in (data.get("resources") or {}).items()
        if isinstance(resource, dict) and resource.get("src")
    }
    views = data.get("views") or []
    if not resources or not views:
        msg = f"{url} names no resources or no views."
        raise GetjmangaError(msg)
    view = views[0]
    transfers: list[Transfer] = []
    for coord in view.get("coords") or []:
        match = _COORD.match(str(coord))
        if match is None or match["res"] not in resources:
            msg = f"{url} holds an unreadable transfer: {coord!r}."
            raise GetjmangaError(msg)
        transfers.append(
            Transfer(
                resource=match["res"],
                x=int(match["x"]),
                y=int(match["y"]),
                width=int(match["w"]),
                height=int(match["h"]),
                dest_x=int(match["dx"]),
                dest_y=int(match["dy"]),
            ),
        )
    if not transfers:
        msg = f"{url} lists no transfers."
        raise GetjmangaError(msg)
    return Ptimg(resources=resources, width=int(view["width"]), height=int(view["height"]), transfers=tuple(transfers))


def descramble_ptimg(ptimg: Ptimg, images: Mapping[str, Image.Image]) -> Image.Image:
    """Put a page back together from its scrambled resources.

    The reader draws onto a blank canvas of the view's size and copies every
    transfer's rectangle across; the resources are larger than the page,
    since the tiles sit in them with a gutter in between.

    Args:
        ptimg: The parsed `ptimg.json`.
        images: The decoded resource images, by resource key.

    Returns:
        A new image, the page in reading order.
    """
    mode = next(iter(images.values())).mode
    out = Image.new(mode if mode in ("L", "RGB") else "RGB", (ptimg.width, ptimg.height), "white")
    for transfer in ptimg.transfers:
        source = images[transfer.resource]
        tile = source.crop(
            (
                transfer.x,
                transfer.y,
                transfer.x + transfer.width,
                transfer.y + transfer.height,
            ),
        )
        out.paste(tile, (transfer.dest_x, transfer.dest_y))
    return out


def split_title(title: str, series: str = "") -> tuple[str, str]:
    """Split the reader's `<title>` into the series and the episode.

    Args:
        title: The `<title>` text, `"<series>　<episode>"`.
        series: The series title, when the work page said what it is; the
            episode is then whatever follows it, however the two are joined.

    Returns:
        The two halves. With no separator to split on, the episode is the
        whole title, and so is the series unless one was given.
    """
    title = title.strip()
    rest = title.removeprefix(series) if series else title
    if series and rest != title and (not rest or _TITLE_GAP.match(rest)):
        return series, _TITLE_GAP.sub("", rest) or title
    parts = _TITLE_SEPARATOR.split(title, maxsplit=1)
    head = parts[0].strip()
    episode = parts[1].strip() if len(parts) > 1 else ""
    if not episode:
        return series or title, title
    return series or head, episode


def fetch_ptimg_page(extractor: Extractor, ptimg_url: str, *, referer: str) -> Image.Image:
    """Fetch one page of a static export: its `ptimg.json`, then the resources it names, put back together.

    Args:
        extractor: Whose session and headers to use.
        ptimg_url: The page's `ptimg.json`.
        referer: What to send as the Referer.

    Returns:
        The page in reading order.
    """
    headers = {**extractor.HEADERS, "Referer": referer}
    ptimg = parse_ptimg(extractor._get(ptimg_url, headers=headers).json(), ptimg_url)
    images = {key: extractor._fetch_image(src, headers=headers) for key, src in ptimg.resources.items()}
    return descramble_ptimg(ptimg, images)
