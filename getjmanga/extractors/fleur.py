"""COMICフルール (KADOKAWA): trial-read episodes laid out as plain images on an a-blog cms page.

The site has no viewer at all. An episode, `/manga/<id>.html`, is a CMS entry
whose pages are ordinary `<img>` elements inside `.manga-content`, served
straight off the site's own `/media/` tree without a Referer or a cookie. The
`src` the page carries is a copy the CMS resized on upload (`mode3_w1200-<file>`
next to the original `<file>`); the original is bigger and always there, so it
is what gets downloaded, with the served copy as a fallback. A work page,
`/lineup_magazine/<id>/`, lists its episodes newest first, and only the ones
still online: older episodes are taken down rather than paywalled, and answer
404, as does the "next episode" link when it skips over them. Nothing needs an
account.
"""

from __future__ import annotations

import re
from http import HTTPStatus
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from bs4.element import Tag
from httpx import HTTPStatusError

from getjmanga.errors import NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractor import Episode, Extractor, Page

if TYPE_CHECKING:
    from httpx import Client
    from PIL import Image

_EPISODE_PATH = re.compile(r"^/manga/(?P<id>[A-Za-z0-9_-]+)\.html$")
_SERIES_PATH = re.compile(r"^/lineup_magazine/(?P<id>[A-Za-z0-9_-]+)/?$")

#: The prefix a-blog cms puts on a resized copy of an uploaded image, `mode3_w1200-<file>`.
_RESIZED = re.compile(r"(?<=/)mode\d+_w\d+-(?=[^/]*$)")

#: `<work>　<episode> | COMICフルール`, which the page title and `og:title` follow.
_SITE_SEPARATOR = " | "
_TITLE_SEPARATOR = "　"


def original_url(url: str) -> str:
    """The full-size image an `<img src>` of the site is a resized copy of.

    Args:
        url: The image URL as the page carries it.

    Returns:
        The URL without the CMS's `mode<n>_w<width>-` prefix, or `url` itself when it has none.
    """
    return _RESIZED.sub("", url, count=1)


