"""トーチweb (to-ti.in), リイド社's free web comic magazine.

A WordPress site with a viewer of its own (`pc/js/viewer.js`): no API, no
scrambling, no account, and every published episode is free to read.

- A work page, `/product/<slug>`, names the work in `.work_detail h3` and
  lists the episodes it currently publishes in `div.episode ul`, oldest
  first; `.page_pager` above it links the first and the latest. Episodes
  the site has taken down are gone from the list and answer 404.
- An episode page, `/story/<slug>`, holds the viewer in `#viewer`. A comic
  episode (`#viewer.manga`) lays its pages out as `span.manga_page_image`
  elements, in reading order, each naming its image in an `img-url`
  attribute the viewer lazy-loads from. A text episode (`#viewer.text`, a
  news post or an interview) has no pages at all. The footer's `h2` links
  the work page and names the work, with the episode's title in
  `span.name`; the footer's `a.next` links the episode that follows, text
  episodes included.
- The images are plain files under `/wp-content/uploads/img/story/`,
  served without a Referer or a cookie.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from http import HTTPStatus
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup
from bs4.element import Tag

from getjmanga.errors import NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractor import Episode, Extractor, Page

if TYPE_CHECKING:
    from collections.abc import Iterable

    from httpx import Response

# An episode page: `/story/<slug>`. A slug may be percent-encoded Japanese.
_STORY_PATH = re.compile(r"^/story/(?P<slug>[^/]+)/?$")
# A work page: `/product/<slug>`; `/product` alone is the catalogue.
_PRODUCT_PATH = re.compile(r"^/product/(?P<slug>[^/]+)/?$")
# The `<title>` of an episode page: `トーチweb <work> 【<episode>】`.
_PAGE_TITLE = re.compile(r"^\s*トーチweb\s*(?P<series>.*?)\s*【(?P<episode>.*)】\s*$", re.DOTALL)


@dataclass(frozen=True)
class Viewer:
    """What an episode page says."""

    #: `manga` for a comic, `text` for a post without pages.
    kind: str
    #: The work's title, from the footer heading.
    series_title: str
    #: The work page the footer links, absolute, or None when it links none.
    series_url: str | None
    #: The episode's title, from `span.name` in the footer heading.
    episode_title: str
    #: The page images in reading order, absolute, deduplicated.
    images: tuple[str, ...]
    #: The episodes the footer names as previous and next, absolute, or None at either end.
    prev_url: str | None
    next_url: str | None
    #: The author, off the tweet the share button drafts: `『<work>／<author>』`.
    writer: str = ""


@dataclass(frozen=True)
class Work:
    """What a work page says."""

    #: The work's title, from the `h3` heading, its 「」 taken off.
    title: str
    #: Episode URL -> the label the listing gives it, oldest first.
    episodes: dict[str, str]


def story_slug(url: str) -> str | None:
    """The slug of an episode URL, or None when it is not one.

    Args:
        url: The URL to read.

    Returns:
        The `<slug>` of `/story/<slug>`.
    """
    match = _STORY_PATH.match(urlparse(url).path)
    return match["slug"] if match else None


def product_slug(url: str) -> str | None:
    """The slug of a work page URL, or None when it is not one.

    Args:
        url: The URL to read.

    Returns:
        The `<slug>` of `/product/<slug>`.
    """
    match = _PRODUCT_PATH.match(urlparse(url).path)
    return match["slug"] if match else None


def parse_viewer(html: str | bytes, url: str) -> Viewer:
    """Read an episode page.

    Args:
        html: The page.
        url: The URL it came from, to resolve the links against.

    Returns:
        What the page says.

    Raises:
        NotAnEpisodePageError: The page has no `#viewer` on it.
    """
    soup = BeautifulSoup(html, "html.parser")
    viewer = soup.find(id="viewer")
    if not isinstance(viewer, Tag):
        msg = f"no viewer on {url}."
        raise NotAnEpisodePageError(msg)
    classes = viewer.get("class") or []
    kind = "text" if "text" in classes else "manga"

    images: dict[str, None] = {}
    for span in viewer.select("span.manga_page_image[img-url]"):
        src = str(span["img-url"]).strip()
        if src:
            images[urljoin(url, src)] = None

    series_title, series_url, episode_title = _footer_heading(viewer, url)
    if not series_title or not episode_title:
        title = soup.find("title")
        match = _PAGE_TITLE.match(title.get_text()) if isinstance(title, Tag) else None
        if match is not None:
            series_title = series_title or match["series"].strip()
            episode_title = episode_title or match["episode"].strip()

    footer = viewer.find("footer")
    return Viewer(
        kind=kind,
        series_title=series_title,
        series_url=series_url,
        episode_title=episode_title,
        images=tuple(images),
        prev_url=_footer_link(footer, "prev", url),
        next_url=_footer_link(footer, "next", url),
        writer=_shared_author(soup),
    )


#: The tweet the share button drafts: `『<work>／<author>』<episode>`.
_SHARE_TEXT = re.compile(r"『[^』／]+／(?P<author>[^』]+)』")


def _shared_author(soup: BeautifulSoup) -> str:
    """The author named in the share button's tweet, the only place a story page names one."""
    for anchor in soup.select('a[href*="twitter.com/share"]'):
        text = parse_qs(urlparse(str(anchor["href"])).query).get("text", [""])[0]
        match = _SHARE_TEXT.search(text)
        if match:
            return match["author"].strip()
    return ""


