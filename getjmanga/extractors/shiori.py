"""栞 (しおり), 大洋図書's free web comic site: plain images on a WordPress page.

A work page at `https://shiori-on.com/product/<slug>` lists the episodes that
are open to read, oldest first. An episode page at
`https://shiori-on.com/story/<slug>_<n>` is a Swiper carousel, one
`div.swiper-slide` per page holding a plain `<img>` -- no scrambling, no
account, no `Referer` check on the images. The page's `次の話へ` link names
the episode after it, skipping the ones that have expired.

Nothing on the site is paid or login-only. An episode whose free period has
ended is simply removed: its URL answers 404, and the work page notes which
episodes went where.
"""

from __future__ import annotations

import re
from http import HTTPStatus
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from bs4.element import Tag

from getjmanga.errors import NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractor import Episode, Extractor, Page

if TYPE_CHECKING:
    from httpx import Response

BASE_URL = "https://shiori-on.com"

# An episode page: `/story/<slug>_<n>`. The numbering is the work's own
# (`runrun_1`, `yominoie_021`), so anything after the underscore is taken.
_EPISODE_PATH = re.compile(r"^/story/(?P<id>[\w-]+)/?$")
# A work page: `/product/<slug>`. `/product/` alone is the catalogue.
_SERIES_PATH = re.compile(r"^/product/(?P<slug>[\w-]+)/?$")

# `span.current` in the viewer's footer, and `og:title`: `【<episode>】<series>`.
_TITLE = re.compile(r"^【(?P<episode>.+?)】(?P<series>.*)$", re.DOTALL)


class Shiori(Extractor):
    """Fetch episodes from 栞 (shiori-on.com).

    Every listed episode is free and needs no account; the pages are plain
    image files under the site's WordPress uploads.
    """

    NAME = "shiori"
    HOSTS = ("shiori-on.com",)
    URL_FORMS = (
        "https://shiori-on.com/story/<slug>_<n>",
        "https://shiori-on.com/product/<slug>",
    )

    @classmethod
    def suitable(cls, url: str) -> bool:
        """Report whether the extractor takes `url`.

        Args:
            url: The URL to check.

        Returns:
            True for an episode (`/story/<id>`) or a work (`/product/<slug>`) URL on shiori-on.com.
        """
        if not super().suitable(url):
            return False
        path = urlparse(url).path
        return _EPISODE_PATH.match(path) is not None or _SERIES_PATH.match(path) is not None

    @classmethod
    def is_series(cls, url: str) -> bool:
        """Report whether `url` names a whole work rather than one episode.

        Args:
            url: The URL to check.

        Returns:
            True for `/product/<slug>`, the work page listing the episodes.
        """
        return _SERIES_PATH.match(urlparse(url).path) is not None

    def series_urls(self, url: str) -> list[str]:
        """List the readable episodes of a work, oldest first.

        Args:
            url: A work URL.

        Returns:
            One episode URL per listed episode, in the order the work page lists them.

        Raises:
            UnsupportedUrlError: The URL is not a work page.
            NotAnEpisodePageError: The work is a 404 or lists no episode.
        """
        if _SERIES_PATH.match(urlparse(url).path) is None:
            msg = f"{url} is not a work page."
            raise UnsupportedUrlError(msg)

        res = self._fetch_page(url, "work")
        urls = _episode_links(BeautifulSoup(res.content, "html.parser"), str(res.url or url))
        if not urls:
            msg = f"the work at {url} lists no episode."
            raise NotAnEpisodePageError(msg)
        return urls

    def episode(self, url: str) -> Episode:
        """Read one episode: its titles, its page images and what follows it.

        Args:
            url: The episode URL.

        Returns:
            The episode. `pages` is empty when the viewer holds no image.

        Raises:
            NotAnEpisodePageError: The page is a 404 (an expired or unknown
                episode) or carries no viewer.
        """
        parsed = urlparse(url)
        match = _EPISODE_PATH.match(parsed.path)
        episode_id = match["id"] if match else parsed.path.rstrip("/").rsplit("/", 1)[-1]

        res = self._fetch_page(url, "episode")
        page_url = str(res.url or url)
        soup = BeautifulSoup(res.content, "html.parser")

        viewer = soup.find(id="storySlide")
        if not isinstance(viewer, Tag):
            msg = f"no viewer on {url}; is it a 栞 episode page?"
            raise NotAnEpisodePageError(msg)

        image_urls = [
            urljoin(page_url, str(img["src"]))
            for slide in viewer.find_all("div", class_="swiper-slide")
            if isinstance(slide, Tag) and "last" not in (slide.get("class") or [])
            for img in slide.find_all("img")
            if isinstance(img, Tag) and img.get("src")
        ]

        series_title, episode_title = _titles(soup)
        series_url = _series_link(soup, page_url)
        return Episode(
            url=url,
            series_title=series_title or episode_id.rsplit("_", 1)[0],
            episode_title=episode_title or episode_id,
            pages=tuple(Page(url=src) for src in image_urls),
            next_url=_nav_link(soup, "次の話", page_url),
            metadata={
                "episode_id": episode_id,
                "series_url": series_url,
                "prev_url": _nav_link(soup, "前の話", page_url),
                "images": image_urls,
            },
        )

    def _fetch_page(self, url: str, kind: str) -> Response:
        """GET a site page, turning a 404 into `NotAnEpisodePageError`."""
        res = self._session.get(url, headers=self.HEADERS, timeout=self.TIMEOUT)
        if res.status_code == HTTPStatus.NOT_FOUND:
            msg = f"no {kind} at {url} (HTTP 404): unknown, or its free period has ended."
            raise NotAnEpisodePageError(msg)
        res.raise_for_status()
        return res


def _episode_links(soup: BeautifulSoup, url: str) -> list[str]:
    """The episode URLs a work page lists, in document order, deduplicated."""
    listing = soup.find("section", class_="product-story")
    scope = listing if isinstance(listing, Tag) else soup
    links: list[str] = []
    for anchor in scope.find_all("a", href=True):
        absolute = urljoin(url, str(anchor["href"]).split("?", 1)[0].split("#", 1)[0])
        if _EPISODE_PATH.match(urlparse(absolute).path) and absolute not in links:
            links.append(absolute)
    return links


def _titles(soup: BeautifulSoup) -> tuple[str, str]:
    """The `(series, episode)` titles of an episode page, `""` for whatever it does not say."""
    current = soup.find("span", class_="current")
    candidates = [current.get_text(strip=True) if isinstance(current, Tag) else ""]
    og_title = soup.find("meta", property="og:title")
    if isinstance(og_title, Tag):
        candidates.append(str(og_title.get("content") or "").strip())
    for text in candidates:
        match = _TITLE.match(text)
        if match:
            return match["series"].strip(), match["episode"].strip()
    return "", ""


def _nav_link(soup: BeautifulSoup, label: str, url: str) -> str | None:
    """The episode an anchor labelled `label` (`次の話`, `前の話`) links to, if there is one."""
    for anchor in soup.find_all("a", href=True):
        if label in anchor.get_text():
            href = urljoin(url, str(anchor["href"]))
            if _EPISODE_PATH.match(urlparse(href).path):
                return href
    return None


def _series_link(soup: BeautifulSoup, url: str) -> str:
    """The work page the episode page links back to, or `""`."""
    for anchor in soup.find_all("a", href=True):
        href = urljoin(url, str(anchor["href"]))
        if _SERIES_PATH.match(urlparse(href).path):
            return href
    return ""
