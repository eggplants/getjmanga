"""マンガ5 (レベルファイブ), comicブースト's storefront with two viewers behind it.

The storefront is the one `boost.py` reads: a work page, `/content/<id>`,
lists its episodes twenty per page (`?order=asc&p=<n>` walks them first
episode first), and an episode page, `/product/<id>`, answers with a 302
when the session may read it and with an HTML page carrying an `alert()`
when it may not. The 302 goes to an interstitial,
`/ads_before_launching_viewer.html?id=<id>`, whose 「作品を読む」 button
carries the viewer URL, `/viewer.html?series=<id>&item=<id>&cid=<token>`,
and sets the `access_id` cookie that the viewer's license call,
`/api4js/contents/license?cid=`, checks the `cid` against. The license names
the episode's content directory on the CDN, a content type (`cty`) and a
CloudFront signature (`Policy`, `Signature`, `Key-Pair-Id`) every file under
the directory has to be asked for with.

What the directory holds depends on `cty`:

- `6` is the site's own React viewer (`/manga5/comic/viewer-*.js`) reading
  `content.json`: a list of story steps, each naming one or more plain images.
- `0`, `1` and `2` are PUBLUS fixed-layout books, `configuration_pack.json`,
  read by ACCESS's PUBLUS Reader. Most packs come wrapped in the cipher
  `boost.py` unwraps, with hashed page names and the xorshift tile shuffle,
  and that port does the work. The free samples of the paid volumes come as
  plain JSON without shuffle seeds instead, and their pages are tiled by an
  older, unkeyed shuffle: 64x64 blocks moved around by one of four fixed
  patterns picked from the page's path. `descramble()` below is a port of
  that one, read out of the React viewer (its `blocks` function).

The viewer page's `window.__data` names the episode (`title`, as
`<episode> - <series>`) and the next one; there is no colophon page, so a
locked episode is named off the work page's listing. Sign-in is OAuth only
(LEVEL5 ID, LINE, Yahoo!, Google), so `login()` stays the default.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import parse_qs, unquote, urlencode, urlparse

from bs4 import BeautifulSoup
from bs4.element import Tag
from PIL import Image

from .boost import Boost, Slice, decode_pack
from .boost import descramble as descramble_keyed
from .common import Episode, Extractor, NotAnEpisodePageError, Page, UnsupportedUrlError

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    from requests import Response

BASE_URL = "https://manga-5.com"
#: The viewer's license call; the `cid` of the viewer URL goes in decoded.
LICENSE_URL = f"{BASE_URL}/api4js/contents/license"
VIEWER_PATH = "/viewer.html"
ADS_PATH = "/ads_before_launching_viewer.html"
#: The `cty` of an episode read from `content.json`; anything else is a PUBLUS pack.
KOMA_TYPE = "6"
#: What the site says on a product page it will not open.
_NOT_FOUND = "作品情報が見つかりませんでした"
#: What the viewer page's `<title>` ends with, after the episode's own title.
_TITLE_SUFFIX = " - マンガ5"
#: What the viewer joins the episode and series titles with.
_TITLE_SEP = " - "

_PRODUCT_PATH = re.compile(r"^/product/(?P<id>\d{8})/?$")
_CONTENT_PATH = re.compile(r"^/content/(?P<id>\d{8})(?:/\d+)?/?$")
_PRODUCT_ID = re.compile(r"^\d{8}$")
_VIEWER_DATA = re.compile(r"window\.__data\s*=\s*(\{.*\})", re.DOTALL)
_LOCATION_REPLACE = re.compile(r"location\.replace\('(?P<url>[^']+)'\)")

# --- the unkeyed tile shuffle (the React viewer's `blocks`) ---------------------------

#: The tile size of the shuffle, and how many patterns it knows.
BLOCK = 64
PATTERN_COUNT = 4
#: Fewer tiles a side than this and the viewer's arithmetic falls apart (`NaN`).
_MIN_TILES = 2


def shuffle_pattern(file: str, page_no: str) -> int:
    """The pattern a page is shuffled with: `1` to `PATTERN_COUNT`, from `<file>/<No>`."""
    return sum(ord(char) for char in f"{file}/{page_no}") % PATTERN_COUNT + 1


def _rem(a: int, b: int) -> int:
    """JavaScript's `%`: the sign follows the dividend."""
    r = abs(a) % b
    return -r if a < 0 else r


