"""電脳マヴォ (mavo.takekuma.jp), 竹熊健太郎's free online comic magazine.

A hand-written PHP site: no viewer script, no API, no scrambling, no account,
and every episode is free. It used to answer on plain `http://` and now
redirects that to https, so both schemes are taken.

- A work page, `/title.php?title=<id>`, lists the work's episodes newest
  first in `#title ul.manga`, each an `<a href="viewer.php?id=<id>">` holding
  the update date, the author and the episode's title.
- An episode page, `/viewer.php?id=<id>` (`/pcviewer.php?id=<id>` redirects
  to it), shows every page as a `div.page` inside `#manga`: the first two hold
  the image in `src`, the rest lazy-load it from `data-original`, and each
  carries a `blank.gif` "protector" on top. The heading `#menu` links the
  work page and names the work; `h1` names the episode and links the author.
  Nothing on the page names the next episode, so it comes from the work page,
  read once per work and kept on the instance.
- An id that names no released episode is a 200 page with the failed SQL
  query on it and no `#manga`.
- The images are plain JPEG/PNG files under `/manga/<work>/<episode>/`,
  served without a Referer or a cookie. A "spread" (見開き) viewer mode is
  stored in the PHP session and swaps the page for a JavaScript viewer; a
  fresh session always gets the scroll mode read here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup
from bs4.element import Tag

from getjmanga.errors import NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractor import Episode, Extractor, Page

if TYPE_CHECKING:
    from httpx import Client

# An episode page: `/viewer.php?id=<id>`, or its older `/pcviewer.php` spelling.
_VIEWER_PATH = re.compile(r"^/(?:pc)?viewer\.php$")
# A work page: `/title.php?title=<id>`.
_TITLE_PATH = re.compile(r"^/title\.php$")


@dataclass(frozen=True)
class Viewer:
    """What an episode page says."""

    #: The work's title, from the `#menu` heading.
    series_title: str
    #: The work page the heading links, absolute, or None when it links none.
    title_url: str | None
    #: The episode's title, from `h1` with the author link taken out.
    episode_title: str
    #: The author, from the link in `h1`.
    author: str
    #: The page images in reading order, absolute, deduplicated.
    images: tuple[str, ...]


@dataclass(frozen=True)
class Listing:
    """The episodes a work page lists, oldest first."""

    #: The work's title, from the title image's alt text.
    title: str
    #: Episode URL (`viewer.php?id=<id>`, absolute) -> the title the listing gives it.
    titles: dict[str, str]

    @property
    def urls(self) -> list[str]:
        """The episode URLs, oldest first."""
        return list(self.titles)

    def next_of(self, url: str) -> str | None:
        """The episode listed after `url`, or None."""
        urls = self.urls
        if url not in urls or urls.index(url) + 1 >= len(urls):
            return None
        return urls[urls.index(url) + 1]


def episode_id(url: str) -> str | None:
    """The episode id of a viewer URL, or None when it is not one.

    Args:
        url: The URL to read.

    Returns:
        The `id` query parameter of `/viewer.php` or `/pcviewer.php`.
    """
    parsed = urlparse(url)
    if _VIEWER_PATH.match(parsed.path) is None:
        return None
    ids = parse_qs(parsed.query).get("id", [])
    return ids[0] if ids and ids[0].isdigit() else None


def title_id(url: str) -> str | None:
    """The work id of a work page URL, or None when it is not one.

    Args:
        url: The URL to read.

    Returns:
        The `title` query parameter of `/title.php`.
    """
    parsed = urlparse(url)
    if _TITLE_PATH.match(parsed.path) is None:
        return None
    ids = parse_qs(parsed.query).get("title", [])
    return ids[0] if ids and ids[0].isdigit() else None


def parse_viewer(html: str | bytes, url: str) -> Viewer:
    """Read an episode page.

    Args:
        html: The page.
        url: The URL it came from, to resolve the image paths against.

    Returns:
        What the page says.

    Raises:
        NotAnEpisodePageError: The page has no `#manga` block or no page image.
    """
    soup = BeautifulSoup(html, "html.parser")
    manga = soup.find(id="manga")
    if not isinstance(manga, Tag):
        msg = f"no viewer on {url}."
        raise NotAnEpisodePageError(msg)
    images = _page_images(manga, url)
    if not images:
        msg = f"no pages on {url}."
        raise NotAnEpisodePageError(msg)
    series_title, title_url = _work_link(soup, url)
    episode_title, author = _heading(manga)
    return Viewer(
        series_title=series_title,
        title_url=title_url,
        episode_title=episode_title,
        author=author,
        images=images,
    )


def _page_images(manga: Tag, url: str) -> tuple[str, ...]:
    """The page images of `#manga`, in order, deduplicated."""
    images: dict[str, None] = {}
    for page in manga.find_all("div", class_="page"):
        if not isinstance(page, Tag):
            continue
        for img in page.find_all("img"):
            if not isinstance(img, Tag):
                continue
            src = str(img.get("data-original") or img.get("src") or "").strip()
            # The `protector` on every page is `blank.gif`, as is the
            # placeholder of a lazy image whose real source is `data-original`.
            if not src or src.endswith("blank.gif"):
                continue
            images[urljoin(url, src)] = None
    return tuple(images)


def _work_link(soup: BeautifulSoup, url: str) -> tuple[str, str | None]:
    """The work's title and page, from the link in the `#menu` heading."""
    menu = soup.find(id="menu")
    if isinstance(menu, Tag):
        for anchor in menu.find_all("a", href=True):
            if not isinstance(anchor, Tag):
                continue
            href = urljoin(url, str(anchor["href"]))
            if title_id(href):
                return anchor.get_text(strip=True), href
    return "", None


