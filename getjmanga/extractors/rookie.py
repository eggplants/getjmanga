"""ジャンプルーキー! (集英社): Hatena's amateur-submission site next to Shonen Jump+.

The site is built by the GigaViewer people and resizes its thumbnails through
`cdn-scissors.gigaviewer.com`, but its reader is an older, server-rendered
one: an episode page at `/series/<series>/<episode>` carries one
`img.js-page-image` per page in reading order inside `.js-viewer-content`,
the page geometry and a `data-page-structure` JSON on the
`section.js-horizontal-viewer` (or `.js-vertical-viewer`) around it, and an
`a.next-episode-button` that names the next episode -- or points back at the
work page on the last one. There is no `script#episode-json`, no `.json`
sibling and no page-list API; the `/api/...` routes are for favourites,
comments and read marks only. The images are plain JPEGs off
`cdn-img.rookie.shonenjump.com`, not scrambled and served without a Referer
or a cookie. Everything is free to read, so nothing is ever locked; a page
without the viewer is not an episode. A work page at `/series/<series>`
lists every episode, oldest first, in `ul.js-episode-list`. Sign-in is a
plain form at `/account/signin`, but gates nothing, so it is not implemented.
"""

from __future__ import annotations

import json
import re
from http import HTTPStatus
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from bs4.element import Tag

from getjmanga.errors import NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractor import Episode, Extractor, Page

if TYPE_CHECKING:
    from collections.abc import Iterator

# Series and episode ids are 11 URL-safe base64 characters (`OmkvmYUVb1c`).
_SERIES_PATH = re.compile(r"^/series/(?P<series>[A-Za-z0-9_-]+)/?$")
_EPISODE_PATH = re.compile(r"^/series/(?P<series>[A-Za-z0-9_-]+)/(?P<episode>[A-Za-z0-9_-]+)/?$")