def _pivot(count: int, prime: int, pattern: int) -> int:
    """Where the edge strip is cut into a row (or column) of `count` tiles."""
    pivot = count - _rem(prime * pattern, count)
    if _rem(pivot, count) == 0:
        pivot = _rem(count - 4, count)
    return count - 1 if pivot == 0 else pivot


def _offset(index: int, pivot: int, gap: int, block: int) -> int:
    """The pixel offset of tile `index`, the edge strip (`gap` wide) inserted at `pivot`."""
    return index * block + (gap if index >= pivot else 0)


def _strip_row(col: int, pivot_x: int, pivot_y: int, rows: int, pattern: int) -> int:
    """The tile row the bottom strip's piece under column `col` came from."""
    odd = pattern % 2 == 1
    upper = odd if col < pivot_x else not odd
    span, base = (pivot_y, 0) if upper else (rows - pivot_y, pivot_y)
    return (col + 53 * pattern + 59 * pivot_y) % span + base


def _strip_col(row: int, pivot_x: int, pivot_y: int, cols: int, pattern: int) -> int:
    """The tile column the right strip's piece beside row `row` came from."""
    odd = pattern % 2 == 1
    right = odd if row < pivot_y else not odd
    span, base = (cols - pivot_x, pivot_x) if right else (pivot_x, 0)
    return (row + 67 * pattern + pivot_x + 71) % span + base


def tile_slices(width: int, height: int, pattern: int, block: int = BLOCK) -> list[Slice]:
    """Where every rectangle of a shuffled page came from.

    Args:
        width: The scrambled image's width.
        height: The scrambled image's height.
        pattern: `shuffle_pattern()` of the page.
        block: The tile size.

    Returns:
        The corner, the bottom strip, the right strip, then the full tiles.
        Empty below two tiles a side, where the viewer's arithmetic falls apart.
    """
    cols, rows = width // block, height // block
    if cols < _MIN_TILES or rows < _MIN_TILES:
        return []
    gap_x, gap_y = width % block, height % block
    pivot_x = _pivot(cols, 43, pattern)
    pivot_y = _pivot(rows, 47, pattern)
    out: list[Slice] = []
    if gap_x and gap_y:
        out.append(Slice(pivot_x * block, pivot_y * block, pivot_x * block, pivot_y * block, gap_x, gap_y))
    if gap_y:
        for x in range(cols):
            src_col = (x + 61 * pattern) % cols
            src_row = _strip_row(src_col, pivot_x, pivot_y, rows, pattern)
            src_x = _offset(src_col, pivot_x, gap_x, block)
            dst_x = _offset(x, pivot_x, gap_x, block)
            out.append(Slice(src_x, src_row * block, dst_x, pivot_y * block, block, gap_y))
    if gap_x:
        for y in range(rows):
            src_row = (y + 73 * pattern) % rows
            src_x = _strip_col(src_row, pivot_x, pivot_y, cols, pattern) * block
            src_y = _offset(src_row, pivot_y, gap_y, block)
            dst_y = _offset(y, pivot_y, gap_y, block)
            out.append(Slice(src_x, src_y, pivot_x * block, dst_y, gap_x, block))
    for x in range(cols):
        for y in range(rows):
            src_col = (x + 29 * pattern + 31 * y) % cols
            src_row = (y + 37 * pattern + 41 * src_col) % rows
            src_x = src_col * block + (gap_x if src_col >= _strip_col(src_row, pivot_x, pivot_y, cols, pattern) else 0)
            src_y = src_row * block + (gap_y if src_row >= _strip_row(src_col, pivot_x, pivot_y, rows, pattern) else 0)
            dst_x = _offset(x, pivot_x, gap_x, block)
            dst_y = _offset(y, pivot_y, gap_y, block)
            out.append(Slice(src_x, src_y, dst_x, dst_y, block, block))
    return out


