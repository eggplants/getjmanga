"""LINEマンガ, whose web viewer embeds its page list in the HTML and tile-shuffles print pages.

Two kinds of works are readable on the web without an account: the serials
(`/product/periodic?id=<product>`, read through `/book/viewer?id=<book>`)
and the indies works (`/indies/product/detail?id=<product>`, read through
`/indies/book/article?id=<book>`). Both viewer pages carry the same `OPTION`
object: a webtoon comes as `imgs`, a list of plain JPEGs, and a print comic
as `portal_pages`, whose blocks the viewer puts back with `parseInt(m, 35)`.
The store's volumes open a Mediado viewer instead and are not read here.
"""

from __future__ import annotations

import html
import re
from http import HTTPStatus
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlparse

from .common import Episode, Extractor, NotAnEpisodePageError, Page, UnsupportedUrlError

if TYPE_CHECKING:
    from PIL import Image
    from requests import Session

BASE_URL = "https://manga.line.me"
#: The episode list a work page loads, in reading order (`rows=1000` is what the site asks for).
PERIODIC_LIST_URL = f"{BASE_URL}/api/book/product_list?need_read_info=1&rows=1000&is_periodic=1&product_id="
#: The same for an indies work, newest first.
INDIES_LIST_URL = f"{BASE_URL}/api/indies/book/product_list?rows=1000&product_id="

_BOOK_ID = re.compile(r"^[A-Za-z0-9]+$")

_API_HEADERS = {"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"}

# `var OPTION = { ... }` of a viewer page: one `key: 'value'`, `key: 123` or `key: true` per line.
_OPTION_START = re.compile(r"var OPTION = \{")
_STRING = r"'(?P<string>(?:[^'\\]|\\.)*)'"
_SCALAR = r"(?P<scalar>-?\d+|true|false|null)"
# `imgs[3] = { 'url': '...', 'height': 123, 'width': 456 };`
_IMG = re.compile(
    r"imgs\[(?P<index>\d+)\]\s*=\s*\{\s*'url':\s*'(?P<url>[^']*)',\s*'height':\s*(?P<h>\d+),\s*'width':\s*(?P<w>\d+)"
)
# `portal_pages[3] = { 'page_number': 4, 'url': '...', 'metadata': { 'hc': 11, 'bwd': 64, 'vc': 18, 'iw': 744, ...`
_PORTAL_PAGE = re.compile(
    r"portal_pages\[(?P<index>\d+)\]\s*=\s*\{\s*'page_number':\s*(?P<number>\d+),\s*'url':\s*'(?P<url>[^']*)',"
    r"\s*'metadata':\s*\{\s*'hc':\s*(?P<hc>\d+),\s*'bwd':\s*(?P<bwd>\d+),\s*'vc':\s*(?P<vc>\d+),"
    r"\s*'iw':\s*(?P<iw>\d+),\s*'ih':\s*(?P<ih>\d+)"
)
# `portal_pages[3].metadata.m[7] = '2x';`
_PORTAL_BLOCK = re.compile(r"portal_pages\[(?P<index>\d+)\]\.metadata\.m\[(?P<slot>\d+)\]\s*=\s*'(?P<block>[^']*)'")


def descramble(image: Image.Image, columns: int, block: int, blocks: list[str]) -> Image.Image:
    """Put a print page back together the way the viewer draws a `Portal` page.

    The page is cut into `block`-pixel squares, `columns` per row, and served
    with those squares shuffled. `blocks[n]` names, in base 35, the square of
    the served file that belongs at slot `n` of the page; the strip on the
    right and at the bottom that no square covers is left as served.

    Args:
        image: The page as served.
        columns: `hc` of the page's metadata.
        block: `bwd` of the page's metadata, the side of a square in pixels.
        blocks: `m` of the page's metadata.

    Returns:
        The readable page, the size of the served one.
    """
    result = image.copy()
    for slot, name in enumerate(blocks):
        source = int(name, 35)
        sx, sy = source % columns * block, source // columns * block
        dx, dy = slot % columns * block, slot // columns * block
        result.paste(image.crop((sx, sy, sx + block, sy + block)), (dx, dy))
    return result


def episode_url(book_id: str, *, indies: bool = False) -> str:
    """The canonical URL of an episode.

    Args:
        book_id: The book id, `Z0090127` of a serial or `184117` of an indies work.
        indies: Whether the episode belongs to an indies work.

    Returns:
        The viewer URL.
    """
    if indies:
        return f"{BASE_URL}/indies/book/article?id={book_id}"
    return f"{BASE_URL}/book/viewer?id={book_id}"


def _query_id(url: str) -> str | None:
    """The `id` parameter of a URL, when it looks like one of the site's ids."""
    values = parse_qs(urlparse(url).query).get("id") or []
    if len(values) == 1 and _BOOK_ID.match(values[0]):
        return values[0]
    return None


def _kind(url: str) -> tuple[str, str] | None:
    """What a URL names: `("episode" | "series", "periodic" | "indies")`, or None."""
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    if _query_id(url) is None:
        return None
    return {
        "/book/viewer": ("episode", "periodic"),
        "/indies/book/article": ("episode", "indies"),
        "/product/periodic": ("series", "periodic"),
        "/indies/product/detail": ("series", "indies"),
    }.get(path)


