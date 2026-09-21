"""Souffle, Akita Shoten's free web comic site: plain images on a WordPress page."""

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

BASE_URL = "https://souffle.life"

#: WordPress' admin-ajax endpoint, which the Ajax Load More plugin fills a
#: series page's episode list from.
AJAX_URL = f"{BASE_URL}/wp-admin/admin-ajax.php"

#: Episodes asked for per `alm_get_posts` call.
PAGE_SIZE = 100

# More calls than this and the listing is not paging the way it is expected to.
_MAX_PAGES = 50

# An episode is `/<section>/<series slug>/<episode slug>/`, where the section
# is `manga` (Souffle's own works) or `petitprincess` (the magazine's). A
# series is `/<section>/<series slug>/`, or the work's `/author/<series slug>/`
# page, which lists the same episodes. `page` and `feed` are WordPress'
# pagination and RSS, not slugs.
_SLUG = r"(?!(?:page|feed)(?:/|$))[^/]+"
_EPISODE_PATH = re.compile(rf"^/(?P<section>manga|petitprincess)/(?P<series>{_SLUG})/(?P<id>{_SLUG})/?$")
_SERIES_PATH = re.compile(rf"^/(?P<section>manga|petitprincess|author)/(?P<series>{_SLUG})/?$")

# The book name is written `『<title>』<author>`.
_BOOK_TITLE = re.compile(r"『(?P<title>.+)』(?P<author>.*)$")

#: What the announcement under the pages says once an episode's free period is over.
EXPIRED_NOTICE = "公開期限が終了"