class Fleur(Extractor):
    """Fetch episodes from COMICフルール."""

    NAME = "fleur"
    HOSTS = ("comic.mf-fleur.jp",)
    PUBLISHER = "KADOKAWA"
    URL_FORMS = (
        "https://comic.mf-fleur.jp/manga/<id>.html",
        "https://comic.mf-fleur.jp/lineup_magazine/<id>/",
    )

    def __init__(self, session: Client | None = None) -> None:
        """Build an extractor.

        Args:
            session: A session to reuse. A retrying one is made when omitted.
        """
        super().__init__(session)
        #: The credits of each work page read, by URL.
        self._credits: dict[str, str] = {}

    @classmethod
    def suitable(cls, url: str) -> bool:
        """Report whether `url` is an episode or a work page of the site.

        Args:
            url: The URL to check.

        Returns:
            True for `/manga/<id>.html` and `/lineup_magazine/<id>/` on the known host.
        """
        if not super().suitable(url):
            return False
        path = urlparse(url).path
        return bool(_EPISODE_PATH.match(path) or _SERIES_PATH.match(path))

    @classmethod
    def is_series(cls, url: str) -> bool:
        """Report whether `url` is a work page, `/lineup_magazine/<id>/`.

        Args:
            url: The URL to check.

        Returns:
            True for a work page.
        """
        return _SERIES_PATH.match(urlparse(url).path) is not None

    def series_urls(self, url: str) -> list[str]:
        """List the episodes a work page still carries, oldest first.

        The page lists them newest first, with the latest one repeated in a
        banner and the odd link to a news post or a video in between, so the
        episode links are picked out, deduplicated and reversed.

        Args:
            url: A `/lineup_magazine/<id>/` URL.

        Returns:
            One `/manga/<id>.html` URL per listed episode.

        Raises:
            UnsupportedUrlError: The URL is not a work page.
            NotAnEpisodePageError: The work is not there, or lists no episode.
        """
        if _SERIES_PATH.match(urlparse(url).path) is None:
            msg = f"{url} is not a work page."
            raise UnsupportedUrlError(msg)
        soup = self._page(url)
        links: list[str] = []
        for anchor in soup.select("a.cb-story-links__item--link[href]"):
            href = urljoin(url, str(anchor["href"])).split("?", 1)[0].split("#", 1)[0]
            if _EPISODE_PATH.match(urlparse(href).path) and href not in links:
                links.append(href)
        if not links:
            msg = f"the work at {url} lists no episode."
            raise NotAnEpisodePageError(msg)
        links.reverse()
        return links

    def episode(self, url: str) -> Episode:
        """Read one episode: its titles, its page images and the episode after it.

        Args:
            url: A `/manga/<id>.html` URL.

        Returns:
            The episode, with no pages should the entry carry no image.

        Raises:
            UnsupportedUrlError: The URL is a work page rather than an episode.
            NotAnEpisodePageError: The episode is not there (404), or the page holds no manga entry.
        """
        match = _EPISODE_PATH.match(urlparse(url).path)
        if match is None:
            msg = f"{url} is not an episode page (a work page is listed with series_urls())."
            raise UnsupportedUrlError(msg)

        soup = self._page(url)
        content = soup.select_one(".manga-content")
        if not isinstance(content, Tag):
            msg = f"no manga entry on {url}."
            raise NotAnEpisodePageError(msg)

        series_title, episode_title = self._titles(soup)
        served = [
            urljoin(url, src)
            for src in (
                str(img.get("src") or img.get("data-src") or "") for img in content.select(".manga-content__image img")
            )
            if src
        ]
        prev_url = self._pager(soup, url, "_prev")
        next_url = self._pager(soup, url, "_next")
        work = soup.select_one(".manga-header__name a[href]")

        return Episode(
            url=url,
            series_title=series_title,
            episode_title=episode_title,
            pages=tuple(Page(url=original_url(src), extra={"served": src}) for src in served),
            prev_url=prev_url,
            next_url=next_url,
            metadata={
                "id": match["id"],
                "images": served,
                "prev_url": self._pager(soup, url, "_prev"),
            },
            writer=self._writer(urljoin(url, str(work["href"]))) if isinstance(work, Tag) else "",
            publisher=self.PUBLISHER,
        )

    def _writer(self, work_url: str) -> str:
        """The authors the work page links in `.cb-author`, read once per work; an episode names none itself."""
        if work_url not in self._credits:
            soup = self._page(work_url)
            self._credits[work_url] = ", ".join(
                link.get_text(strip=True) for link in soup.select(".cb-author a.cb-author__link")
            )
        return self._credits[work_url]

    def image(self, page: Page, episode: Episode) -> Image.Image:
        """Fetch one page, at full size when the CMS still has the original.

        Args:
            page: The page to fetch.
            episode: The episode the page belongs to.

        Returns:
            The page image.
        """
        headers = {**self.HEADERS, "Referer": episode.url}
        served = str(page.extra.get("served") or "")
        if served and served != page.url:
            try:
                return self._fetch_image(page.url, headers=headers)
            except HTTPStatusError:
                return self._fetch_image(served, headers=headers)
        return self._fetch_image(page.url, headers=headers)

    def _page(self, url: str) -> BeautifulSoup:
        """Fetch a page of the site and parse it."""
        res = self._session.get(url, headers=self.HEADERS, timeout=self.TIMEOUT)
        if res.status_code == HTTPStatus.NOT_FOUND:
            msg = f"{url} is not there (404)."
            raise NotAnEpisodePageError(msg)
        res.raise_for_status()
        return BeautifulSoup(res.content, "html.parser")

    @staticmethod
    def _titles(soup: BeautifulSoup) -> tuple[str, str]:
        """The work's and the episode's title, off the entry header or else the page title."""
        name = soup.select_one(".manga-header__name")
        title = soup.select_one(".manga-header__title")
        series_title = name.get_text(strip=True) if isinstance(name, Tag) else ""
        episode_title = title.get_text(strip=True) if isinstance(title, Tag) else ""
        if series_title and episode_title:
            return series_title, episode_title

        og = soup.select_one('meta[property="og:title"]')
        heading = str(og.get("content") or "") if isinstance(og, Tag) else ""
        if not heading and soup.title:
            heading = soup.title.get_text()
        heading = heading.rsplit(_SITE_SEPARATOR, 1)[0].strip()
        fallback_series, _, fallback_episode = heading.partition(_TITLE_SEPARATOR)
        return series_title or fallback_series.strip(), episode_title or fallback_episode.strip() or heading

    @staticmethod
    def _pager(soup: BeautifulSoup, url: str, direction: str) -> str | None:
        """The `次の話` / `前の話` link of the entry, if there is one."""
        anchor = soup.select_one(f"a.manga-pager__btn.{direction}[href]")
        if not isinstance(anchor, Tag):
            return None
        href = urljoin(url, str(anchor["href"]))
        return href if _EPISODE_PATH.match(urlparse(href).path) else None
