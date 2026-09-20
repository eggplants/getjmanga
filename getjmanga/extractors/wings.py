"""ウェブマガジン ウィングス (Shinshokan): work pages, and the two viewers its episodes open in.

The magazine's site is static HTML. A work page at
`https://www.shinshokan.com/webwings/title<NN>.html` names the work and holds
one to three "read it" buttons -- the first episode, the latest, sometimes
one in between -- each opening a viewer directory under
`https://www.shinshokan.com/webwings/contents/<slug>/`. An episode the
magazine has taken down is gone for good (HTTP 404) and its button with it;
nothing is paywalled and nothing asks for an account.

Two viewers are in use, both by Logosware and both plain static exports:

- **FLIPPER** (`html5/js/flipper.js`, `book.xml`): `book.xml` names the book,
  its page ids (`<data>`), its size and its `maxMagnification`. Every page is
  `page<id>/x1.jpg` at 1x; FLIPPER U (5.x) also keeps the whole page at
  `page<id>/x<n>/x<n>.jpg` for each magnification, while the older FLIPPER 3
  cuts the magnified page into `page<id>/x<n>/<k>.jpg` tiles of
  `sliceWidth` x `sliceHeight`, row by row. `image()` takes the largest
  magnification and stitches the tiles when it has to.
- **SMOOZY** (`smoozy.swf`, `xml/data.xml`): the Flash-era viewer of the
  oldest episodes. `xml/data.xml` lists one `swf/<k>.swf` per spread; each
  SWF carries its one or two pages as plain JPEGs (`DefineBitsJPEG2` tags),
  left page first, so a right-to-left book (`DIRECTION=right` in
  `data/book.conf`) reads them back to front.

Neither viewer scrambles anything, and the images need no cookie or Referer.
"""

from __future__ import annotations

import html
import re
import struct
import zlib
from dataclasses import dataclass
from http import HTTPStatus
from io import BytesIO
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from bs4.element import Tag
from PIL import Image
from requests import HTTPError

from getjmanga.errors import GetjmangaError, NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractor import Episode, Extractor, Page

if TYPE_CHECKING:
    from collections.abc import Iterator

    from requests import Response, Session

HOST = "www.shinshokan.com"
BASE_URL = f"https://{HOST}/webwings/"

# A work page: `/webwings/title80.html`.
_WORK_PATH = re.compile(r"^/webwings/(?P<id>title\d+)\.html$")
# A viewer directory: `/webwings/contents/title80-202607-0/`, with or without `index.html`.
_EPISODE_PATH = re.compile(r"^/webwings/contents/(?P<slug>[\w.-]+)/(?:index\.html)?$")
# The work a slug belongs to, when the slug says: `title80-202607-0` -> `title80`.
_SLUG_WORK = re.compile(r"^(?P<id>title\d+)(?![\d])")

# What a button's caption adds to the episode's name: `第0話を読む`, `第2話はこちらから`.
_CAPTION_SUFFIX = re.compile(r"(?:を読む|はこちらから|はこちら|を見る)$")
# A caption that names no episode.
_GENERIC_CAPTION = "最新話"
# Whatever joins the work's title and the episode's in a FLIPPER `bookTitle`.
_TITLE_GAP = re.compile(r"^[\s\u3000:/|\-\uff1a\uff0f\uff5c\uff0d]+")

# `<tag ...>value</tag>` of `book.xml`, the value with or without CDATA.
_XML_FIELD = re.compile(r"<(?P<tag>\w+)\b[^>]*>(?P<value>[^<]*|<!\[CDATA\[.*?\]\]>)</(?P=tag)>", re.DOTALL)
_CDATA = re.compile(r"^<!\[CDATA\[(?P<body>.*)\]\]>$", re.DOTALL)
# One spread of SMOOZY's `xml/data.xml`.
_SLIDE = re.compile(r'<slide\b[^>]*\burl="(?P<url>[^"]+)"')

# The major version FLIPPER U starts at; earlier exports keep magnified pages as tiles.
_FLIPPER_U_MAJOR = 5

