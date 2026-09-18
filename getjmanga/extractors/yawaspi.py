"""やわらかスピリッツ (小学館): a static site that serves its episodes as plain pages.

A work lives under `/<work>/`: `/<work>/index.html` is its work page, which
lists the episodes still free to read newest first in `section.page__read`
(mixed with shop links for the volumes that hold the expired ones), and
`/<work>/comic/<episode>.html` is one episode, a vertical strip of `<img>`
tags under `div.page__detail__vertical` in reading order with a
`ul.detail__navi` naming the previous and the next episode. A one-shot has no
`comic/` directory at all: its `index.html` (and an `index2.html` for a second
half) *is* the episode. So a `/<work>/` URL is either kind, and `is_series()`
has to fetch it and keep what it read for `episode()` / `series_urls()`.

The images are ordinary JPEGs on `cdn.yawaspi.com`, unscrambled, served
without a Referer or a cookie. An episode whose free run has ended is gone
altogether -- its URL is a 404 and the work page links the volume in the
shop instead -- and the neighbours' navigation skips over it. There is no
account and nothing to sign in to.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from http import HTTPStatus
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from bs4.element import Tag

from .common import Episode, Extractor, NotAnEpisodePageError, Page, UnsupportedUrlError

if TYPE_CHECKING:
    from collections.abc import Iterator

    from requests import Session

# A work page, or a one-shot's episode page: `/<work>/`, `/<work>/index.html`, `/<work>/index2.html`.
_WORK_PATH = re.compile(r"^/(?P<work>[A-Za-z0-9_-]+)/?(?:index\d*\.html)?$")
# An episode of a serial: `/<work>/comic/001_001.html`, `/<work>/comic/sp003_001.html`.
_EPISODE_PATH = re.compile(r"^/(?P<work>[A-Za-z0-9_-]+)/comic/(?P<episode>[A-Za-z0-9_-]+)\.html$")
# Top-level sections of the site that are not works.
_SECTIONS = frozenset(
    {
        "calendar",
        "comicbooks",
        "commons",
        "completion",
        "news",
        "series",
        "shortstory",
        "shu1_result",
        "special",
        "weekly_tryout",
    },
)


@dataclass(frozen=True)
class Document:
    """What one page under `/<work>/` says, whichever of the two kinds it is."""

    #: The URL the page was served at, redirects followed.
    url: str
    #: True for a work page, which only lists episodes; False for an episode page.
    is_work: bool
    #: `.page__header h2`: the work's title, on both kinds of page.
    series_title: str
    #: `.page__header h3`: the episode's title. Empty on a work page, and on a one-shot.
    episode_title: str
    #: `.page__header strong`: the author, as far as the header names one.
    author: str
    #: `.page__header .-date`: the `更新日: yyyy/m/d` line, when the page carries one.
    updated: str
    #: The page images in reading order. Empty on a work page.
    images: tuple[str, ...]
    #: The `a.-next` link of the navigation, when the episode page carries one.
    next_url: str | None
    #: The `a.-prev` link of the navigation, when the episode page carries one.
    prev_url: str | None
    #: The episodes the page lists, oldest first, deduplicated. An episode
    #: page repeats the work's listing under its strip.
    episode_urls: tuple[str, ...]
    #: The page's `<title>`.
    title: str


def parse_document(html: str | bytes, url: str) -> Document:
    """Read a work page or an episode page into a `Document`.

    Args:
        html: The page.
        url: The URL it came from, to resolve relative links against.

    Returns:
        What the page says.

    Raises:
        NotAnEpisodePageError: The page is neither a work page nor an episode page.
    """
    soup = BeautifulSoup(html, "html.parser")
    listing = soup.find("section", class_="page__read")
    detail = soup.find("section", class_="page__detail")
    if not isinstance(listing, Tag) and not isinstance(detail, Tag):
        msg = f"neither a work page nor an episode page at {url}."
        raise NotAnEpisodePageError(msg)

    header = soup.find("div", class_="page__header")
    header = header if isinstance(header, Tag) else soup
    return Document(
        url=url,
        is_work=not isinstance(detail, Tag),
        series_title=_text(header.find("h2")),
        episode_title=_text(header.find("h3")),
        author=_text(header.find("strong")),
        updated=_text(header.find(class_="-date")),
        images=tuple(
            urljoin(url, str(img["src"]))
            for img in (detail.select("div.page__detail__vertical li img[src]") if isinstance(detail, Tag) else ())
            if isinstance(img, Tag)
        ),
        next_url=_link(soup.select_one("ul.detail__navi a.-next[href]"), url),
        prev_url=_link(soup.select_one("ul.detail__navi a.-prev[href]"), url),
        episode_urls=tuple(_episode_links(listing, url)) if isinstance(listing, Tag) else (),
        title=_text(soup.title),
    )


def _text(tag: Tag | None) -> str:
    return tag.get_text(strip=True) if isinstance(tag, Tag) else ""


def _link(anchor: Tag | None, url: str) -> str | None:
    if not isinstance(anchor, Tag):
        return None
    href = str(anchor["href"]).split("#", 1)[0]
    return urljoin(url, href) if href else None


def _episode_links(listing: Tag, url: str) -> Iterator[str]:
    """The episode links of a work page's listing, oldest first, deduplicated.

    The listing runs newest first and links the shop for the volumes holding
    the episodes whose free run has ended; only the site's own episode pages
    are kept.
    """
    seen: set[str] = set()
    host = urlparse(url).hostname
    for anchor in reversed(listing.select("a[href]")):
        absolute = urljoin(url, str(anchor["href"]).split("#", 1)[0])
        parsed = urlparse(absolute)
        if parsed.hostname == host and _EPISODE_PATH.match(parsed.path) and absolute not in seen:
            seen.add(absolute)
            yield absolute


class Yawaspi(Extractor):
    """Fetch episodes from やわらかスピリッツ.

    A work page and a one-shot's episode page share the `/<work>/` URL shape,
    so `is_series()` has to fetch the page; what it read is kept on the
    instance, keyed by URL, and `episode()` / `series_urls()` reuse it, so the
    CLI's `is_series()` + `episode()` sequence costs one request.
    """

    NAME = "yawaspi"
    HOSTS = ("www.yawaspi.com", "yawaspi.com")
    URL_FORMS = (
        "https://yawaspi.com/<work>/comic/<episode>.html",
        "https://yawaspi.com/<work>/",
    )

    def __init__(self, session: Session | None = None) -> None:
        """Build an extractor.

        Args:
            session: A session to reuse. A retrying one is made when omitted.
        """
        super().__init__(session)
        # Normalised page URL -> what the page said, so a page is read once.
        self._documents: dict[str, Document] = {}

    @classmethod
    def suitable(cls, url: str) -> bool:
        """Report whether the extractor takes `url`.

        Args:
            url: The URL to check.

        Returns:
            True for an https work or episode URL on a known host.
        """
        return super().suitable(url) and _kind(url) is not None

    def is_series(self, url: str) -> bool:
        """Report whether `url` is a work page rather than an episode.

        An episode URL under `comic/` is never a series; a `/<work>/` URL is
        fetched, since the URL alone cannot tell a work page from a one-shot,
        and kept for `episode()` or `series_urls()`. Only the path shape is
        checked first, so a forced `--extractor yawaspi` works on an unlisted
        host too.

        Args:
            url: The URL to check.

        Returns:
            True when the page lists episodes.

        Raises:
            NotAnEpisodePageError: The page is neither a work page nor an episode page.
        """
        if _kind(url) != "work":
            return False
        return self._document(url).is_work

    def series_urls(self, url: str) -> list[str]:
        """List every readable episode a work page links to, oldest first.

        Args:
            url: A work page URL.

        Returns:
            One episode URL per linked episode, oldest first.

        Raises:
            UnsupportedUrlError: The URL is not a work page.
            NotAnEpisodePageError: The page links no episode.
        """
        if _kind(url) != "work":
            msg = f"{url} is not a series page."
            raise UnsupportedUrlError(msg)
        document = self._document(url)
        if not document.is_work:
            msg = f"{url} is an episode, not a series page."
            raise UnsupportedUrlError(msg)
        if not document.episode_urls:
            msg = f"the series at {url} lists no readable episode."
            raise NotAnEpisodePageError(msg)
        return list(document.episode_urls)

    def episode(self, url: str) -> Episode:
        """Read one episode and list its pages.

        Args:
            url: The episode URL.

        Returns:
            The episode. A one-shot, whose page has no episode heading, is
            titled after the work. `pages` is empty on a page that carries the
            viewer without images.

        Raises:
            NotAnEpisodePageError: The page is a work page, gone (an expired
                episode is a 404), or neither kind.
        """
        if _kind(url) is None:
            msg = f"{url} is neither a work page nor an episode page."
            raise NotAnEpisodePageError(msg)
        document = self._document(url)
        if document.is_work:
            msg = f"{url} is a work page, not an episode."
            raise NotAnEpisodePageError(msg)
        return Episode(
            url=document.url,
            series_title=document.series_title,
            episode_title=document.episode_title or document.series_title,
            pages=tuple(Page(url=src) for src in document.images),
            next_url=document.next_url,
            metadata={
                "title": document.title,
                "author": document.author,
                "updated": document.updated,
                "prev_url": document.prev_url,
                "images": list(document.images),
            },
        )

    def _document(self, url: str) -> Document:
        """Read a page under `/<work>/`, once per URL."""
        key = _key(url)
        if key not in self._documents:
            res = self._session.get(key, headers=self.HEADERS, timeout=self.TIMEOUT)
            if res.status_code == HTTPStatus.NOT_FOUND:
                msg = f"{url} is gone (HTTP 404): an expired episode, or not a page of the site."
                raise NotAnEpisodePageError(msg)
            res.raise_for_status()
            self._documents[key] = parse_document(res.content, str(res.url or key))
        return self._documents[key]


def _kind(url: str) -> str | None:
    """`"work"` for a `/<work>/` URL, `"episode"` for a `/<work>/comic/...` one, None otherwise."""
    path = urlparse(url).path
    episode = _EPISODE_PATH.match(path)
    if episode is not None:
        return None if episode["work"] in _SECTIONS else "episode"
    work = _WORK_PATH.match(path)
    if work is not None:
        return None if work["work"] in _SECTIONS else "work"
    return None


def _key(url: str) -> str:
    """`url` without query or fragment, a bare `/<work>` or `/<work>/` spelled out as its `index.html`."""
    parsed = urlparse(url)
    path = parsed.path
    if not path.endswith(".html"):
        path = f"{path.rstrip('/')}/index.html"
    return f"{parsed.scheme}://{parsed.netloc}{path}"