def _heading(manga: Tag) -> tuple[str, str]:
    """The episode's title and author, from `<h1><title> / <a><author></a></h1>`."""
    heading = manga.find("h1")
    if not isinstance(heading, Tag):
        return "", ""
    author = ""
    link = heading.find("a")
    if isinstance(link, Tag):
        author = link.get_text(strip=True)
        link.decompose()
    return heading.get_text(" ", strip=True).rstrip(" /　").strip(), author


def parse_listing(html: str | bytes, url: str) -> Listing:
    """Read the episodes a work page lists.

    The page lists them newest first, and the "related titles" block below
    the listing links other works, not episodes, so only `viewer.php` links
    in `#title` are taken, reversed into reading order.

    Args:
        html: The work page.
        url: The URL it came from, to resolve the links against.

    Returns:
        The listing, oldest first, deduplicated.
    """
    soup = BeautifulSoup(html, "html.parser")
    title = ""
    logo = soup.find(id="logo")
    if isinstance(logo, Tag):
        img = logo.find("img", alt=True)
        if isinstance(img, Tag):
            title = str(img["alt"]).strip()
    newest_first: dict[str, str] = {}
    block = soup.find(id="title")
    anchors = block.find_all("a", href=True) if isinstance(block, Tag) else []
    for anchor in anchors:
        if not isinstance(anchor, Tag):
            continue
        href = urljoin(url, str(anchor["href"]).strip())
        eid = episode_id(href)
        if eid is None:
            continue
        key = urljoin(url, f"viewer.php?id={eid}")
        if key in newest_first:
            continue
        name = anchor.find(class_="mangatitle")
        newest_first[key] = name.get_text(strip=True) if isinstance(name, Tag) else anchor.get_text(strip=True)
    return Listing(title=title, titles=dict(reversed(list(newest_first.items()))))


class Mavo(Extractor):
    """Fetch episodes from 電脳マヴォ.

    An episode page lists its own pages; the work page it links is read once
    per work and kept on the instance, to name the episode that follows.
    """

    NAME = "mavo"
    HOSTS = ("mavo.takekuma.jp",)
    URL_FORMS = (
        "http://mavo.takekuma.jp/viewer.php?id=<id>",
        "http://mavo.takekuma.jp/title.php?title=<id>",
    )

    def __init__(self, session: Client | None = None) -> None:
        """Build an extractor.

        Args:
            session: A session to reuse. A retrying one is made when omitted.
        """
        super().__init__(session)
        # Work page URL -> what it lists, so a work is read once.
        self._listings: dict[str, Listing] = {}

    @classmethod
    def suitable(cls, url: str) -> bool:
        """Report whether the extractor takes `url`.

        The site used to be served over plain http (and still redirects it),
        so unlike the default this takes http as well as https.

        Args:
            url: The URL to check.

        Returns:
            True for an episode or a work URL on mavo.takekuma.jp.
        """
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or parsed.hostname not in cls.HOSTS:
            return False
        return episode_id(url) is not None or title_id(url) is not None

    @classmethod
    def is_series(cls, url: str) -> bool:
        """Report whether `url` is a work page.

        Args:
            url: The URL to check.

        Returns:
            True for `/title.php?title=<id>`.
        """
        return title_id(url) is not None

    def series_urls(self, url: str) -> list[str]:
        """List every episode a work page links, oldest first.

        Args:
            url: The work page URL.

        Returns:
            The episode URLs.

        Raises:
            UnsupportedUrlError: The URL is not a work page.
            NotAnEpisodePageError: The work page lists no episode.
        """
        if not self.is_series(url):
            msg = f"{url} is not a series page."
            raise UnsupportedUrlError(msg)
        listing = self._listing(url)
        if not listing.titles:
            msg = f"the series at {url} lists no episode."
            raise NotAnEpisodePageError(msg)
        return listing.urls

    def episode(self, url: str) -> Episode:
        """Read one episode and list its pages.

        Args:
            url: The episode URL.

        Returns:
            The episode. Nothing on the site is locked, so `pages` is never empty.

        Raises:
            NotAnEpisodePageError: The URL is not an episode page, or the id names none.
        """
        eid = episode_id(url)
        if eid is None:
            msg = f"{url} is not an episode page."
            raise NotAnEpisodePageError(msg)
        key = urljoin(url, f"viewer.php?id={eid}")
        res = self._get(key)
        viewer = parse_viewer(res.text, str(res.url or key))
        listing = self._listing(viewer.title_url) if viewer.title_url else Listing("", {})
        # The listing is keyed on the URL the work page links, which may be
        # on another scheme than the one asked for.
        listed = next((listed for listed in listing.urls if episode_id(listed) == eid), None)
        next_url = listing.next_of(listed) if listed else None
        return Episode(
            url=key,
            series_title=viewer.series_title or listing.title or eid,
            episode_title=viewer.episode_title or listing.titles.get(listed or "", "") or eid,
            pages=tuple(Page(url=src) for src in viewer.images),
            next_url=urljoin(key, f"viewer.php?id={episode_id(next_url)}") if next_url else None,
            metadata={
                "id": eid,
                "title_url": viewer.title_url,
                "series_title": viewer.series_title,
                "episode_title": viewer.episode_title,
                "author": viewer.author,
                "images": list(viewer.images),
            },
        )

    def _listing(self, url: str) -> Listing:
        """What the work page at `url` lists, read once."""
        tid = title_id(url)
        key = urljoin(url, f"title.php?title={tid}") if tid else url
        if key not in self._listings:
            res = self._get(key)
            self._listings[key] = parse_listing(res.text, str(res.url or key))
        return self._listings[key]