# How the viewer page announces itself.
_FLIPPER_MARK = re.compile(r"flipper3js/|html5/js/flipper\.js|flipper-app", re.IGNORECASE)
_SMOOZY_MARK = re.compile(r"smoozy\.swf", re.IGNORECASE)

# SWF tags that carry a JPEG: DefineBitsJPEG2 and DefineBitsJPEG3.
_DEFINE_BITS_JPEG2 = 21
_DEFINE_BITS_JPEG3 = 35
_TAG_END = 0
_LONG_TAG_LENGTH = 0x3F
# A JPEG the Flash authoring tool wrote with an EOI/SOI pair in front.
_JPEG_ERRONEOUS_HEADER = b"\xff\xd9\xff\xd8"


@dataclass(frozen=True)
class Work:
    """What a work page says."""

    #: The work page URL.
    url: str
    #: `h3.title`: the work's title.
    title: str
    #: The listed episodes in reading order: canonical episode URL -> the button's caption.
    episodes: dict[str, str]


@dataclass(frozen=True)
class Book:
    """What a FLIPPER `book.xml` says."""

    #: `flipperVersion`, e.g. `5.0.10` or `3.0`.
    version: str
    #: `bookTitle`.
    title: str
    #: `data`: one folder id per page, in reading order.
    page_ids: tuple[str, ...]
    #: `maxMagnification`: the largest `x<n>` the export holds.
    magnification: int
    #: `pageWidth`, `pageHeight` at 1x.
    width: int
    height: int
    #: `sliceWidth`, `sliceHeight`: the tile size of a magnified page cut into tiles.
    slice_width: int
    slice_height: int
    #: Every simple field of the file, as written.
    fields: dict[str, str]

    @property
    def sliced(self) -> bool:
        """Whether the magnified pages are tiles (FLIPPER 3) rather than one file (FLIPPER U)."""
        return _int(self.version.split(".")[0]) < _FLIPPER_U_MAJOR

    @property
    def password_protected(self) -> bool:
        """Whether the viewer asks for a password before showing a page."""
        return bool(self.fields.get("contentPasswordHash", "").strip())


def episode_url(url: str) -> str | None:
    """The canonical `https://www.shinshokan.com/webwings/contents/<slug>/` of a viewer URL, or None."""
    parsed = urlparse(url)
    if parsed.hostname != HOST:
        return None
    match = _EPISODE_PATH.match(parsed.path)
    return f"{BASE_URL}contents/{match['slug']}/" if match else None


def work_url(url: str) -> str | None:
    """The canonical `https://www.shinshokan.com/webwings/title<NN>.html` of a work URL, or None."""
    parsed = urlparse(url)
    if parsed.hostname != HOST:
        return None
    match = _WORK_PATH.match(parsed.path)
    return f"{BASE_URL}{match['id']}.html" if match else None


def parse_work(page: str | bytes, url: str) -> Work:
    """Read a work page into a `Work`.

    Args:
        page: The page.
        url: The URL it came from, to resolve links against.

    Returns:
        The work's title and the viewer links of its `div.to_viewer` blocks,
        in page order (first episode, then the later ones), deduplicated.
        Links to other stores, and links in HTML comments, are left out.

    Raises:
        NotAnEpisodePageError: The page is not a work page.
    """
    soup = BeautifulSoup(page, "html.parser")
    detail = soup.select_one("section.detail")
    heading = detail.select_one(".block_head .title") if isinstance(detail, Tag) else None
    if not isinstance(heading, Tag):
        msg = f"no work page at {url}."
        raise NotAnEpisodePageError(msg)
    episodes: dict[str, str] = {}
    for block in soup.select("div.to_viewer"):
        # A block is captions and buttons in turn; a button belongs to the caption before it.
        caption = ""
        for element in block.find_all(["p", "a"]):
            if element.name == "p" and "title" in (element.get("class") or []):
                caption = element.get_text(strip=True)
            elif element.name == "a" and element.has_attr("href"):
                canonical = episode_url(urljoin(url, str(element["href"])))
                if canonical is not None:
                    episodes.setdefault(canonical, caption)
    return Work(url=url, title=heading.get_text(strip=True), episodes=episodes)