class Rookie(Extractor):
    """Fetch episodes from ジャンプルーキー!."""

    NAME = "rookie"
    HOSTS = ("rookie.shonenjump.com",)
    URL_FORMS = (
        "https://rookie.shonenjump.com/series/<series>/<episode>",
        "https://rookie.shonenjump.com/series/<series>",
    )

    @classmethod
    def suitable(cls, url: str) -> bool:
        """Report whether `url` is an episode or a work page on the site.

        Args:
            url: The URL to check.

        Returns:
            True for an https `/series/<series>/<episode>` or `/series/<series>` URL on `HOSTS`.
        """
        if not super().suitable(url):
            return False
        path = urlparse(url).path
        return _EPISODE_PATH.match(path) is not None or _SERIES_PATH.match(path) is not None

    @classmethod
    def is_series(cls, url: str) -> bool:
        """Report whether `url` is a work page rather than an episode.

        Args:
            url: The URL to check.

        Returns:
            True for `/series/<series>`.
        """
        return _SERIES_PATH.match(urlparse(url).path) is not None

    def series_urls(self, url: str) -> list[str]:
        """List every episode a work page carries, oldest first.

        Args:
            url: The `/series/<series>` URL.

        Returns:
            One episode URL per listed episode, in the order the page lists them.

        Raises:
            UnsupportedUrlError: The URL is no work page.
            NotAnEpisodePageError: The page is gone or lists no episode.
        """
        if not self.is_series(url):
            msg = f"{url} is not a series page."
            raise UnsupportedUrlError(msg)
        soup = self._page(url)
        listing = soup.find("ul", class_="js-episode-list")
        urls = list(_episode_links(listing if isinstance(listing, Tag) else soup, url))
        if not urls:
            msg = f"the series at {url} lists no episode."
            raise NotAnEpisodePageError(msg)
        return urls

    def episode(self, url: str) -> Episode:
        """Read the titles, the page images and the next episode off an episode page.

        Only the path is checked, so `--extractor rookie` works on an unlisted host too.

        Args:
            url: The `/series/<series>/<episode>` URL.

        Returns:
            The episode. `pages` is empty only if the viewer carries no image.

        Raises:
            UnsupportedUrlError: The URL is no episode URL.
            NotAnEpisodePageError: The page is gone or carries no viewer.
        """
        if _EPISODE_PATH.match(urlparse(url).path) is None:
            msg = f"{url} is not an episode url."
            raise UnsupportedUrlError(msg)
        soup = self._page(url)
        viewer = soup.find("section", class_="js-episode-read-mark-source")
        if not isinstance(viewer, Tag):
            viewer = soup.select_one("[data-page-structure]")
        content = soup.find(class_="js-viewer-content")
        if not isinstance(viewer, Tag) or not isinstance(content, Tag):
            msg = f"no viewer on {url}."
            raise NotAnEpisodePageError(msg)

        series_title, episode_title = _titles(soup)
        width = _int(viewer.get("data-page-width"))
        height = _int(viewer.get("data-page-height"))
        pages = tuple(
            Page(url=urljoin(url, str(img["src"])), width=width, height=height)
            for img in content.select("img.js-page-image[src]")
            if isinstance(img, Tag)
        )

        next_url = None
        button = soup.find("a", class_="next-episode-button")
        if isinstance(button, Tag) and button.has_attr("href"):
            candidate = urljoin(url, str(button["href"]))
            # The last episode's button points back at the work page.
            if _EPISODE_PATH.match(urlparse(candidate).path):
                next_url = candidate

        author = soup.select_one(".page-upper .user-container .user-name")
        published = soup.select_one("#series-history .series-history-date")
        return Episode(
            url=url,
            series_title=series_title,
            episode_title=episode_title,
            pages=pages,
            next_url=next_url,
            metadata={
                "title": soup.title.get_text(strip=True) if isinstance(soup.title, Tag) else "",
                "episode_id": str(viewer.get("data-episode-id") or ""),
                "author": author.get_text(strip=True) if isinstance(author, Tag) else "",
                "published": published.get_text(strip=True) if isinstance(published, Tag) else "",
                "page_structure": _json(viewer.get("data-page-structure")),
                "images": [page.url for page in pages],
            },
        )

    def _page(self, url: str) -> BeautifulSoup:
        """GET a page of the site and parse it; a 404 is no page at all."""
        res = self._session.get(url, headers=self.HEADERS, timeout=self.TIMEOUT)
        if res.status_code == HTTPStatus.NOT_FOUND:
            msg = f"{url} is gone (HTTP 404)."
            raise NotAnEpisodePageError(msg)
        res.raise_for_status()
        return BeautifulSoup(res.content, "html.parser")


def _titles(soup: BeautifulSoup) -> tuple[str, str]:
    """The series and episode titles off a viewer page.

    The back matter's `p.series-title-container` writes the episode as
    `第1話`; the viewer's `h2.series-title-container` as `1話`; the
    `<title>` is `<series> <episode> - ジャンプルーキー!` (a fullwidth `!`) and is the last resort.
    """
    for selector in ("p.series-title-container", "h2.series-title-container", ".series-title-container"):
        container = soup.select_one(selector)
        if not isinstance(container, Tag):
            continue
        series = container.select_one(".series-title")
        number = container.select_one(".episode-number")
        if isinstance(series, Tag) and isinstance(number, Tag):
            return series.get_text(strip=True), number.get_text(strip=True)
    title = soup.title.get_text(strip=True) if isinstance(soup.title, Tag) else ""
    title = re.sub(r"\s*-\s*ジャンプルーキー.?$", "", title)
    series_title, _, episode_title = title.rpartition(" ")
    return (series_title or title, episode_title if series_title else "")


def _episode_links(listing: Tag, url: str) -> Iterator[str]:
    """The episode links of a work page, in page order, deduplicated."""
    seen: set[str] = set()
    for anchor in listing.select("a[href]"):
        absolute = urljoin(url, str(anchor["href"]).split("#", 1)[0].split("?", 1)[0])
        if _EPISODE_PATH.match(urlparse(absolute).path) and absolute not in seen:
            seen.add(absolute)
            yield absolute


def _int(value: object) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return 0


def _json(value: object) -> Any:  # noqa: ANN401 (whatever JSON the attribute holds)
    if value is None:
        return None
    try:
        return json.loads(str(value))
    except ValueError:
        return None
