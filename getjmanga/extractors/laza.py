"""まんだらけWEBコミック ラザ (Mandarake's web comic site), two generations of static pages.

No viewer script, no API, no scrambling, no account. The site names itself
`http://laza.mandarake.co.jp/` in every link it writes, but port 80 does not
answer any more while https does, so `suitable()` takes both schemes and
every request goes out over https. Two page layouts coexist under one host:

- The older works live at `/comicNNN/`. The work's index lists its updates
  newest first as `pN.html`, each a page of `<iframe>`s, one per strip; the
  frame `NNN.html` shows the strip's caption in `h1` and its image in
  `.img_block`. An update that is no longer public is listed as 公開終了 with
  a link to the comics' product page instead, and its `pN.html` is gone.
- The newer works (`kimono-lolita`, `BTP_plus`) are Movable Type blogs. A
  page is an entry `/<work>/manga/<N>.html` with one image in `.ohanashi`
  and a `<link rel="next">` to the following page; an episode is a run of
  pages whose `h1` carries the same title after the page counter (`001`,
  `002`, ... and a fullwidth colon). The work's `list.html` links the first
  page of every episode, newest first, so it both names the episodes and
  bounds them. An episode that was
  taken down links the product page there and its pages are gone; the
  `rel="next"` chain skips over the hole.

A few pages (`/BTP_plus/`, `/BTP_plus/list.html`) embed a feed that never
finishes: the page arrives whole in the first tenth of a second and the
connection then hangs open. Those pages are read as a stream and whatever
arrived before the stall is used.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup
from bs4.element import Tag
from httpx import Timeout, TransportError

from getjmanga.errors import NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractor import Episode, Extractor, Page, neighbours, ordinal

if TYPE_CHECKING:
    from collections.abc import Mapping

    from httpx import Client, Response

#: Seconds to wait for the next byte of a page before taking what arrived as the page.
STALL_TIMEOUT = 10

# A Movable Type page: `/<work>/manga/<page>.html`. Most pages are numbered,
# an announcement slipped into the chain is named (`4kan.html`).
_MT_PAGE_PATH = re.compile(r"^/(?P<work>[\w-]+)/manga/(?P<page>[\w-]+)\.html$")
# An update of an older work: `/<work>/p<N>.html`.
_OLD_EPISODE_PATH = re.compile(r"^/(?P<work>[\w-]+)/p(?P<number>\d+)\.html$")
# A work's front page, either generation, or the Movable Type episode list.
_SERIES_PATH = re.compile(r"^/(?P<work>[\w-]+)(?:/(?:index\.html|list\.html)?)?$")
# A Movable Type page title: a page counter, the episode's title, a page mark on some works.
_MT_TITLE = re.compile(r"^\s*(?:\d+\s*[:\uff1a]\s*)?(?P<title>.*?)(?:\s*《\d+》)?\s*$", re.DOTALL)
# An older work's `<title>`: the work in 『』, then the author, then the site after a `|`.
_OLD_SERIES_TITLE = re.compile(r"『(?P<title>.+?)』\s*(?P<author>[^|]*)")


@dataclass(frozen=True)
class MtPage:
    """What one Movable Type page says."""

    #: The page URL, https.
    url: str
    #: The `h1`: the page counter, a fullwidth colon, the episode's title.
    title: str
    #: The images in `.ohanashi`, in order.
    images: tuple[str, ...]
    #: The `rel="next"` page, https, or None on the last page.
    next_url: str | None
    #: The work's title, from `og:site_name`.
    series_title: str

    @property
    def episode_title(self) -> str:
        """The title without its page counter and page mark."""
        return episode_title(self.title)


@dataclass(frozen=True)
class MtListing:
    """The episodes a Movable Type work's `list.html` links, oldest first."""

    #: The first page of each episode, https, oldest first.
    starts: tuple[str, ...] = ()
    #: First page -> the label the list gives the episode (`第1話`).
    labels: Mapping[str, str] = field(default_factory=dict)

    def start_of(self, url: str) -> str | None:
        """The first page of the episode a numbered page belongs to, or None when unlisted.

        Args:
            url: A page URL.

        Returns:
            The greatest numbered start at or before the page, when there is one.
        """
        number = _page_number(url)
        if number is None:
            return url if url in self.starts else None
        candidates = [
            (start_number, start)
            for start in self.starts
            if (start_number := _page_number(start)) is not None and start_number <= number
        ]
        return max(candidates)[1] if candidates else None

    def neighbours_of(self, start: str) -> tuple[str | None, str | None]:
        """The first pages of the episodes either side of the one starting at `start`, None at either end."""
        return neighbours(self.starts, start)

    def number_of(self, start: str) -> int | None:
        """Where the episode starting at `start` stands in the list, counted from 1; None when unlisted."""
        return ordinal(self.starts, start)