def descramble(image: Image.Image, pattern: int, size: tuple[int, int] | None = None) -> Image.Image:
    """Put an unkeyed-shuffle page back together.

    Args:
        image: The page as the CDN serves it, dummy strips included.
        pattern: `shuffle_pattern()` of the page.
        size: The page's content area, to cut the dummy strips off; None keeps the whole image.

    Returns:
        A new image.
    """
    out = Image.new(image.mode, image.size)
    for piece in tile_slices(image.width, image.height, pattern):
        tile = image.crop((piece.src_x, piece.src_y, piece.src_x + piece.width, piece.src_y + piece.height))
        out.paste(tile, (piece.dst_x, piece.dst_y))
    if size is not None and 0 < size[0] <= out.width and 0 < size[1] <= out.height:
        out = out.crop((0, 0, size[0], size[1]))
    return out


# --- the site ------------------------------------------------------------------------


@dataclass(frozen=True)
class ViewerData:
    """What a viewer page's `window.__data` says about its episode."""

    series_title: str
    episode_title: str
    next_url: str | None = None
    raw: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class Listing:
    """One page of a work page's episode list."""

    #: The work's title, off its `h1`.
    title: str
    #: `(product id, episode title)` in page order.
    items: tuple[tuple[str, str], ...]
    #: Whether the pager offers a page after this one.
    has_next: bool


def product_url(product_id: str) -> str:
    """The canonical URL of an episode."""
    return f"{BASE_URL}/product/{product_id}"


def content_url(content_id: str) -> str:
    """The canonical URL of a work page."""
    return f"{BASE_URL}/content/{content_id}"


def series_id(product_id: str) -> str:
    """The work an episode belongs to: the first four digits of the id, then `0001`."""
    return product_id[:4] + "0001"


def viewer_cid(url: str) -> str | None:
    """The `cid` of a viewer URL, decoded, or None for any other page.

    The interstitial writes the viewer URL with the `cid` encoded twice; the
    viewer's `URLSearchParams` undoes one layer and the license call the other.
    """
    parsed = urlparse(url)
    if parsed.path != VIEWER_PATH:
        return None
    cids = parse_qs(parsed.query).get("cid")
    return unquote(cids[0]) if cids else None