def parse_option(page: str) -> dict[str, Any]:
    """Read the scalar fields of the viewer's `OPTION` object, and its `next_book`.

    Strings are HTML-escaped by the site (`Let&#39;s`), so they are unescaped here.

    Args:
        page: The viewer page.

    Returns:
        The fields by name, `next_book` as a nested dict when the page has one.
    """
    start = _OPTION_START.search(page)
    if start is None:
        return {}
    body = page[start.end() :]
    end = body.find("\n        };")
    body = body if end < 0 else body[:end]

    option: dict[str, Any] = {}
    field = re.compile(rf"^\s*(?P<key>\w+):\s*(?:{_STRING}|{_SCALAR})\s*,?\s*$", re.MULTILINE)
    nested = re.search(r"next_book:\s*\{(?P<body>.*?)\}", body, re.DOTALL)
    for match in field.finditer(body if nested is None else body[: nested.start()] + body[nested.end() :]):
        option[match["key"]] = _value(match)
    if nested is not None:
        option["next_book"] = {match["key"]: _value(match) for match in field.finditer(nested["body"])}
    return option


def _value(match: re.Match[str]) -> str | int | bool | None:
    if match["string"] is not None:
        return html.unescape(re.sub(r"\\(.)", r"\1", match["string"]))
    scalar = match["scalar"]
    if scalar in ("true", "false"):
        return scalar == "true"
    if scalar == "null":
        return None
    return int(scalar)


def parse_pages(page: str) -> tuple[Page, ...]:
    """Read the page list of a viewer page.

    A webtoon lists plain images as `imgs`; a print comic lists shuffled
    ones as `portal_pages`, with what `descramble()` needs in `Page.extra`.

    Args:
        page: The viewer page.

    Returns:
        The pages in reading order.
    """
    portal = {int(m["index"]): m for m in _PORTAL_PAGE.finditer(page)}
    if portal:
        blocks: dict[int, list[tuple[int, str]]] = {index: [] for index in portal}
        for match in _PORTAL_BLOCK.finditer(page):
            blocks.setdefault(int(match["index"]), []).append((int(match["slot"]), match["block"]))
        pages = []
        for index in sorted(portal):
            match = portal[index]
            names = [name for _, name in sorted(blocks[index])]
            extra = {"columns": int(match["hc"]), "block": int(match["bwd"]), "blocks": names}
            pages.append(Page(url=match["url"], width=int(match["iw"]), height=int(match["ih"]), extra=extra))
        return tuple(pages)
    plain = {int(m["index"]): m for m in _IMG.finditer(page)}
    return tuple(Page(url=plain[i]["url"], width=int(plain[i]["w"]), height=int(plain[i]["h"])) for i in sorted(plain))