@dataclass(frozen=True)
class OldEpisodePage:
    """What an older work's `pN.html` says."""

    #: The strip frames, https, in reading order.
    frames: tuple[str, ...]
    #: The previous update the arrow points at, or None.
    prev_url: str | None
    #: The work's title, from the `<title>`.
    series_title: str
    #: The next update the page itself links, https, or None.
    next_url: str | None
    #: The author, named after the title in the `<title>`.
    writer: str = ""


@dataclass(frozen=True)
class OldStrip:
    """What one strip frame of an older work says."""

    #: The frame URL, https.
    url: str
    #: The caption in `h1`: `191 ふざけてはいない。本当の愛だ。`.
    caption: str
    #: The images in `.img_block`, https, in order.
    images: tuple[str, ...]


@dataclass(frozen=True)
class OldListing:
    """The updates an older work's index links, oldest first."""

    #: The update pages, https, oldest first.
    urls: tuple[str, ...] = ()
    #: Update page -> the date the index shows for it.
    dates: Mapping[str, str] = field(default_factory=dict)

    def neighbours_of(self, url: str) -> tuple[str | None, str | None]:
        """The updates either side of `url`, None at either end."""
        return neighbours(self.urls, url)

    def number_of(self, url: str) -> int | None:
        """Where `url` stands among the updates, counted from 1; None when unlisted."""
        return ordinal(self.urls, url)


def read_body(res: Response) -> bytes:
    """Read a streamed response, keeping what arrived if the connection stalls.

    Args:
        res: A response opened with `Client.stream()`.

    Returns:
        The body, or as much of it as the site sent before hanging.

    Raises:
        httpx.TransportError: Nothing arrived before the stall.
    """
    body = bytearray()
    try:
        # Chunks as they arrive, so the stall costs only the read timeout.
        for chunk in res.iter_bytes():
            body += chunk
    except TransportError:
        if not body:
            raise
    return bytes(body)


def episode_title(page_title: str) -> str:
    """Strip the page counter and page mark off a Movable Type page title.

    Args:
        page_title: The `h1` of a page: `826`, a fullwidth colon, `負け人類に恩を売れ《1》`.

    Returns:
        The episode's title: `負け人類に恩を売れ`.
    """
    match = _MT_TITLE.match(page_title)
    return match["title"] if match else page_title.strip()


def parse_mt_page(html: str | bytes, url: str) -> MtPage:
    """Read a Movable Type page.

    Args:
        html: The page.
        url: The URL it came from, to resolve links against.

    Returns:
        What the page says.

    Raises:
        NotAnEpisodePageError: The page carries no comic image.
    """
    soup = BeautifulSoup(html, "html.parser")
    block = soup.select_one(".ohanashi")
    images = tuple(_https(urljoin(url, str(img["src"]))) for img in (block.find_all("img", src=True) if block else []))
    if not images:
        msg = f"no comic on {url}."
        raise NotAnEpisodePageError(msg)
    next_link = soup.find("link", rel="next", href=True)
    if not isinstance(next_link, Tag):
        next_link = soup.select_one(".button .next a[href]")
    site_name = soup.find("meta", property="og:site_name")
    return MtPage(
        url=_https(url),
        title=_text(soup.h1),
        images=images,
        next_url=_https(urljoin(url, str(next_link["href"]))) if isinstance(next_link, Tag) else None,
        series_title=str(site_name["content"]).strip() if isinstance(site_name, Tag) else "",
    )