def _footer_link(footer: Tag | None, direction: str, url: str) -> str | None:
    """The viewer footer's `a.prev` / `a.next` episode link, or None when it is absent or empty."""
    anchor = footer.select_one(f"a.{direction}[href]") if isinstance(footer, Tag) else None
    if isinstance(anchor, Tag) and str(anchor["href"]).strip():
        return urljoin(url, str(anchor["href"]).strip())
    return None


def _footer_heading(viewer: Tag, url: str) -> tuple[str, str | None, str]:
    """The work's title and page, and the episode's title, from `<footer><h2><a>`."""
    footer = viewer.find("footer")
    heading = footer.find("h2") if isinstance(footer, Tag) else None
    if not isinstance(heading, Tag):
        return "", None, ""
    anchor = heading.find("a", href=True)
    series_url = None
    if isinstance(anchor, Tag):
        href = urljoin(url, str(anchor["href"]).strip())
        if product_slug(href):
            series_url = href
    episode_title = ""
    name = heading.find(class_="name")
    if isinstance(name, Tag):
        episode_title = name.get_text(" ", strip=True)
        name.decompose()
    return heading.get_text(" ", strip=True), series_url, episode_title


def parse_work(html: str | bytes, url: str) -> Work:
    """Read the episodes a work page lists.

    `div.episode ul` runs oldest first and holds every episode the site
    still publishes; `.page_pager` links the first and the latest, which
    stands in when the list is missing. Other `/story/` links on the page
    (the cover, the news) are not taken.

    Args:
        html: The work page.
        url: The URL it came from, to resolve the links against.

    Returns:
        The work's title and its listed episodes, oldest first, deduplicated.

    Raises:
        NotAnEpisodePageError: The page is not a work page.
    """
    soup = BeautifulSoup(html, "html.parser")
    detail = soup.select_one("div.work_detail")
    if not isinstance(detail, Tag):
        msg = f"no work page at {url}."
        raise NotAnEpisodePageError(msg)
    heading = detail.find("h3")
    title = heading.get_text(" ", strip=True).strip("「」").strip() if isinstance(heading, Tag) else ""

    episodes: dict[str, str] = {}
    listing = detail.select_one("div.episode")
    if isinstance(listing, Tag):
        _collect(episodes, listing.select("li a[href]"), url)
    if not episodes:
        pager = detail.select_one("div.page_pager")
        if isinstance(pager, Tag):
            _collect(episodes, pager.select("a[href]"), url)
    return Work(title=title, episodes=episodes)


