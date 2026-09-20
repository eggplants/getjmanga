"""Gaugau Monster Plus (がうがうモンスター, Futabasha), and the SpeedBinb viewer it serves episodes through.

SpeedBinb (Voyager Japan) is the viewer behind a good number of Japanese
publishers' sites. Its protocol is the same everywhere, and what is specific to
this site is the page around it: the `#content[data-ptbinb]` element that names
the API endpoint and the content id, the episode listing and the titles.

The viewer's dance, as read out of `speedbinb.js`:

1. GET `bibGetCntntInfo` with the content id, a client-made key `k` and a
   timestamp. The answer carries the contents server and four tables encrypted
   with `cid:k`. `k` is a random string interleaved with a checksum of the
   content id, and the server hands out *decoy* tables unless that checksum
   is right -- so `viewer_key` builds it exactly the way the viewer does.
2. GET `<ContentsServer>/content.js`, a JSONP blob whose `ttx` field is the
   page list as `<t-img>` tags.
3. GET `<ContentsServer>/<src>/M_H.jpg` per page and put its tiles back: the
   page is cut into an `n x m` grid, every tile is padded on all sides and the
   tiles are shuffled with a permutation derived from the two tables the file
   name picks.
"""

from __future__ import annotations

import json
import math
import re
import secrets
import time
from dataclasses import dataclass
from http import HTTPStatus
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from bs4.element import Tag
from PIL import Image

from getjmanga.errors import GetjmangaError, NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractor import Episode, Extractor, Page

if TYPE_CHECKING:
    from collections.abc import Iterator

    from requests import Session

BASE_URL = "https://gaugau.futabanet.jp"

#: The alphabet SpeedBinb's key and its scramble tables are written in.
_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
_KEY_NONCE_LENGTH = 16

#: Feedback taps of the shift register the scramble tables are encrypted with.
_LFSR_TAPS = 0x48200004

#: `ServerType` of a `bibGetCntntInfo` item whose content is static files.
_SERVER_TYPE_DIRECT = 1

#: Element the site mounts the viewer on. `data-ptbinb` names the API endpoint.
_VIEWER_ID = "content"