def parse_mt_listing(html: str | bytes, url: str) -> MtListing:
    """Read the episodes a Movable Type work's `list.html` links.

    The list runs newest first and carries the product page in place of an
    episode that was taken down; those are dropped and the order reversed.

    Args:
        html: The list page.
        url: The URL it came from, to resolve links against.

    Returns:
        The listing, oldest first, deduplicated, restricted to the work's own pages.
    """
    work = _work_of(url)
    soup = BeautifulSoup(html, "html.parser")
    starts: dict[str, str] = {}
    for item in soup.select("ul.thum li"):
        anchor = item.find("a", href=True)
        if not isinstance(anchor, Tag):
            continue
        href = _https(urljoin(url, str(anchor["href"])))
        match = _MT_PAGE_PATH.match(urlparse(href).path)
        if match is None or match["work"] != work:
            continue
        label = item.find("p")
        starts.setdefault(href, _text(label) if isinstance(label, Tag) else "")
    order = list(reversed(starts))
    return MtListing(starts=tuple(order), labels={start: starts[start] for start in order})


def parse_old_episode(html: str | bytes, url: str) -> OldEpisodePage:
    """Read an older work's `pN.html`.

    Args:
        html: The page.
        url: The URL it came from, to resolve links against.

    Returns:
        What the page says. `frames` is empty when the page holds no comic.
    """
    soup = BeautifulSoup(html, "html.parser")
    frames: dict[str, None] = {}
    for iframe in soup.select(".comic_blocklist iframe[src]"):
        src = _https(urljoin(url, str(iframe["src"])))
        # The ad frames point elsewhere; the strips sit next to the page.
        if urlparse(src).hostname == urlparse(url).hostname:
            frames[src] = None
    title_match = _OLD_SERIES_TITLE.search(_text(soup.title))
    prev_anchor = soup.select_one(".arrow .prev a[href]")
    next_anchor = soup.select_one(".arrow .next a[href]")
    return OldEpisodePage(
        frames=tuple(frames),
        prev_url=_https(urljoin(url, str(prev_anchor["href"]))) if isinstance(prev_anchor, Tag) else None,
        series_title=title_match["title"].strip() if title_match else _text(soup.title).split("|", 1)[0].strip(),
        next_url=_https(urljoin(url, str(next_anchor["href"]))) if isinstance(next_anchor, Tag) else None,
        writer=title_match["author"].strip() if title_match else "",
    )


def parse_old_strip(html: str | bytes, url: str) -> OldStrip:
    """Read one strip frame of an older work.

    Args:
        html: The frame.
        url: The URL it came from, to resolve the image against.

    Returns:
        What the frame says. `images` is empty when it shows none.
    """
    soup = BeautifulSoup(html, "html.parser")
    return OldStrip(
        url=_https(url),
        caption=_text(soup.select_one(".subject h1") or soup.h1),
        images=tuple(_https(urljoin(url, str(img["src"]))) for img in soup.select(".img_block img[src]")),
    )


def parse_old_listing(html: str | bytes, url: str) -> OldListing:
    """Read the updates an older work's index links.

    The index runs newest first; updates that are no longer public link the
    product page instead and are dropped.

    Args:
        html: The index page.
        url: The URL it came from, to resolve links against.

    Returns:
        The listing, oldest first, deduplicated, restricted to the work's own updates.
    """
    work = _work_of(url)
    soup = BeautifulSoup(html, "html.parser")
    updates: dict[int, tuple[str, str]] = {}
    for item in soup.select(".select li"):
        anchor = item.find("a", href=True)
        if not isinstance(anchor, Tag):
            continue
        href = _https(urljoin(url, str(anchor["href"])))
        match = _OLD_EPISODE_PATH.match(urlparse(href).path)
        if match is None or match["work"] != work:
            continue
        paragraphs = [_text(p) for p in item.find_all("p")]
        date = next((text for text in reversed(paragraphs) if re.match(r"^\d{4}/\d{1,2}/\d{1,2}$", text)), "")
        updates.setdefault(int(match["number"]), (href, date))
    order = [updates[number] for number in sorted(updates)]
    return OldListing(urls=tuple(href for href, _ in order), dates={href: date for href, date in order if date})


def _https(url: str) -> str:
    """`url` with its scheme forced to https: the site links itself as http, which no longer answers."""
    return urlunparse(urlparse(url)._replace(scheme="https"))


def _page_number(url: str) -> int | None:
    """The number of a numbered Movable Type page, or None for a named one."""
    match = _MT_PAGE_PATH.match(urlparse(url).path)
    return int(match["page"]) if match and match["page"].isdigit() else None


def _work_of(url: str) -> str:
    """The work directory of any URL under `/<work>/`."""
    return urlparse(url).path.strip("/").split("/", 1)[0]