class Souffle(Extractor):
    """Fetch episodes from Souffle (souffle.life).

    Every episode is free and needs no account; older episodes of a running
    series expire instead, leaving a page with a single preview image and a
    notice, which comes back as an episode without pages.
    """

    NAME = "souffle"
    HOSTS = ("souffle.life",)
    PUBLISHER = "秋田書店"
    URL_FORMS = (
        "https://souffle.life/manga/<series>/<id>",
        "https://souffle.life/petitprincess/<series>/<id>",
        "https://souffle.life/manga/<series>",
        "https://souffle.life/petitprincess/<series>",
        "https://souffle.life/author/<series>",
    )

    @classmethod
    def suitable(cls, url: str) -> bool:
        """Report whether the extractor takes `url`.

        Args:
            url: The URL to check.

        Returns:
            True for an episode or a series URL on souffle.life.
        """
        if not super().suitable(url):
            return False
        path = urlparse(url).path
        return _EPISODE_PATH.match(path) is not None or _SERIES_PATH.match(path) is not None

    @classmethod
    def is_series(cls, url: str) -> bool:
        """Report whether `url` names a whole series rather than one episode.

        Args:
            url: The URL to check.

        Returns:
            True for `/manga/<series>/`, `/petitprincess/<series>/` or `/author/<series>/`.
        """
        return _SERIES_PATH.match(urlparse(url).path) is not None

    def series_urls(self, url: str) -> list[str]:
        """List every episode of a series, oldest first.

        The series page renders its first few episodes and hands the rest to
        Ajax Load More, which queries WordPress for the posts of the series'
        author. Asking that endpoint directly, in ascending date order, lists
        the whole series in reading order -- expired episodes included, so a
        bulk run reports them as skipped.

        Args:
            url: A series URL.

        Returns:
            One episode URL per listed episode.

        Raises:
            UnsupportedUrlError: The URL is not a series page.
            NotAnEpisodePageError: The series lists no episode.
        """
        parsed = urlparse(url)
        match = _SERIES_PATH.match(parsed.path)
        if match is None:
            msg = f"{url} is not a series page."
            raise UnsupportedUrlError(msg)

        res = self._session.get(url, headers=self.HEADERS, timeout=self.TIMEOUT)
        if res.status_code == HTTPStatus.NOT_FOUND:
            msg = f"no series at {url}."
            raise NotAnEpisodePageError(msg)
        res.raise_for_status()
        soup = BeautifulSoup(res.content, "html.parser")

        listing = soup.find("div", class_="alm-listing")
        author = str(listing.attrs.get("data-author", "")) if isinstance(listing, Tag) else ""
        urls = self._ajax_episode_urls(url, author) if author else []
        if not urls:
            # Without the plugin, the page itself still links the newest episodes.
            urls = list(reversed(_episode_links(soup, url)))
        if not urls:
            msg = f"the series at {url} lists no episode."
            raise NotAnEpisodePageError(msg)
        return urls

    def episode(self, url: str) -> Episode:
        """Read one episode: its titles, its page images and what follows it.

        Args:
            url: The episode URL.

        Returns:
            The episode. `pages` is empty when its free period is over.

        Raises:
            NotAnEpisodePageError: The page is a 404 or carries no pages.
        """
        parsed = urlparse(url)
        match = _EPISODE_PATH.match(parsed.path)
        section = match["section"] if match else ""
        series_slug = match["series"] if match else ""
        episode_id = match["id"] if match else parsed.path.rstrip("/").rsplit("/", 1)[-1]

        res = self._session.get(url, headers=self.HEADERS, timeout=self.TIMEOUT)
        if res.status_code == HTTPStatus.NOT_FOUND:
            msg = f"no episode at {url}."
            raise NotAnEpisodePageError(msg)
        res.raise_for_status()
        soup = BeautifulSoup(res.content, "html.parser")

        container = soup.find("div", class_="sf-content_img")
        if not isinstance(container, Tag):
            msg = f"no pages on {url}; is it a Souffle episode page?"
            raise NotAnEpisodePageError(msg)
        image_urls = [
            urljoin(str(res.url or url), str(img["src"]))
            for img in container.find_all("img")
            if isinstance(img, Tag) and img.get("src")
        ]

        announce = soup.find("div", class_="sf-announce")
        expired = isinstance(announce, Tag) and EXPIRED_NOTICE in announce.get_text()

        book = soup.find("p", class_="sf-content_book_name")
        book_link = book.find("a") if isinstance(book, Tag) else None
        book_name = book_link.get_text(strip=True) if isinstance(book_link, Tag) else ""
        title_match = _BOOK_TITLE.search(book_name)
        series_title = (title_match["title"] if title_match else book_name).strip() or series_slug or episode_id

        heading = soup.find("h1")
        episode_title = (heading.get_text(strip=True) if isinstance(heading, Tag) else "") or episode_id

        date = soup.find("span", class_="sf-date")
        return Episode(
            url=url,
            series_title=series_title,
            episode_title=episode_title,
            pages=() if expired else tuple(Page(url=src) for src in image_urls),
            prev_url=_button_link(soup, "sf-before_btn", url),
            next_url=_button_link(soup, "sf-next_btn", url),
            metadata={
                "section": section,
                "series_slug": series_slug,
                "episode_id": episode_id,
                "book_name": book_name,
                "date": date.get_text(strip=True) if isinstance(date, Tag) else "",
                "expired": expired,
                "prev_url": _button_link(soup, "sf-before_btn", url),
                "images": image_urls,
            },
            writer=title_match["author"].strip() if title_match else "",
            publisher=self.PUBLISHER,
        )

    def _ajax_episode_urls(self, url: str, author: str) -> list[str]:
        """Walk the Ajax Load More listing of an author's posts, oldest first."""
        urls: list[str] = []
        seen: set[str] = set()
        for number in range(_MAX_PAGES):
            res = self._ajax_page(url, author, number)
            body = res.json() if res.is_success else None
            html = body.get("html") if isinstance(body, dict) else None
            if not html:
                break
            fresh = [href for href in _episode_links(BeautifulSoup(html, "html.parser"), url) if href not in seen]
            seen.update(fresh)
            urls += fresh
            meta = body.get("meta") if isinstance(body, dict) else None
            count = int(meta.get("postcount") or 0) if isinstance(meta, dict) else 0
            total = int(meta.get("totalposts") or 0) if isinstance(meta, dict) else 0
            if not fresh or count < PAGE_SIZE or (total and len(urls) >= total):
                break
        return urls

    def _ajax_page(self, url: str, author: str, number: int) -> Response:
        """Ask WordPress for page `number` of the author's posts, as the plugin would."""
        return self._session.get(
            AJAX_URL,
            params={
                "action": "alm_get_posts",
                "repeater": "template_2",
                "author": author,
                "order": "ASC",
                "orderby": "date",
                "posts_per_page": PAGE_SIZE,
                "page": number,
            },
            headers={**self.HEADERS, "Referer": url},
            timeout=self.TIMEOUT,
        )


def _episode_links(soup: BeautifulSoup, url: str) -> list[str]:
    """The episode URLs the `sf-content_book_article` cards link to, in document order, deduplicated."""
    links: list[str] = []
    for article in soup.find_all("article", class_="sf-content_book_article"):
        if not isinstance(article, Tag):
            continue
        for anchor in article.find_all("a", href=True):
            absolute = urljoin(url, str(anchor["href"]).split("?", 1)[0].split("#", 1)[0])
            if _EPISODE_PATH.match(urlparse(absolute).path) and absolute not in links:
                links.append(absolute)
    return links


def _button_link(soup: BeautifulSoup, class_name: str, url: str) -> str | None:
    """The episode a `<span class="sf-next_btn">` / `sf-before_btn` button links to, if any."""
    button = soup.find("span", class_=class_name)
    anchor = button.find("a", href=True) if isinstance(button, Tag) else None
    if not isinstance(anchor, Tag):
        return None
    return urljoin(url, str(anchor["href"]))