def ads_viewer_url(html: str | bytes) -> str | None:
    """The viewer URL the 「作品を読む」 button of the interstitial opens, or None."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(onclick=True):
        match = _LOCATION_REPLACE.search(str(tag.attrs["onclick"]))
        if match is not None and urlparse(match["url"]).path == VIEWER_PATH:
            return match["url"]
    return None


def split_title(title: str, hint: str = "") -> tuple[str, str]:
    """Take a viewer title, `<episode> - <series>`, apart.

    Args:
        title: The viewer's `title`.
        hint: The viewer's tweet text, `<series> <episode>を読みました ...`,
            which settles a title with more than one ` - ` in it.

    Returns:
        The series title and the episode title; both the whole title when it
        holds no separator.
    """
    parts = title.split(_TITLE_SEP)
    splits = [(_TITLE_SEP.join(parts[i:]), _TITLE_SEP.join(parts[:i])) for i in range(1, len(parts))]
    for series, episode in splits:
        if hint.startswith(f"{series} {episode}"):
            return series, episode
    return splits[0] if splits else (title, title)


def parse_viewer(html: str | bytes) -> ViewerData | None:
    """Read the titles and the next episode off a viewer page.

    Args:
        html: The page.

    Returns:
        None when the page names no episode.
    """
    soup = BeautifulSoup(html, "html.parser")
    data: dict[str, Any] = {}
    for script in soup.find_all("script"):
        match = _VIEWER_DATA.search(script.get_text())
        if match is None:
            continue
        try:
            loaded = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(loaded, dict):
            data = loaded
            break
    title = str(data.get("title") or "")
    if not title and soup.title is not None:
        title, _, _ = soup.title.get_text().partition(_TITLE_SUFFIX)
    if not title:
        return None
    twitter = data.get("twitter")
    tweet_url = str(twitter.get("to") or "") if isinstance(twitter, dict) else ""
    hint = parse_qs(urlparse(tweet_url).query).get("text", [""])[0]
    series, episode = split_title(title, hint)

    next_url = None
    next_ = data.get("next")
    if isinstance(next_, dict) and next_.get("to"):
        ids = parse_qs(urlparse(str(next_["to"])).query).get("content_id", [""])
        if _PRODUCT_ID.match(ids[0]):
            next_url = product_url(ids[0])
    return ViewerData(series_title=series, episode_title=episode, next_url=next_url, raw=data)


def parse_listing(html: str | bytes) -> Listing:
    """Read one page of a work page's episode list.

    Args:
        html: The page.

    Returns:
        The work's title, its listed episodes and whether the pager goes on.
    """
    soup = BeautifulSoup(html, "html.parser")
    heading = soup.select_one("h1.comic-title")
    items = tuple(
        (str(item.attrs["data-id"]), " ".join(str(item.attrs.get("data-title") or "").split()))
        for item in soup.select("a.book-product-list-item[data-id]")
    )
    pager_next = soup.select_one("li.to-next")
    has_next = isinstance(pager_next, Tag) and "disabled" not in (pager_next.get("class") or [])
    return Listing(
        title=" ".join(heading.get_text().split()) if isinstance(heading, Tag) else "",
        items=items,
        has_next=has_next,
    )


def koma_pages(content: Any, base: str, auth: str) -> list[Page]:  # noqa: ANN401 (whatever the CDN's JSON parsed to)
    """The images of a `content.json` episode, in reading order.

    Args:
        content: The parsed `content.json`: a list of stories, each a list of steps.
        base: The content directory on the CDN, with its trailing slash.
        auth: The signature query string every file is fetched with.

    Returns:
        One page per image a step names.
    """
    pages: list[Page] = []
    for story in content if isinstance(content, list) else []:
        for step in story if isinstance(story, list) else []:
            if not isinstance(step, dict):
                continue
            width = int(step.get("originalPicWidth") or 0)
            height = int(step.get("originalPicHeight") or 0)
            pages.extend(
                Page(url=f"{base}{name}?{auth}", width=width, height=height)
                for name in step.get("effectTargetImgs") or []
            )
    return pages


def plain_pages(pack: Mapping[str, Any], base: str, auth: str) -> list[Page]:
    """The pages of a plain, unwrapped configuration pack, in reading order.

    Args:
        pack: The parsed `configuration_pack.json`.
        base: The content directory on the CDN, with its trailing slash.
        auth: The signature query string every file is fetched with.

    Returns:
        One page per `PageLinkInfoList` entry, each with its shuffle pattern.
    """
    configuration = pack.get("configuration")
    if not isinstance(configuration, dict):
        return []
    pages: list[Page] = []
    for entry in configuration.get("contents") or []:
        file = str(entry.get("file") or "") if isinstance(entry, dict) else ""
        described = pack.get(file)
        if not file or not isinstance(described, dict) or described.get("Linear") == 0:
            continue
        link_info = described.get("FileLinkInfo") or {}
        for link in link_info.get("PageLinkInfoList") or []:
            info = link.get("Page") if isinstance(link, dict) else None
            if not isinstance(info, dict):
                continue
            page_no = str(info.get("No", 0))
            area = info.get("ContentArea") or info.get("Size") or {}
            width, height = int(area.get("Width") or 0), int(area.get("Height") or 0)
            extra: dict[str, Any] = {"pattern": shuffle_pattern(file, page_no)}
            if width and height:
                extra["size"] = [width, height]
            pages.append(Page(url=f"{base}{file}/{page_no}.jpeg?{auth}", width=width, height=height, extra=extra))
    return pages


class Manga5(Extractor):
    """Fetch episodes from マンガ5."""

    NAME = "manga5"
    HOSTS = ("manga-5.com", "www.manga-5.com")
    URL_FORMS = (
        "https://manga-5.com/product/<id>",
        "https://manga-5.com/content/<id>",
    )
    #: How many listing pages a work is allowed to have before the walk gives up.
    MAX_LISTING_PAGES: ClassVar[int] = 500

    @classmethod
    def suitable(cls, url: str) -> bool:
        """Report whether the extractor takes `url`.

        Args:
            url: The URL to check.

        Returns:
            True for an episode or a work page on a known host.
        """
        if not super().suitable(url):
            return False
        path = urlparse(url).path
        return _PRODUCT_PATH.match(path) is not None or _CONTENT_PATH.match(path) is not None

    @classmethod
    def is_series(cls, url: str) -> bool:
        """Report whether `url` is a work page rather than an episode.

        Args:
            url: The URL to check.

        Returns:
            True for `/content/<id>`, with or without a listing page number.
        """
        return _CONTENT_PATH.match(urlparse(url).path) is not None

    def series_urls(self, url: str) -> list[str]:
        """List every episode of a work, first episode first.

        Args:
            url: A work URL.

        Returns:
            One episode URL per listed episode, paid ones included.

        Raises:
            UnsupportedUrlError: The URL is not a work page.
            NotAnEpisodePageError: The work lists no episode.
        """
        match = _CONTENT_PATH.match(urlparse(url).path)
        if match is None:
            msg = f"{url} is not a work page."
            raise UnsupportedUrlError(msg)
        urls: list[str] = []
        for listing in self._listings(match["id"]):
            fresh = False
            for product_id, _ in listing.items:
                candidate = product_url(product_id)
                if candidate not in urls:
                    urls.append(candidate)
                    fresh = True
            if not fresh:
                break
        if not urls:
            msg = f"the work at {url} lists no episode."
            raise NotAnEpisodePageError(msg)
        return urls

    def episode(self, url: str) -> Episode:
        """Read one episode and list its pages.

        Args:
            url: The episode URL.

        Returns:
            The episode. `pages` is empty when the product page does not
            hand the session over to the viewer, or the license call says no.

        Raises:
            UnsupportedUrlError: The URL is not an episode URL.
            NotAnEpisodePageError: The site knows no such episode.
        """
        match = _PRODUCT_PATH.match(urlparse(url).path)
        if match is None:
            msg = f"{url} is not an episode page."
            raise UnsupportedUrlError(msg)
        product_id = match["id"]
        canonical = product_url(product_id)

        res = self._get(canonical)
        viewer = self._viewer_page(res)
        if viewer is None:
            if _NOT_FOUND in res.text:
                msg = f"no episode {product_id} on {BASE_URL}."
                raise NotAnEpisodePageError(msg)
            return self._locked(product_id)
        data = parse_viewer(viewer.content)
        if data is None:
            msg = f"no episode data on the viewer page of {canonical}."
            raise NotAnEpisodePageError(msg)
        cid = viewer_cid(viewer.url)
        license_: dict[str, Any] = {}
        if cid is not None:
            answer = self._get(LICENSE_URL, params={"cid": cid}, headers={**self.HEADERS, "Referer": viewer.url}).json()
            license_ = answer if isinstance(answer, dict) else {}
        base = str(license_.get("url") or "")
        auth_info = license_.get("auth_info")
        if str(license_.get("status")) != "200" or not base or not isinstance(auth_info, dict):
            return Episode(
                url=canonical,
                series_title=data.series_title,
                episode_title=data.episode_title,
                next_url=data.next_url,
                metadata={"viewer": data.raw, "license": license_},
            )
        if not base.endswith("/"):
            base += "/"
        auth = {key: str(value) for key, value in auth_info.items()}
        pages, described = self._pages(base, auth, str(license_.get("cty")))
        return Episode(
            url=canonical,
            series_title=data.series_title,
            episode_title=data.episode_title,
            pages=tuple(pages),
            next_url=data.next_url,
            metadata={"viewer": data.raw, "license": license_, **described},
        )

    def image(self, page: Page, episode: Episode) -> Image.Image:
        """Fetch one page and unshuffle its tiles, whichever shuffle it has.

        Args:
            page: The page to fetch.
            episode: The episode the page belongs to.

        Returns:
            The page image.
        """
        image = self._fetch_image(page.url, headers={**self.HEADERS, "Referer": episode.url})
        extra = page.extra
        if "seeds" in extra:
            seeds, block = extra["seeds"], extra["block"]
            return descramble_keyed(image, int(extra["pattern"]), (seeds[0], seeds[1], seeds[2]), (block[0], block[1]))
        if "pattern" in extra:
            size = extra.get("size")
            return descramble(image, int(extra["pattern"]), (int(size[0]), int(size[1])) if size else None)
        return image

    def _viewer_page(self, res: Response) -> Response | None:
        """The viewer page a product page led to, or None when it led nowhere."""
        path = urlparse(res.url).path
        if path == VIEWER_PATH:
            return res
        if path != ADS_PATH:
            return None
        viewer_url = ads_viewer_url(res.content)
        if viewer_url is None:
            return None
        return self._get(viewer_url, headers={**self.HEADERS, "Referer": res.url})

    def _pages(self, base: str, auth: Mapping[str, str], cty: str) -> tuple[list[Page], dict[str, Any]]:
        """The pages of a licensed content directory, and what described them."""
        query = urlencode(auth)
        if cty == KOMA_TYPE:
            content = self._get(f"{base}content.json", params=auth).json()
            steps = [story for story in content if story] if isinstance(content, list) else content
            return koma_pages(content, base, query), {"content": steps}
        res = self._get(f"{base}configuration_pack.json", params=auth)
        pack = res.json()
        if not isinstance(pack, dict):
            return [], {}
        if "data" in pack and "configuration" not in pack:
            decoded = decode_pack(res.text)
            pages = [
                replace(page, url=f"{page.url}?{query}")
                for page in Boost._pages(decoded, base)  # noqa: SLF001 (the PUBLUS pack walk lives with its port)
            ]
            return pages, {"configuration": decoded.content["configuration"]}
        return plain_pages(pack, base, query), {"configuration": pack.get("configuration")}

    def _listings(self, content_id: str) -> Iterator[Listing]:
        """Walk a work's listing pages oldest first, until the pager's "next" is disabled."""
        listing_url = content_url(content_id)
        for page in range(1, self.MAX_LISTING_PAGES + 1):
            listing = parse_listing(self._get(listing_url, params={"order": "asc", "p": page}).content)
            yield listing
            if not listing.has_next:
                break

    def _locked(self, product_id: str) -> Episode:
        """An episode the product page would not open, named off the work's listing."""
        series_title = episode_title = product_id
        next_url = None
        found = False
        for listing in self._listings(series_id(product_id)):
            series_title = listing.title or series_title
            for item_id, item_title in listing.items:
                if found:
                    next_url = product_url(item_id)
                    break
                if item_id == product_id:
                    found = True
                    episode_title = item_title or episode_title
            if next_url is not None:
                break
        return Episode(
            url=product_url(product_id),
            series_title=series_title,
            episode_title=episode_title,
            next_url=next_url,
            metadata={"listing": {"content_id": series_id(product_id), "found": found}},
        )
