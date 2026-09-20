"""最前線 (星海社): the ツイ4 four-panel strips and the comics reader, both plain HTML.

The site is static files behind no login. Three kinds of page read here:

- **ツイ4** (`/comics/twi4/<work>/<NNNN>.html`): one strip per page, served
  as `article.comic img`. A work opens a few strips at the start and the
  newest few (`PublishingRange` in the work's `index.js`); a closed strip's
  page keeps the title and shows `第N話は公開を終了しました` where the image
  was. Every page carries `nav#backnumbers`, the whole work newest first,
  which is where the next strip comes from. A ツイ4新人賞座談会 round
  (`zadankai-YYYYMM`) is the same shape, with `cNNN.html` comment pages in
  between (not episodes) and `NNNN-all.html` pages holding every strip of an
  entry.
- **The reader** (`/works/comics/<work>/<NN>/01.html`, and 4ページマンガ最前線
  at `/special/4pages-comics/<work>/<NN>.html`): a Bibi-based viewer whose
  page images stand in the HTML as `div.item > noscript > img`, one per page
  in reading order. `/comics/<work>/meta.json` carries an `index` bit string,
  one bit per volume, `1` for the volumes still served -- the viewer's own
  "next" is the next `1`. An expired volume is gone altogether (404).
- **The legacy reader** (`data-szsr-mode="text legacy"`, `青春離婚`): every
  page is cut into four `p.image img` strips named `<page>.<strip>.jpg`;
  `image()` glues the strips of a page back together with `stitch()`.

Images are ordinary files: not scrambled, no Referer or cookie check. There
is no sign-in anywhere on the site.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from http import HTTPStatus
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from bs4.element import Tag
from PIL import Image

from getjmanga.errors import NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractor import Episode, Extractor, Page

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from requests import Response, Session

# A ツイ4 strip page, or the `-all` page of a 座談会 entry.
_TWI4_EPISODE = re.compile(r"^/comics/twi4/(?P<work>[^/]+)/(?P<number>\d{4})(?P<all>-all)?\.html$")
# A ツイ4 work page. `/comics/twi4/special/<book>/` is a book advert, not a work.
_TWI4_WORK = re.compile(r"^/comics/twi4/(?!special/?$)(?P<work>[^/.]+)/?$")
# A reader volume, and the work page that lists them.
_READER_EPISODE = re.compile(r"^/works/comics/(?P<work>[^/]+)/(?P<number>\d+)/01\.html$")
_READER_WORK = re.compile(r"^/comics/(?!twi4/?$)(?P<work>[^/.]+)/?$")
# A 4ページマンガ最前線 volume, and its work page.
_FOURPAGES_EPISODE = re.compile(r"^/special/4pages-comics/(?P<work>[^/]+)/(?P<number>\d+)\.html$")
_FOURPAGES_WORK = re.compile(r"^/special/4pages-comics/(?P<work>[^/.]+)/?$")

# `<title>` of a ツイ4 page: `<strip> -『<work>』<author> | ツイ４ | 最前線`, the strip part missing on some.
_TWI4_TITLE = re.compile(r"^(?:(?P<episode>.*?) -)?『(?P<series>.+)』(?P<author>.*)$")
# `<h1>` of a reader page: `<author>『<work>』<volume> <credits> | 最前線`.
_READER_TITLE = re.compile(r"^(?P<author>[^『]*)『(?P<series>.+?)』(?P<rest>.*)$")
# Where the credits start in what follows the work title.
_CREDITS = re.compile(
    r"\s*(?:原作|原案|著者|作画|漫画|監修|脚本|構成|キャラクターデザイン原案|キャラクター原案)[／：/:].*$",  # noqa: RUF001 (the site writes a fullwidth colon)
)
# A strip of a legacy reader page: `<page>.<strip>.jpg`.
_STRIP_NAME = re.compile(r"^(?P<page>\d+)\.(?P<strip>\d+)\.\w+$")
# The `Format` of each item of a ツイ4 work's `index.js`; a trailing `0` marks a closed one.
_FORMAT = re.compile(r'Format\s*:\s*"(?P<kind>[a-z]+?)(?P<closed>0?)"')
# What every reader heading ends in.
_SITE_SUFFIX = " | 最前線"


@dataclass(frozen=True)
class Document:
    """What an episode page says, whichever kind it is."""

    #: `"twi4"`, `"reader"` or `"legacy"`.
    kind: str
    #: The work's title.
    series_title: str
    #: The strip's or volume's title.
    episode_title: str
    #: The page images in reading order; a legacy page is a tuple of its strips.
    pages: tuple[tuple[str, ...], ...]
    #: The page's `<title>`.
    title: str
    #: ツイ4: the strip numbers `nav#backnumbers` links, newest first.
    listed: tuple[int, ...]
    #: ツイ4: the page says the strip is no longer public.
    closed: bool


def parse_twi4_page(html: str | bytes, url: str) -> Document:
    """Read a ツイ4 strip page.

    Args:
        html: The page.
        url: The URL it came from, to resolve the image against.

    Returns:
        What the page says. `pages` is empty for a closed strip.

    Raises:
        NotAnEpisodePageError: The page carries no `article.comic`.
    """
    soup = BeautifulSoup(html, "html.parser")
    articles = soup.select("article.comic")
    if not articles:
        msg = f"no strip on {url}."
        raise NotAnEpisodePageError(msg)
    images = [
        urljoin(url, str(img["src"]))
        for article in articles
        for img in article.select("div.pgroup > p > img[src]")
        if isinstance(img, Tag)
    ]
    title = _text(soup.title)
    match = _TWI4_TITLE.match(title.split(" | ", 1)[0])
    series_title = match["series"] if match else ""
    episode_title = (match["episode"] or "") if match else ""
    if not episode_title:
        alt = articles[0].select_one("div.pgroup > p > img[alt]")
        episode_title = str(alt["alt"]) if isinstance(alt, Tag) else ""
    path_match = _TWI4_EPISODE.match(urlparse(url).path)
    if not episode_title and path_match:
        episode_title = f"#{path_match['number']}"
    work = path_match["work"] if path_match else ""
    listed = tuple(
        int(link["number"])
        for anchor in soup.select("nav#backnumbers a[href]")
        if (link := _TWI4_EPISODE.match(urlparse(urljoin(url, str(anchor["href"]))).path))
        and link["work"] == work
        and not link["all"]
    )
    return Document(
        kind="twi4",
        series_title=series_title or work,
        episode_title=episode_title,
        pages=tuple((src,) for src in images),
        title=title,
        listed=listed,
        closed=not images and "公開を終了" in articles[0].get_text(),
    )


def parse_reader_page(html: str | bytes, url: str) -> Document:
    """Read a reader volume page, of the current or the legacy layout.

    Args:
        html: The page.
        url: The URL it came from, to resolve the images against.

    Returns:
        What the page says.

    Raises:
        NotAnEpisodePageError: The page carries no `data-bibi-book` viewer.
    """
    soup = BeautifulSoup(html, "html.parser")
    body = soup.body
    book = str(body["data-bibi-book"]) if isinstance(body, Tag) and body.has_attr("data-bibi-book") else ""
    if not book:
        msg = f"no reader on {url}."
        raise NotAnEpisodePageError(msg)
    legacy = "legacy" in str(body.get("data-szsr-mode", "")) if isinstance(body, Tag) else False
    if legacy:
        # Only what lies under the volume's own directory is a page: the legacy
        # layout also puts a `continue.png` and the work's banner in `p.image`.
        prefix = "/" + book.strip("/") + "/"
        images = [
            src
            for img in soup.select("p.image img[src]")
            if isinstance(img, Tag) and urlparse(src := urljoin(url, str(img["src"]))).path.startswith(prefix)
        ]
    else:
        images = [urljoin(url, str(img["src"])) for img in soup.select("div.item img[src]") if isinstance(img, Tag)]
    title = _text(soup.title)
    heading = _text(soup.select_one("article.book h1")) or title
    series_title, episode_title = split_reader_title(heading)
    path = urlparse(url).path
    path_match = _READER_EPISODE.match(path) or _FOURPAGES_EPISODE.match(path)
    if not episode_title and path_match:
        episode_title = f"第{int(path_match['number'])}回"
    return Document(
        kind="legacy" if legacy else "reader",
        series_title=series_title or book.split("/")[-2],
        episode_title=episode_title,
        pages=tuple(group_strips(images)) if legacy else tuple((src,) for src in images),
        title=title,
        listed=(),
        closed=False,
    )


def split_reader_title(heading: str) -> tuple[str, str]:
    """Split a reader heading into the work's title and the volume's.

    `シオミヤイルカ『非実在推理少女あ〜や』第一話「コンダラ殺人事件」第三回 原作／錦メガネ | 最前線`
    gives `("非実在推理少女あ〜や", "第一話「コンダラ殺人事件」第三回")`: the author
    before the brackets and the credits after the volume are dropped.

    Args:
        heading: The page's `<h1>` or `<title>`.

    Returns:
        The work title and the volume title, either empty when not found.
    """
    heading = heading.strip()
    heading = heading.removesuffix(_SITE_SUFFIX).strip()
    match = _READER_TITLE.match(heading)
    if match is None:
        return "", ""
    return match["series"].strip(), _CREDITS.sub("", match["rest"]).strip()


def group_strips(urls: Iterable[str]) -> list[tuple[str, ...]]:
    """Group the strips of a legacy reader page by the page they cut.

    Strips are named `<page>.<strip>.jpg`; consecutive ones with the same
    `<page>` make one page. An image named otherwise is a page of its own.

    Args:
        urls: The image URLs in reading order.

    Returns:
        One tuple of strip URLs per page.
    """
    pages: list[tuple[str, ...]] = []
    current: list[str] = []
    current_key: str | None = None
    for url in urls:
        match = _STRIP_NAME.match(urlparse(url).path.rsplit("/", 1)[-1])
        key = match["page"] if match else None
        if key is None or key != current_key or not current:
            if current:
                pages.append(tuple(current))
            current = [url]
            current_key = key
        else:
            current.append(url)
    if current:
        pages.append(tuple(current))
    return pages


def stitch(strips: Sequence[Image.Image]) -> Image.Image:
    """Glue the strips of a page back together, top to bottom.

    Args:
        strips: The strips in reading order.

    Returns:
        A new image as wide as the widest strip and as tall as all of them.
    """
    width = max(strip.width for strip in strips)
    page = Image.new("RGB", (width, sum(strip.height for strip in strips)), (255, 255, 255))
    top = 0
    for strip in strips:
        page.paste(strip.convert("RGB"), (0, top))
        top += strip.height
    return page


def parse_twi4_index(js: str) -> list[bool] | None:
    """Read which strips a ツイ4 work's `index.js` says are open.

    `t4.Meta.Items` lists the strips in order, a `Format` each: `r`/`x` for
    a strip, `c` for a comment page, a trailing `0` for a closed strip. The
    viewer pads a list shorter than `TotalEpisodes` with closed ones.

    Args:
        js: The script.

    Returns:
        One flag per strip, in strip order, True when open; None when the script
        lists no strip at all.
    """
    flags = [not match["closed"] for match in _FORMAT.finditer(js) if match["kind"][0] in "rx"]
    return flags or None


def _text(tag: Tag | None) -> str:
    return tag.get_text(strip=True) if isinstance(tag, Tag) else ""


class Saizensen(Extractor):
    """Fetch episodes from 最前線 (sai-zen-sen.jp), ツイ4 included."""

    NAME = "saizensen"
    HOSTS = ("sai-zen-sen.jp",)
    URL_FORMS = (
        "https://sai-zen-sen.jp/comics/twi4/<work>/<nnnn>.html",
        "https://sai-zen-sen.jp/comics/twi4/<work>/",
        "https://sai-zen-sen.jp/works/comics/<work>/<nn>/01.html",
        "https://sai-zen-sen.jp/comics/<work>/",
        "https://sai-zen-sen.jp/special/4pages-comics/<work>/<nn>.html",
        "https://sai-zen-sen.jp/special/4pages-comics/<work>/",
    )

    def __init__(self, session: Session | None = None) -> None:
        """Build an extractor.

        Args:
            session: A session to reuse. A retrying one is made when omitted.
        """
        super().__init__(session)
        # Work URL -> the `index` bit string of its `meta.json` ("" when it has none).
        self._indexes: dict[str, str] = {}
        # ツイ4 work URL -> one flag per strip from its `index.js`, True when open (None when it has none).
        self._flags: dict[str, list[bool] | None] = {}

    @classmethod
    def suitable(cls, url: str) -> bool:
        """Report whether the extractor takes `url`.

        Args:
            url: The URL to check.

        Returns:
            True for an https episode or work URL of one of the three sections.
        """
        return super().suitable(url) and (cls._episode_kind(url) is not None or cls.is_series(url))

    @classmethod
    def is_series(cls, url: str) -> bool:
        """Report whether `url` is a work page.

        Args:
            url: The URL to check.

        Returns:
            True for a ツイ4, reader or 4ページマンガ work page.
        """
        path = urlparse(url).path
        return any(pattern.match(path) for pattern in (_TWI4_WORK, _READER_WORK, _FOURPAGES_WORK))

    def series_urls(self, url: str) -> list[str]:
        """List the episodes a work page links, oldest first.

        A ツイ4 work's `index.js` says which strips are open, and is what the
        list comes from when it is there -- the work page lists every strip
        the work ever ran, and a series run should not fetch a thousand pages
        to skip the closed ones. A reader work page lists only the volumes
        still served.

        Args:
            url: A work page URL.

        Returns:
            One episode URL per open episode, in number order.

        Raises:
            UnsupportedUrlError: The URL is not a work page.
            NotAnEpisodePageError: The page links no episode.
        """
        path = urlparse(url).path
        if _TWI4_WORK.match(path):
            episode_pattern = _TWI4_EPISODE
        elif _READER_WORK.match(path):
            episode_pattern = _READER_EPISODE
        elif _FOURPAGES_WORK.match(path):
            episode_pattern = _FOURPAGES_EPISODE
        else:
            msg = f"{url} is not a work page."
            raise UnsupportedUrlError(msg)
        work_url = self._with_slash(url)
        flags = self._twi4_flags(work_url) if episode_pattern is _TWI4_EPISODE else None
        if flags is not None:
            numbered = {n: urljoin(work_url, f"{n:04d}.html") for n, is_open in enumerate(flags, 1) if is_open}
        else:
            soup = BeautifulSoup(self._page(work_url).content, "html.parser")
            work = self._work_id(work_url)
            numbered = {}
            for anchor in soup.select("a[href]"):
                absolute = urljoin(work_url, str(anchor["href"]).split("#", 1)[0])
                match = episode_pattern.match(urlparse(absolute).path)
                if match and match["work"] == work and not match.groupdict().get("all"):
                    numbered.setdefault(int(match["number"]), absolute)
        if not numbered:
            msg = f"the work at {url} lists no open episode."
            raise NotAnEpisodePageError(msg)
        return [numbered[number] for number in sorted(numbered)]

    def episode(self, url: str) -> Episode:
        """Read one strip or volume and list its pages.

        Args:
            url: The episode URL.

        Returns:
            The episode. `pages` is empty for a ツイ4 strip the work has closed;
            `next_url` names the next open strip, or the next volume
            `meta.json` still lists.

        Raises:
            NotAnEpisodePageError: The URL is not an episode URL, the page is gone
                (404) or it carries no viewer.
        """
        kind = self._episode_kind(url)
        if kind is None:
            msg = f"{url} is not an episode page."
            raise NotAnEpisodePageError(msg)
        res = self._page(url)
        url = str(res.url or url)
        if kind is _TWI4_EPISODE:
            document = parse_twi4_page(res.content, url)
            next_url = self._twi4_next(url, document.listed, self._twi4_flags(urljoin(url, ".")))
        else:
            document = parse_reader_page(res.content, url)
            next_url = self._reader_next(url, kind)
        return Episode(
            url=url,
            series_title=document.series_title,
            episode_title=document.episode_title,
            pages=tuple(
                Page(url=strips[0], extra={"strips": list(strips)} if len(strips) > 1 else {})
                for strips in document.pages
            ),
            next_url=next_url,
            metadata={
                "kind": document.kind,
                "title": document.title,
                "closed": document.closed,
                "pages": [list(strips) for strips in document.pages],
            },
        )

    def image(self, page: Page, episode: Episode) -> Image.Image:
        """Fetch one page, gluing its strips together on a legacy volume.

        Args:
            page: The page to fetch.
            episode: The episode the page belongs to.

        Returns:
            The page image.
        """
        headers = {**self.HEADERS, "Referer": episode.url}
        strips = [str(src) for src in page.extra.get("strips", [])] or [page.url]
        if len(strips) == 1:
            return self._fetch_image(strips[0], headers=headers)
        return stitch([self._fetch_image(src, headers=headers) for src in strips])

    # --- helpers ------------------------------------------------------------------------

    @staticmethod
    def _episode_kind(url: str) -> re.Pattern[str] | None:
        """The episode pattern `url` matches, if any."""
        path = urlparse(url).path
        for pattern in (_TWI4_EPISODE, _READER_EPISODE, _FOURPAGES_EPISODE):
            if pattern.match(path):
                return pattern
        return None

    @staticmethod
    def _with_slash(url: str) -> str:
        """`url` without query or fragment, ending in a slash as the site serves work pages."""
        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}/"

    @staticmethod
    def _work_id(work_url: str) -> str:
        """The work's id: the last path segment of its work page URL."""
        return urlparse(work_url).path.rstrip("/").rsplit("/", 1)[-1]

    def _page(self, url: str) -> Response:
        """GET a page, turning a 404 into `NotAnEpisodePageError`."""
        res = self._session.get(url, headers=self.HEADERS, timeout=self.TIMEOUT)
        if res.status_code == HTTPStatus.NOT_FOUND:
            msg = f"{url} is gone (HTTP 404)."
            raise NotAnEpisodePageError(msg)
        res.raise_for_status()
        return res

    def _twi4_flags(self, work_url: str) -> list[bool] | None:
        """Which strips a ツイ4 work's `index.js` says are open, fetched once per work; None when it cannot tell."""
        if work_url not in self._flags:
            res = self._session.get(urljoin(work_url, "index.js"), headers=self.HEADERS, timeout=self.TIMEOUT)
            self._flags[work_url] = parse_twi4_index(res.text) if res.ok else None
        return self._flags[work_url]

    @staticmethod
    def _twi4_next(url: str, listed: Iterable[int], flags: Sequence[bool] | None) -> str | None:
        """The next open strip after the current one.

        `index.js` is the source when it is there: it is current and says
        which strips are open, so the closed ones in between are skipped and a
        bulk run does not fetch a thousand pages to skip them. Without it, the
        next strip `nav#backnumbers` lists is named, closed or not -- that
        list is baked into the static page and can be behind.
        """
        match = _TWI4_EPISODE.match(urlparse(url).path)
        if match is None:
            return None
        current = int(match["number"])
        if flags is not None:
            following = [number for number in range(current + 1, len(flags) + 1) if flags[number - 1]]
        else:
            following = [number for number in listed if number > current]
        if not following:
            return None
        return urljoin(url, f"{min(following):04d}.html")

    def _reader_next(self, url: str, kind: re.Pattern[str]) -> str | None:
        """The next volume the work's `meta.json` still lists, if any."""
        match = kind.match(urlparse(url).path)
        if match is None:
            return None
        origin = self._origin(url)
        if kind is _FOURPAGES_EPISODE:
            work_url = f"{origin}/special/4pages-comics/{match['work']}/"
            volume_path = "{number:02d}.html"
        else:
            work_url = f"{origin}/comics/{match['work']}/"
            volume_path = "/works/comics/" + match["work"] + "/{number:02d}/01.html"
        index = self._index(work_url)
        current = int(match["number"])
        for number in range(current + 1, len(index) + 1):
            if index[number - 1] == "1":
                return urljoin(work_url, volume_path.format(number=number))
        return None

    def _index(self, work_url: str) -> str:
        """The `index` bit string of a work's `meta.json`, fetched once per work; "" without one."""
        if work_url not in self._indexes:
            res = self._session.get(urljoin(work_url, "meta.json"), headers=self.HEADERS, timeout=self.TIMEOUT)
            index = ""
            if res.ok:
                try:
                    payload: Any = res.json()
                    index = str(payload["sai-zen-sen"]["index"])
                except (ValueError, KeyError, TypeError):
                    index = ""
            self._indexes[work_url] = index
        return self._indexes[work_url]