_WORK_PATH = re.compile(r"^/list/work/(?P<id>[^/]+)(?:/(?P<kind>episodes|comics))?/?$")
_EPISODE_PATH = re.compile(r"^/list/work/(?P<id>[^/]+)/episodes/(?P<order>\d+)/?$")
_READER_PATH = re.compile(r"^/list/work/(?P<id>[^/]+)/reader/comics/(?P<cid>[^/]+)/?$")

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
class Transfer:
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

    def transfers(self, width: int, height: int) -> list[Transfer]:
        """The tiles to copy, for a served image of `width` x `height`."""
        if not self.applies(width, height):
            return [Transfer(0, 0, width, height, 0, 0)]
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
                Transfer(
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


class Gaugau(Extractor):
    """Fetch episodes from Gaugau Monster Plus (gaugau.futabanet.jp)."""

    NAME = "gaugau"
    HOSTS = ("gaugau.futabanet.jp",)
    URL_FORMS = (
        "https://gaugau.futabanet.jp/list/work/<work-id>/episodes/<n>",
        "https://gaugau.futabanet.jp/list/work/<work-id>/reader/comics/<content-id>",
        "https://gaugau.futabanet.jp/list/work/<work-id>",
        "https://gaugau.futabanet.jp/list/work/<work-id>/episodes",
        "https://gaugau.futabanet.jp/list/work/<work-id>/comics",
    )

    def __init__(self, session: Session | None = None) -> None:
        """Build an extractor.

        Args:
            session: A session to reuse. A retrying one is made when omitted.
        """
        super().__init__(session)
        self._listings: dict[tuple[str, str], list[str]] = {}

    @classmethod
    def suitable(cls, url: str) -> bool:
        """Report whether `url` is an episode, a volume trial or a work page.

        Args:
            url: The URL to check.

        Returns:
            True for the URL shapes in `URL_FORMS`.
        """
        if not super().suitable(url):
            return False
        path = urlparse(url).path
        return bool(_EPISODE_PATH.match(path) or _READER_PATH.match(path) or _WORK_PATH.match(path))

    @classmethod
    def is_series(cls, url: str) -> bool:
        """Report whether `url` is a work page or one of its listings.

        Args:
            url: The URL to check.

        Returns:
            True for `/list/work/<id>`, `/list/work/<id>/episodes` and `/list/work/<id>/comics`.
        """
        return cls.suitable(url) and _WORK_PATH.match(urlparse(url).path) is not None

    def series_urls(self, url: str) -> list[str]:
        """List a work's episodes, oldest first, or its volume trials with `/comics`.

        Args:
            url: A work URL, with or without `/episodes` or `/comics`.

        Returns:
            One URL per listed episode (or volume trial), in reading order.

        Raises:
            UnsupportedUrlError: The URL is not a work page.
            NotAnEpisodePageError: The listing is empty.
        """
        match = _WORK_PATH.match(urlparse(url).path)
        if match is None:
            msg = f"{url} is not a work page."
            raise UnsupportedUrlError(msg)
        kind = match["kind"] or "episodes"
        urls = self.listing(match["id"], kind)
        if not urls:
            msg = f"the work at {url} lists no {kind}."
            raise NotAnEpisodePageError(msg)
        return urls

    def episode(self, url: str) -> Episode:
        """Read one episode: its titles, its pages and the episode after it.

        Args:
            url: An episode or volume trial URL.

        Returns:
            The episode. `pages` is empty when the free run of the episode is
            over and the site sends readers to its app instead.

        Raises:
            UnsupportedUrlError: The URL is not an episode URL.
            NotAnEpisodePageError: The page is a 404 or has no episode on it.
        """
        parsed = urlparse(url)
        match = _EPISODE_PATH.match(parsed.path) or _READER_PATH.match(parsed.path)
        if match is None:
            msg = f"{url} is not an episode page."
            raise UnsupportedUrlError(msg)
        work_id = match["id"]
        kind = "episodes" if "order" in match.groupdict() else "comics"

        res = self._session.get(url, headers=self.HEADERS, timeout=self.TIMEOUT)
        if res.status_code == HTTPStatus.NOT_FOUND:
            msg = f"{url} is not there (404)."
            raise NotAnEpisodePageError(msg)
        res.raise_for_status()
        soup = BeautifulSoup(res.content, "html.parser")

        series_title = _series_title(soup, work_id)
        episode_title = _episode_title(soup)
        viewer = soup.select_one(f"#{_VIEWER_ID}[data-ptbinb][data-ptbinb-cid]")
        next_url = self._next_url(url, work_id, kind)

        if not isinstance(viewer, Tag):
            if not episode_title:
                msg = f"no viewer and no episode heading on {url}."
                raise NotAnEpisodePageError(msg)
            return Episode(
                url=url,
                series_title=series_title,
                episode_title=episode_title,
                next_url=next_url,
                metadata={"work_id": work_id, "locked": True},
            )

        content_id = str(viewer.attrs["data-ptbinb-cid"])
        info_url = urljoin(url, str(viewer.attrs["data-ptbinb"]))
        key = viewer_key(content_id)
        item = self._content_info(info_url, content_id, key, referer=url)
        server = str(item["ContentsServer"]).rstrip("/")
        ctbl = decode_table(content_id, key, str(item.get("ctbl", "")))
        ptbl = decode_table(content_id, key, str(item.get("ptbl", "")))
        if not isinstance(ctbl, list) or not isinstance(ptbl, list):
            msg = f"{info_url} carried no scramble tables for {content_id}."
            raise GetjmangaError(msg)

        content_res = self._get(
            f"{server}/content.js",
            params={"dmytime": _now_ms()},
            headers={**self.HEADERS, "Referer": url},
        )
        content = parse_content(content_res.text)
        file_name = "M.jpg" if content.get("ImageClass") == "singlequality" else "M_H.jpg"
        pages = []
        for attrs in parse_pages(content["ttx"]):
            page_table, served_table = pick_tables(attrs["src"], ctbl, ptbl)
            pages.append(
                Page(
                    url=f"{server}/{attrs['src']}/{file_name}",
                    width=int(attrs.get("orgwidth") or 0),
                    height=int(attrs.get("orgheight") or 0),
                    extra={"ctbl": page_table, "ptbl": served_table},
                ),
            )

        return Episode(
            url=url,
            series_title=series_title,
            episode_title=episode_title or content_id,
            pages=tuple(pages),
            next_url=next_url,
            metadata={
                "work_id": work_id,
                "content_id": content_id,
                "contents_server": server,
                "view_mode": item.get("ViewMode"),
                "title": item.get("Title"),
            },
        )

    def image(self, page: Page, episode: Episode) -> Image.Image:
        """Fetch one page and put its tiles back where they belong.

        Args:
            page: The page to fetch.
            episode: The episode the page belongs to.

        Returns:
            The page in reading order, padding gone.
        """
        image = self._fetch_image(page.url, headers={**self.HEADERS, "Referer": episode.url})
        return descramble(image, str(page.extra.get("ctbl", "")), str(page.extra.get("ptbl", "")))

    def listing(self, work_id: str, kind: str = "episodes") -> list[str]:
        """List a work's episodes or volume trials, in reading order.

        The site lists episodes newest first and skips numbers, so the list is
        what says which episode follows which. It is fetched once per work.

        Args:
            work_id: The work's id.
            kind: `"episodes"` for the episode list, `"comics"` for the volume list.

        Returns:
            The episode (or volume trial) URLs, oldest first.
        """
        cached = self._listings.get((work_id, kind))
        if cached is not None:
            return cached

        page_url = f"{BASE_URL}/list/work/{work_id}/{kind}"
        soup = BeautifulSoup(self._get(page_url).content, "html.parser")
        pattern = _EPISODE_PATH if kind == "episodes" else _READER_PATH
        urls: list[str] = []
        for anchor in soup.find_all("a", href=True):
            href = urljoin(page_url, str(anchor["href"]).split("?", 1)[0].split("#", 1)[0])
            parsed = urlparse(href)
            match = pattern.match(parsed.path)
            if match and match["id"] == work_id and parsed.hostname in self.HOSTS and href not in urls:
                urls.append(href)
        if kind == "episodes":
            urls.reverse()
        self._listings[work_id, kind] = urls
        return urls

    def _next_url(self, url: str, work_id: str, kind: str) -> str | None:
        """The listing entry after `url`, or None when it is the last (or unlisted)."""
        urls = self.listing(work_id, kind)
        key = url.rstrip("/")
        for index, candidate in enumerate(urls):
            if candidate.rstrip("/") == key:
                return urls[index + 1] if index + 1 < len(urls) else None
        return None

    def _content_info(self, info_url: str, content_id: str, key: str, *, referer: str) -> dict[str, Any]:
        """Call `bibGetCntntInfo` and return its first item."""
        res = self._get(
            info_url,
            params={"cid": content_id, "k": key, "dmytime": _now_ms()},
            headers={**self.HEADERS, "Referer": referer},
        )
        body = res.json()
        items = body.get("items") if isinstance(body, dict) and body.get("result") == 1 else None
        if not items or not isinstance(items[0], dict) or not items[0].get("ContentsServer"):
            msg = f"{info_url} did not describe {content_id}: {str(body)[:200]}"
            raise NotAnEpisodePageError(msg)
        item: dict[str, Any] = items[0]
        if int(item.get("ServerType", 0)) != _SERVER_TYPE_DIRECT:
            msg = f"{content_id} is on a SpeedBinb ServerType {item.get('ServerType')} backend, which is not supported."
            raise GetjmangaError(msg)
        return item


def _now_ms() -> int:
    """The cache-busting timestamp the viewer sends as `dmytime`."""
    return int(time.time() * 1000)


def _series_title(soup: BeautifulSoup, work_id: str) -> str:
    """The work's title, off the breadcrumb, else off the page title, else the id."""
    for anchor in soup.select('ol.breadcrumb a[itemprop="item"]'):
        if _WORK_PATH.match(urlparse(str(anchor.get("href", ""))).path):
            name = anchor.get_text(strip=True)
            if name:
                return name
    og = soup.find("meta", property="og:title")
    heading = str(og.attrs.get("content", "")) if isinstance(og, Tag) else ""
    if not heading and soup.title:
        heading = soup.title.get_text()
    # "公式-<series> <episode> | 作品詳細 | <site>"
    heading = heading.split(" | ", 1)[0].strip().removeprefix("公式-").strip()
    return heading or work_id


def _episode_title(soup: BeautifulSoup) -> str:
    """The episode's heading, `<h1 class="detailHead__title">`, or "" without one."""
    heading = soup.select_one("h1.detailHead__title")
    return heading.get_text(strip=True).strip("　 ") if heading else ""