def _collect(episodes: dict[str, str], anchors: Iterable[Tag], url: str) -> None:
    """Add the `/story/` links among `anchors` to `episodes`, labelled by their `<span>`."""
    for anchor in anchors:
        href = urljoin(url, str(anchor["href"]).strip())
        if story_slug(href) is None or href in episodes:
            continue
        label = anchor.find("span")
        episodes[href] = (label if isinstance(label, Tag) else anchor).get_text(" ", strip=True)


class Torch(Extractor):
    """Fetch episodes from トーチweb.

    An episode page lists its own pages and names the next episode, so a
    work page is only read to list a series.
    """

    NAME = "torch"
    HOSTS = ("to-ti.in",)
    PUBLISHER = "リイド社"
    URL_FORMS = (
        "https://to-ti.in/story/<slug>",
        "https://to-ti.in/product/<slug>",
    )

    @classmethod
    def suitable(cls, url: str) -> bool:
        """Report whether the extractor takes `url`.

        Args:
            url: The URL to check.

        Returns:
            True for an episode or a work URL on to-ti.in.
        """
        if not super().suitable(url):
            return False
        return story_slug(url) is not None or product_slug(url) is not None

    @classmethod
    def is_series(cls, url: str) -> bool:
        """Report whether `url` is a work page.

        Args:
            url: The URL to check.

        Returns:
            True for `/product/<slug>`.
        """
        return product_slug(url) is not None

    def series_urls(self, url: str) -> list[str]:
        """List every episode a work page publishes, oldest first.

        Args:
            url: The work page URL.

        Returns:
            The episode URLs.

        Raises:
            UnsupportedUrlError: The URL is not a work page.
            NotAnEpisodePageError: There is no such work, or it lists no episode.
        """
        if not self.is_series(url):
            msg = f"{url} is not a series page."
            raise UnsupportedUrlError(msg)
        work = parse_work(self._fetch_page(url).text, url)
        if not work.episodes:
            msg = f"the series at {url} lists no episode."
            raise NotAnEpisodePageError(msg)
        return list(work.episodes)

    def episode(self, url: str) -> Episode:
        """Read one episode and list its pages.

        Args:
            url: The episode URL.

        Returns:
            The episode. `pages` is empty for a text post, which has none.

        Raises:
            NotAnEpisodePageError: The URL is not an episode page, or the site
                no longer serves it (404).
        """
        if story_slug(url) is None:
            msg = f"{url} is not an episode page."
            raise NotAnEpisodePageError(msg)
        res = self._fetch_page(url)
        viewer = parse_viewer(res.text, str(res.url or url))
        return self._dated_by_upload(
            Episode(
                url=url,
                series_title=viewer.series_title,
                episode_title=viewer.episode_title,
                pages=tuple(Page(url=src) for src in viewer.images),
                prev_url=viewer.prev_url,
                next_url=viewer.next_url,
                metadata={
                    "slug": story_slug(url),
                    "kind": viewer.kind,
                    "series_title": viewer.series_title,
                    "series_url": viewer.series_url,
                    "episode_title": viewer.episode_title,
                    "images": list(viewer.images),
                    "next_url": viewer.next_url,
                },
                writer=viewer.writer,
                publisher=self.PUBLISHER,
                number=self._listed_number(viewer.series_url, url) if viewer.series_url else None,
            )
        )

    def _fetch_page(self, url: str) -> Response:
        """GET a page of the site, turning its 404 into `NotAnEpisodePageError`."""
        res = self._session.get(url, headers=self.HEADERS, timeout=self.TIMEOUT)
        if res.status_code == HTTPStatus.NOT_FOUND:
            msg = f"{url} is gone (HTTP 404): the site no longer serves it."
            raise NotAnEpisodePageError(msg)
        res.raise_for_status()
        return res