def parse_book(text: str) -> Book:
    """Read a FLIPPER `book.xml`.

    Args:
        text: The file.

    Returns:
        The book: title, page ids, size and magnification.

    Raises:
        NotAnEpisodePageError: The file lists no page.
    """
    fields: dict[str, str] = {}
    for match in _XML_FIELD.finditer(text):
        value = match["value"]
        cdata = _CDATA.match(value)
        fields.setdefault(match["tag"], (cdata["body"] if cdata else html.unescape(value)).strip())
    page_ids = tuple(part.strip() for part in fields.get("data", "").split(",") if part.strip())
    if not page_ids:
        msg = "book.xml lists no page."
        raise NotAnEpisodePageError(msg)
    return Book(
        version=fields.get("flipperVersion", ""),
        title=fields.get("bookTitle", ""),
        page_ids=page_ids,
        magnification=max(_int(fields.get("maxMagnification")), 1),
        width=_int(fields.get("pageWidth")),
        height=_int(fields.get("pageHeight")),
        slice_width=_int(fields.get("sliceWidth")),
        slice_height=_int(fields.get("sliceHeight")),
        fields=fields,
    )


def parse_slides(text: str) -> list[str]:
    """The `url` of every `<slide>` of SMOOZY's `xml/data.xml`, in order."""
    return [match["url"] for match in _SLIDE.finditer(text)]


def parse_book_conf(text: str) -> dict[str, str]:
    """Read SMOOZY's `data/book.conf`, a `KEY=value&KEY=value` string."""
    conf: dict[str, str] = {}
    for part in text.strip().split("&"):
        key, sep, value = part.partition("=")
        if sep and key:
            conf[key.strip()] = value.strip()
    return conf


def swf_jpegs(data: bytes) -> list[bytes]:
    """The JPEG files a SWF carries, in the order they are defined.

    Args:
        data: The SWF file (`FWS` or zlib-compressed `CWS`).

    Returns:
        One JPEG per `DefineBitsJPEG2`/`DefineBitsJPEG3` tag, the erroneous
        EOI/SOI pair old authoring tools put in front stripped.

    Raises:
        GetjmangaError: The file is not a SWF this reads.
    """
    signature = data[:3]
    if signature == b"CWS":
        body = zlib.decompress(data[8:])
    elif signature == b"FWS":
        body = data[8:]
    else:
        msg = f"not a SWF file (signature {signature!r})."
        raise GetjmangaError(msg)
    # Header: a RECT (its bit width in the top five bits), frame rate and frame count.
    rect_bits = 5 + 4 * (body[0] >> 3)
    position = (rect_bits + 7) // 8 + 4
    jpegs: list[bytes] = []
    while position + 2 <= len(body):
        (code_and_length,) = struct.unpack_from("<H", body, position)
        position += 2
        code, length = code_and_length >> 6, code_and_length & _LONG_TAG_LENGTH
        if length == _LONG_TAG_LENGTH:
            (length,) = struct.unpack_from("<I", body, position)
            position += 4
        payload = body[position : position + length]
        position += length
        if code == _TAG_END:
            break
        if code == _DEFINE_BITS_JPEG2:
            jpeg = payload[2:]
        elif code == _DEFINE_BITS_JPEG3:
            (alpha_offset,) = struct.unpack_from("<I", payload, 2)
            jpeg = payload[6 : 6 + alpha_offset]
        else:
            continue
        jpegs.append(jpeg.removeprefix(_JPEG_ERRONEOUS_HEADER))
    return jpegs