class LineManga(Extractor):
    """Fetch episodes from LINEマンガ."""

    NAME = "linemanga"
    HOSTS = ("manga.line.me",)
    URL_FORMS = (
        "https://manga.line.me/book/viewer?id=<book>",
        "https://manga.line.me/product/periodic?id=<product>",
        "https://manga.line.me/indies/book/article?id=<book>",
        "https://manga.line.me/indies/product/detail?id=<product>",
    )

    def __init__(self, session: Session | None = None) -> None:
        """Build an extractor.

        Args:
            session: A session to reuse. A retrying one is made when omitted.
        """
        super().__init__(session)
        #: Episode listings by `(flavour, product id)`; a bulk run asks for one per episode.
        self._listings: dict[tuple[str, str], list[dict[str, Any]]] = {}
        #: Which product each listed book belongs to, so a locked episode needs no extra request.
        self._products: dict[str, str] = {}
        #: Work titles by `(flavour, product id)`, as the listings named them.
        self._names: dict[tuple[str, str], str] = {}

    @classmethod
    def suitable(cls, url: str) -> bool:
        """Report whether the extractor takes `url`.

        Args:
            url: The URL to check.

        Returns:
            True for a viewer or a work page, of a serial or an indies work.
        """
        return super().suitable(url) and _kind(url) is not None

    @classmethod
    def is_series(cls, url: str) -> bool:
        """Report whether `url` names a whole work rather than one episode.

        Args:
            url: The URL to check.

        Returns:
            True for `/product/periodic?id=` and `/indies/product/detail?id=`.
        """
        kind = _kind(url)
        return kind is not None and kind[0] == "series"

    def series_urls(self, url: str) -> list[str]:
        """List every episode of a work, first episode first.

        Args:
            url: A work URL.

        Returns:
            One viewer URL per listed episode, locked ones included.

        Raises:
            UnsupportedUrlError: The URL is not a work page.
            NotAnEpisodePageError: The work lists no episode.
        """
        kind = _kind(url)
        if kind is None or kind[0] != "series":
            msg = f"{url} is not a series page."
            raise UnsupportedUrlError(msg)
        flavour = kind[1]
        product_id = _query_id(url) or ""
        urls: list[str] = []
        for entry in self._listing(flavour, product_id, url):
            candidate = episode_url(str(entry["id"]), indies=flavour == "indies")
            if candidate not in urls:
                urls.append(candidate)
        if not urls:
            msg = f"the series at {url} lists no episode."
            raise NotAnEpisodePageError(msg)
        return urls

    def episode(self, url: str) -> Episode:
        """Read one episode and list its pages.

        The viewer page carries everything for a readable episode. A locked
        one (coins, an advance release) answers 404 instead, so the book's
        `/book/detail` redirect names its work and the work's listing names
        the episode and the one after it.

        Args:
            url: The viewer URL.

        Returns:
            The episode. `pages` is empty when it is not readable.

        Raises:
            UnsupportedUrlError: The URL is not a viewer URL.
            NotAnEpisodePageError: The site knows no such episode.
        """
        kind = _kind(url)
        if kind is None or kind[0] != "episode":
            msg = f"{url} is not an episode page."
            raise UnsupportedUrlError(msg)
        flavour = kind[1]
        book_id = _query_id(url) or ""
        canonical = episode_url(book_id, indies=flavour == "indies")

        res = self._session.get(canonical, headers=self.HEADERS, timeout=self.TIMEOUT)
        if res.status_code == HTTPStatus.NOT_FOUND:
            return self._locked(flavour, book_id, canonical)
        res.raise_for_status()
        option = parse_option(res.text)
        if not option.get("bookId"):
            msg = f"no viewer on {canonical}."
            raise NotAnEpisodePageError(msg)

        next_book = option.get("next_book") or {}
        next_id = next_book.get("id")
        return Episode(
            url=canonical,
            series_title=str(option.get("productName") or option.get("productId") or ""),
            episode_title=str(option.get("title") or book_id),
            pages=parse_pages(res.text),
            next_url=episode_url(str(next_id), indies=flavour == "indies") if next_id else None,
            metadata={"option": option},
        )

    def image(self, page: Page, episode: Episode) -> Image.Image:
        """Fetch one page, and put a print page back together.

        Args:
            page: The page to fetch.
            episode: The episode the page belongs to.

        Returns:
            The page image.
        """
        image = self._fetch_image(page.url, headers={**self.HEADERS, "Referer": episode.url})
        blocks = page.extra.get("blocks")
        if not blocks:
            return image
        return descramble(image, int(page.extra["columns"]), int(page.extra["block"]), [str(b) for b in blocks])

    def _locked(self, flavour: str, book_id: str, url: str) -> Episode:
        """Describe an episode whose viewer answered 404, from its work's listing.

        Raises:
            NotAnEpisodePageError: No work claims the book.
        """
        product_id = self._products.get(book_id)
        if product_id is None and flavour == "periodic":
            res = self._session.get(f"{BASE_URL}/book/detail?id={book_id}", headers=self.HEADERS, timeout=self.TIMEOUT)
            landed = _kind(res.url)
            if res.ok and landed == ("series", "periodic"):
                product_id = _query_id(res.url)
        if product_id is None:
            msg = f"no episode {book_id} on {BASE_URL}."
            raise NotAnEpisodePageError(msg)

        entries = self._listing(flavour, product_id, url)
        index = next((i for i, entry in enumerate(entries) if str(entry.get("id")) == book_id), None)
        if index is None:
            msg = f"no episode {book_id} in the series {product_id} on {BASE_URL}."
            raise NotAnEpisodePageError(msg)
        entry = entries[index]
        next_url = None
        if index + 1 < len(entries):
            next_url = episode_url(str(entries[index + 1]["id"]), indies=flavour == "indies")
        return Episode(
            url=url,
            series_title=str(entry.get("product_name") or self._names.get((flavour, product_id)) or product_id),
            episode_title=str(entry.get("name") or book_id),
            next_url=next_url,
            metadata={"book": entry},
        )

    def _listing(self, flavour: str, product_id: str, referer: str) -> list[dict[str, Any]]:
        """The episodes of a work in reading order, fetched once per work.

        Raises:
            NotAnEpisodePageError: The site knows no such work.
        """
        key = (flavour, product_id)
        if key not in self._listings:
            api = INDIES_LIST_URL if flavour == "indies" else PERIODIC_LIST_URL
            res = self._session.get(
                api + product_id, headers={**self.HEADERS, **_API_HEADERS, "Referer": referer}, timeout=self.TIMEOUT
            )
            body = res.json() if res.status_code == HTTPStatus.OK else None
            result = body.get("result") if isinstance(body, dict) else None
            if not isinstance(result, dict) or not isinstance(result.get("rows"), list):
                msg = f"no series {product_id} on {BASE_URL}."
                raise NotAnEpisodePageError(msg)
            rows: list[dict[str, Any]] = [row for row in result["rows"] if isinstance(row, dict)]
            if flavour == "indies":
                rows.reverse()
            product = result.get("product")
            if isinstance(product, dict) and product.get("name"):
                self._names[key] = str(product["name"])
            for row in rows:
                self._products[str(row.get("id"))] = product_id
            self._listings[key] = rows
        return self._listings[key]