def _text(tag: Tag | None) -> str:
    return tag.get_text(strip=True) if isinstance(tag, Tag) else ""


class Laza(Extractor):
    """Fetch episodes from まんだらけWEBコミック ラザ.

    An older work's update is read frame by frame; a Movable Type episode is
    walked along its `rel="next"` links until the title changes or the next
    page starts another listed episode. Each work's listing is read once and
    kept on the instance.
    """

    NAME = "laza"
    HOSTS = ("laza.mandarake.co.jp",)
    PUBLISHER = "まんだらけ"
    URL_FORMS = (
        "http://laza.mandarake.co.jp/<work>/manga/<page>.html",
        "http://laza.mandarake.co.jp/<work>/p<n>.html",
        "http://laza.mandarake.co.jp/<work>/",
        "http://laza.mandarake.co.jp/<work>/list.html",
    )

    def __init__(self, session: Client | None = None) -> None:
        """Build an extractor.

        Args:
            session: A session to reuse. A retrying one is made when omitted.
        """
        super().__init__(session)
        # Work base URL -> what its list.html / index links, so each is read once.
        self._mt_listings: dict[str, MtListing] = {}
        self._old_listings: dict[str, OldListing] = {}

    @classmethod
    def suitable(cls, url: str) -> bool:
        """Report whether the extractor takes `url`.

        The site writes its own links as http, so both schemes are taken.

        Args:
            url: The URL to check.

        Returns:
            True for a page, an update or a work's front page or list on the host.
        """
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or parsed.hostname not in cls.HOSTS:
            return False
        return bool(
            _MT_PAGE_PATH.match(parsed.path) or _OLD_EPISODE_PATH.match(parsed.path) or _SERIES_PATH.match(parsed.path)
        )

    @classmethod
    def is_series(cls, url: str) -> bool:
        """Report whether `url` is a work's front page or episode list.

        Args:
            url: The URL to check.

        Returns:
            True for `/<work>/`, `/<work>/index.html` and `/<work>/list.html`.
        """
        return _SERIES_PATH.match(urlparse(url).path) is not None

    def series_urls(self, url: str) -> list[str]:
        """List every episode of a work, oldest first.

        A Movable Type work is listed from its `list.html`; when the work has
        none it is an older work, listed from its index.

        Args:
            url: The work's front page or list.

        Returns:
            The first page of each Movable Type episode, or each `pN.html` update.

        Raises:
            UnsupportedUrlError: The URL is not a work page.
            NotAnEpisodePageError: The work lists no episode.
        """
        if not self.is_series(url):
            msg = f"{url} is not a series page."
            raise UnsupportedUrlError(msg)
        base = self._base(url)
        urls = self._mt_listing(base).starts or self._old_listing(base).urls
        if not urls:
            msg = f"the series at {url} lists no episode."
            raise NotAnEpisodePageError(msg)
        return list(urls)

    def episode(self, url: str) -> Episode:
        """Read one episode and list its pages.

        Args:
            url: Any page of a Movable Type episode (the episode is read from
                its first listed page), or an older work's `pN.html`.

        Returns:
            The episode. The site locks nothing, so `pages` is never empty.

        Raises:
            NotAnEpisodePageError: The page is gone or carries no comic.
        """
        path = urlparse(url).path
        if _MT_PAGE_PATH.match(path):
            return self._mt_episode(url)
        if _OLD_EPISODE_PATH.match(path):
            return self._old_episode(url)
        msg = f"{url} is not an episode page."
        raise NotAnEpisodePageError(msg)

    def _mt_episode(self, url: str) -> Episode:
        """Walk a Movable Type episode from its first page."""
        url = _https(url)
        listing = self._mt_listing(self._base(url))
        start = listing.start_of(url) or url
        pages, stop_url = self._mt_pages(start, listing)
        first = pages[0]
        return self._dated_by_upload(
            Episode(
                url=start,
                series_title=first.series_title or _work_of(url),
                episode_title=first.episode_title or first.title,
                pages=tuple(Page(url=src) for page in pages for src in page.images),
                prev_url=listing.neighbours_of(start)[0],
                next_url=listing.neighbours_of(start)[1] or stop_url,
                metadata={
                    "site": "laza",
                    "work": _work_of(url),
                    "label": listing.labels.get(start, ""),
                    "pages": [{"url": page.url, "title": page.title, "images": list(page.images)} for page in pages],
                },
                # A Movable Type work names nobody on its pages.
                publisher=self.PUBLISHER,
                number=listing.number_of(start),
            )
        )

    def _mt_pages(self, start: str, listing: MtListing) -> tuple[list[MtPage], str | None]:
        """Follow the `rel="next"` links from `start` through the episode.

        The walk stops at a page that starts another listed episode or that
        carries another title (an announcement in the chain, or the next
        episode when the list is stale), and at the end of the chain.

        Returns:
            The pages, and the URL the walk stopped at, if any.
        """
        pages: list[MtPage] = []
        current: str | None = start
        while current:
            seen = {page.url for page in pages}
            if pages and (current in listing.starts or current in seen):
                break
            body = self._fetch(current)
            if body is None:
                if not pages:
                    msg = f"{start} is gone (HTTP 404): not an episode."
                    raise NotAnEpisodePageError(msg)
                current = None
                break
            try:
                page = parse_mt_page(body, current)
            except NotAnEpisodePageError:
                # A page without an image in the chain is not part of the episode.
                if not pages:
                    raise
                break
            if pages and page.episode_title != pages[0].episode_title:
                break
            pages.append(page)
            current = page.next_url
        return pages, current if current and current not in {page.url for page in pages} else None

    def _old_episode(self, url: str) -> Episode:
        """Read an older work's update frame by frame."""
        url = _https(url)
        body = self._fetch(url)
        if body is None:
            msg = f"{url} is gone (HTTP 404): not an episode."
            raise NotAnEpisodePageError(msg)
        page = parse_old_episode(body, url)
        strips = []
        for frame in page.frames:
            frame_body = self._fetch(frame)
            if frame_body is not None:
                strips.append(parse_old_strip(frame_body, frame))
        if not any(strip.images for strip in strips):
            msg = f"no comic on {url}."
            raise NotAnEpisodePageError(msg)
        listing = self._old_listing(self._base(url))
        match = _OLD_EPISODE_PATH.match(urlparse(url).path)
        number = match["number"] if match else ""
        return self._dated_by_upload(
            Episode(
                url=url,
                series_title=page.series_title or _work_of(url),
                episode_title=strips[0].caption or f"p{number}",
                pages=tuple(Page(url=src) for strip in strips for src in strip.images),
                prev_url=listing.neighbours_of(url)[0] or page.prev_url,
                next_url=listing.neighbours_of(url)[1] or page.next_url,
                metadata={
                    "site": "laza",
                    "work": _work_of(url),
                    "update": f"p{number}",
                    "date": listing.dates.get(url, ""),
                    "strips": [
                        {"url": strip.url, "caption": strip.caption, "images": list(strip.images)} for strip in strips
                    ],
                },
                writer=page.writer,
                publisher=self.PUBLISHER,
                number=listing.number_of(url),
            )
        )

    def _mt_listing(self, base: str) -> MtListing:
        """Read a work's `list.html`, once per work. A work without one lists nothing."""
        if base not in self._mt_listings:
            body = self._fetch(urljoin(base, "list.html"))
            self._mt_listings[base] = parse_mt_listing(body, urljoin(base, "list.html")) if body else MtListing()
        return self._mt_listings[base]

    def _old_listing(self, base: str) -> OldListing:
        """Read an older work's index, once per work. An index that is gone lists nothing."""
        if base not in self._old_listings:
            body = self._fetch(base)
            self._old_listings[base] = parse_old_listing(body, base) if body else OldListing()
        return self._old_listings[base]

    def _fetch(self, url: str) -> bytes | None:
        """GET a page over https, streamed so a stalled page still yields what arrived.

        Returns:
            The body, or None when the page is gone (HTTP 404).
        """
        with self._session.stream(
            "GET",
            _https(url),
            headers=self.HEADERS,
            timeout=Timeout(self.TIMEOUT, read=STALL_TIMEOUT),
        ) as res:
            if res.status_code == HTTPStatus.NOT_FOUND:
                return None
            res.raise_for_status()
            return read_body(res)

    @staticmethod
    def _base(url: str) -> str:
        """`https://<host>/<work>/` for any URL under a work."""
        parsed = urlparse(url)
        return f"https://{parsed.netloc}/{_work_of(url)}/"