def stitch(tiles: list[Image.Image], columns: int, size: tuple[int, int]) -> Image.Image:
    """Lay FLIPPER's tiles of one magnified page back out, row by row.

    Args:
        tiles: The tiles in file order (`1.jpg`, `2.jpg`, ...), row by row.
        columns: How many tiles make a row.
        size: The magnified page's size.

    Returns:
        The page.
    """
    out = Image.new("RGB", size)
    if not tiles:
        return out
    tile_width, tile_height = tiles[0].size
    for index, tile in enumerate(tiles):
        out.paste(tile.convert("RGB"), ((index % columns) * tile_width, (index // columns) * tile_height))
    return out


def tile_grid(book: Book, scale: int) -> tuple[int, int]:
    """How many columns and rows of tiles a page magnified `scale` times is cut into."""
    if book.slice_width <= 0 or book.slice_height <= 0:
        return 1, 1
    columns = -(-book.width * scale // book.slice_width)
    rows = -(-book.height * scale // book.slice_height)
    return max(columns, 1), max(rows, 1)


def split_title(title: str, series: str) -> str:
    """The episode half of a FLIPPER `bookTitle` that starts with the work's title, else ""."""
    if not series or not title.startswith(series):
        return ""
    return _TITLE_GAP.sub("", title.removeprefix(series)).strip()


def page_title(page: str | bytes) -> str:
    """The `<title>` of a viewer page, "" when it has none."""
    title = BeautifulSoup(page, "html.parser").find("title")
    return title.get_text(strip=True) if isinstance(title, Tag) else ""


def caption_title(caption: str) -> str:
    """The episode name a work page's button caption carries, "" when it names none."""
    name = _CAPTION_SUFFIX.sub("", caption.strip()).strip()
    return "" if name == _GENERIC_CAPTION else name


class Wings(Extractor):
    """Fetch episodes from ウェブマガジン ウィングス.

    An episode URL is a viewer directory; a series URL is a work page, which
    is also where the work's title, the episode names and the order of the
    episodes come from. A work page is fetched once per run.
    """

    NAME = "wings"
    HOSTS = (HOST,)
    URL_FORMS = (
        "https://www.shinshokan.com/webwings/contents/<slug>/",
        "https://www.shinshokan.com/webwings/title<NN>.html",
    )

    def __init__(self, session: Session | None = None) -> None:
        """Build an extractor.

        Args:
            session: A session to reuse. A retrying one is made when omitted.
        """
        super().__init__(session)
        # Work page URL -> what it listed (None when it is gone), so a work page is read once.
        self._works: dict[str, Work | None] = {}
        # SWF URL -> the JPEGs in it, for the SMOOZY episode being read.
        self._swfs: dict[str, list[bytes]] = {}

    @classmethod
    def suitable(cls, url: str) -> bool:
        """Report whether `url` is a viewer directory or a work page of the magazine.

        Args:
            url: The URL to check.

        Returns:
            True for `https://www.shinshokan.com/webwings/contents/<slug>/`
            and `https://www.shinshokan.com/webwings/title<NN>.html`.
        """
        if not super().suitable(url):
            return False
        return episode_url(url) is not None or work_url(url) is not None

    @classmethod
    def is_series(cls, url: str) -> bool:
        """Report whether `url` is a work page.

        Args:
            url: The URL to check.

        Returns:
            True for `https://www.shinshokan.com/webwings/title<NN>.html`.
        """
        return work_url(url) is not None

    def series_urls(self, url: str) -> list[str]:
        """List the episodes a work page opens, first episode first.

        Args:
            url: A work page URL.

        Returns:
            One viewer URL per listed episode, in page order.

        Raises:
            UnsupportedUrlError: The URL is not a work page.
            NotAnEpisodePageError: The page is gone, or lists no episode of its own.
        """
        canonical = work_url(url)
        if canonical is None:
            msg = f"{url} is not a work page."
            raise UnsupportedUrlError(msg)
        work = self._work(canonical)
        if work is None:
            msg = f"{url} is gone (HTTP 404): not a work page."
            raise NotAnEpisodePageError(msg)
        if not work.episodes:
            msg = f"the work at {url} lists no episode on the magazine's own viewer."
            raise NotAnEpisodePageError(msg)
        return list(work.episodes)

    def episode(self, url: str) -> Episode:
        """Read one episode: its titles, its pages and the episode after it.

        Args:
            url: A viewer directory URL.

        Returns:
            The episode. `pages` is empty when the viewer wants a password.

        Raises:
            UnsupportedUrlError: The URL is not a viewer URL.
            NotAnEpisodePageError: The directory is gone (404) or holds no viewer this reads.
        """
        canonical = episode_url(url)
        if canonical is None:
            msg = f"{url} is not an episode page."
            raise UnsupportedUrlError(msg)
        slug = canonical.rstrip("/").rsplit("/", 1)[-1]

        res = self._session.get(canonical, headers=self.HEADERS, timeout=self.TIMEOUT)
        if res.status_code == HTTPStatus.NOT_FOUND:
            msg = f"{url} is gone (HTTP 404): the magazine no longer serves it."
            raise NotAnEpisodePageError(msg)
        res.raise_for_status()
        page = _utf8(res)
        work = self._work_of(canonical, slug)
        if _FLIPPER_MARK.search(page):
            return self._flipper_episode(canonical, slug, work)
        if _SMOOZY_MARK.search(page):
            return self._smoozy_episode(canonical, slug, work, page_title(page))
        msg = f"no viewer on {url}."
        raise NotAnEpisodePageError(msg)

    def image(self, page: Page, episode: Episode) -> Image.Image:
        """Fetch one page, out of the SWF or the tiles it is kept in when it is.

        Args:
            page: The page to fetch.
            episode: The episode the page belongs to.

        Returns:
            The page image.
        """
        headers = {**self.HEADERS, "Referer": episode.url}
        if "swf_index" in page.extra:
            jpegs = self._swfs.get(page.url)
            if jpegs is None:
                jpegs = swf_jpegs(self._get(page.url, headers=headers, timeout=self.IMAGE_TIMEOUT).content)
                self._swfs[page.url] = jpegs
            return Image.open(BytesIO(jpegs[int(page.extra["swf_index"])]))
        if not page.extra.get("sliced"):
            try:
                return self._fetch_image(page.url, headers=headers)
            except HTTPError as error:
                # FLIPPER 3 keeps no whole magnified page; its tiles are all there is.
                if error.response is None or error.response.status_code != HTTPStatus.NOT_FOUND:
                    raise
        return stitch(
            [self._fetch_image(tile, headers=headers) for tile in self._tile_urls(page)],
            int(page.extra["columns"]),
            (page.width, page.height),
        )

    def _flipper_episode(self, canonical: str, slug: str, work: Work | None) -> Episode:
        """Read a FLIPPER export: `book.xml` and the pages it names."""
        book = parse_book(_utf8(self._get(f"{canonical}book.xml", headers={**self.HEADERS, "Referer": canonical})))
        series_title, episode_title = self._titles(
            work,
            canonical,
            slug,
            series=book.title,
            episode=split_title(book.title, work.title) if work else "",
        )
        scale = book.magnification
        columns, rows = tile_grid(book, scale)
        metadata: dict[str, Any] = {
            "viewer": "flipper",
            "slug": slug,
            "work_url": work.url if work else None,
            "book": book.fields,
        }
        if book.password_protected:
            return Episode(
                url=canonical,
                series_title=series_title,
                episode_title=episode_title,
                next_url=self._next_url(work, canonical),
                metadata={**metadata, "locked": True},
            )
        pages = tuple(
            Page(
                url=f"{canonical}page{page_id}/x{scale}/x{scale}.jpg"
                if scale > 1
                else f"{canonical}page{page_id}/x1.jpg",
                width=book.width * scale,
                height=book.height * scale,
                extra={
                    "page_id": page_id,
                    "scale": scale,
                    "sliced": book.sliced and scale > 1,
                    "columns": columns,
                    "rows": rows,
                },
            )
            for page_id in book.page_ids
        )
        return Episode(
            url=canonical,
            series_title=series_title,
            episode_title=episode_title,
            pages=pages,
            next_url=self._next_url(work, canonical),
            metadata=metadata,
        )

    def _smoozy_episode(self, canonical: str, slug: str, work: Work | None, title: str) -> Episode:
        """Read a SMOOZY export: `xml/data.xml`, then the JPEGs of every spread's SWF."""
        headers = {**self.HEADERS, "Referer": canonical}
        slides = parse_slides(_utf8(self._get(f"{canonical}xml/data.xml", headers=headers)))
        if not slides:
            msg = f"{canonical}xml/data.xml lists no spread."
            raise NotAnEpisodePageError(msg)
        conf_res = self._session.get(f"{canonical}data/book.conf", headers=headers, timeout=self.TIMEOUT)
        conf = parse_book_conf(_utf8(conf_res)) if conf_res.ok else {}
        right_to_left = conf.get("DIRECTION", "right").lower() == "right"

        self._swfs.clear()
        pages: list[Page] = []
        for slide in slides:
            swf_url = urljoin(canonical, slide)
            jpegs = swf_jpegs(self._get(swf_url, headers=headers, timeout=self.IMAGE_TIMEOUT).content)
            self._swfs[swf_url] = jpegs
            # The SWF holds the spread left page first; a right-to-left book reads the right one first.
            indices = range(len(jpegs) - 1, -1, -1) if right_to_left else range(len(jpegs))
            pages.extend(
                Page(
                    url=swf_url,
                    width=_int(conf.get("PAGEWIDTH")),
                    height=_int(conf.get("PAGEHEIGHT")),
                    extra={"swf_index": index},
                )
                for index in indices
            )
        # The page's `<title>` is `<work>／<author>`; it names no episode.
        series_title, episode_title = self._titles(work, canonical, slug, series=title.split("／", 1)[0].strip())
        return Episode(
            url=canonical,
            series_title=series_title,
            episode_title=episode_title,
            pages=tuple(pages),
            next_url=self._next_url(work, canonical),
            metadata={
                "viewer": "smoozy",
                "slug": slug,
                "work_url": work.url if work else None,
                "title": title,
                "slides": slides,
                "conf": conf,
            },
        )

    def _work(self, url: str) -> Work | None:
        """Read a work page, once per URL. None when the page is gone."""
        if url not in self._works:
            res = self._session.get(url, headers=self.HEADERS, timeout=self.TIMEOUT)
            if res.status_code == HTTPStatus.NOT_FOUND:
                self._works[url] = None
            else:
                res.raise_for_status()
                self._works[url] = parse_work(res.content, str(res.url or url))
        return self._works[url]

    def _work_of(self, canonical: str, slug: str) -> Work | None:
        """The work page an episode belongs to: one already read that lists it, else the one its slug names."""
        for work in self._works.values():
            if work is not None and canonical in work.episodes:
                return work
        match = _SLUG_WORK.match(slug)
        if match is None:
            return None
        try:
            return self._work(f"{BASE_URL}{match['id']}.html")
        except HTTPError:
            return None

    @staticmethod
    def _titles(work: Work | None, canonical: str, slug: str, *, series: str, episode: str = "") -> tuple[str, str]:
        """The series and episode titles.

        The work page names the series; the episode is what the viewer's title
        adds to it (`episode`), else what the button caption said, else the
        slug. Without a work page the viewer's own `series` stands, and the
        slug names the episode.
        """
        if work is None or not work.title:
            return series or slug, slug
        return work.title, episode or caption_title(work.episodes.get(canonical, "")) or slug

    @staticmethod
    def _next_url(work: Work | None, canonical: str) -> str | None:
        """The listed episode after `canonical`, or None when it is the last (or unlisted)."""
        if work is None:
            return None
        urls = list(work.episodes)
        if canonical not in urls:
            return None
        index = urls.index(canonical) + 1
        return urls[index] if index < len(urls) else None

    @staticmethod
    def _tile_urls(page: Page) -> Iterator[str]:
        """The tile files of a magnified FLIPPER 3 page: `<dir>/1.jpg`, `<dir>/2.jpg`, ..."""
        directory = page.url.rsplit("/", 1)[0]
        count = int(page.extra["columns"]) * int(page.extra["rows"])
        return (f"{directory}/{number}.jpg" for number in range(1, count + 1))


def _utf8(res: Response) -> str:
    """The body as UTF-8, which the site writes everything in but rarely says so in a header."""
    return res.content.decode("utf-8", errors="replace")


def _int(value: str | None) -> int:
    """`value` as an int, 0 when it is not one."""
    try:
        return int(float(value or 0))
    except ValueError:
        return 0
