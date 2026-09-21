"""コミックエッセイ劇場, KADOKAWA's free comic essay site: plain images on an a-blog cms page."""

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

BASE_URL = "https://www.comic-essay.com"

#: How many episodes a work page lists at most. Older ones stay readable and
#: are reached through the `前の話へ` link of the oldest listed episode.
LISTING_LIMIT = 100

# A work (series) page is `/episode/<code>/`, where the code is the work's
# a-blog cms category code: a number for the older works, `a<number>` for the
# newer ones. Its `/page/<n>/` and `/limit/<n>/` variants list the same
# episodes and are not taken.
_SERIES_PATH = re.compile(r"^/episode/(?P<id>[a-z0-9_-]+)/?$")

# An episode (reading) page is `/read/<code>/entry-<eid>.html`, or
# `/read/<code>/<eid>.html` for the entries older than a site update.
_EPISODE_PATH = re.compile(r"^/read/(?P<series>[a-z0-9_-]+)/(?P<id>(?:entry-)?\d+)\.html$")


class ComicEssay(Extractor):
    """Fetch episodes from コミックエッセイ劇場 (www.comic-essay.com).

    Every episode is free and needs no account; the pages are plain JPEGs the
    site serves to anyone. A work page lists only the newest `LISTING_LIMIT`
    episodes, so a longer series is completed by walking the previous-episode
    links back from the oldest one listed.
    """

    NAME = "comicessay"
    HOSTS = ("www.comic-essay.com",)
    PUBLISHER = "KADOKAWA"
    URL_FORMS = (
        "https://www.comic-essay.com/read/<series>/entry-<id>.html",
        "https://www.comic-essay.com/read/<series>/<id>.html",
        "https://www.comic-essay.com/episode/<series>/",
    )

    @classmethod
    def suitable(cls, url: str) -> bool:
        """Report whether the extractor takes `url`.

        Args:
            url: The URL to check.

        Returns:
            True for an episode or a work URL on www.comic-essay.com.
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
            True for `/episode/<code>/`, the work page listing the episodes.
        """
        return _SERIES_PATH.match(urlparse(url).path) is not None

    def series_urls(self, url: str) -> list[str]:
        """List every episode of a work, oldest first.

        The work page links its episodes newest first, `LISTING_LIMIT` of them
        at most. When the list is that long, the episodes before the oldest
        listed one are found by following each reading page's `前の話へ` link
        until there is none.

        Args:
            url: A work URL.

        Returns:
            One episode URL per episode, in reading order.

        Raises:
            UnsupportedUrlError: The URL is not a work page.
            NotAnEpisodePageError: The work is a 404 or lists no episode.
        """
        if _SERIES_PATH.match(urlparse(url).path) is None:
            msg = f"{url} is not a work page."
            raise UnsupportedUrlError(msg)

        res = self._fetch_page(url, "work")
        listed = list(reversed(_episode_links(BeautifulSoup(res.content, "html.parser"), str(res.url or url))))
        if not listed:
            msg = f"the work at {url} lists no episode."
            raise NotAnEpisodePageError(msg)

        older: list[str] = []
        if len(listed) >= LISTING_LIMIT:
            seen = set(listed)
            prev_url = self.episode(listed[0]).metadata["prev_url"]
            while prev_url and prev_url not in seen:
                seen.add(prev_url)
                older.append(prev_url)
                prev_url = self.episode(prev_url).metadata["prev_url"]
        return list(reversed(older)) + listed

    def episode(self, url: str) -> Episode:
        """Read one episode: its titles, its page images and what follows it.

        Args:
            url: The episode URL.

        Returns:
            The episode. `pages` is empty when the page shows no comic.

        Raises:
            NotAnEpisodePageError: The page is a 404 or is not a reading page.
        """
        parsed = urlparse(url)
        match = _EPISODE_PATH.match(parsed.path)
        series_id = match["series"] if match else ""
        episode_id = match["id"] if match else parsed.path.rstrip("/").rsplit("/", 1)[-1].removesuffix(".html")

        res = self._fetch_page(url, "episode")
        soup = BeautifulSoup(res.content, "html.parser")

        # The site answers an unknown entry with a 200 "not found" page as often
        # as with a 404, so the reading block itself decides.
        detail = soup.find("div", class_="episode-detail")
        if not isinstance(detail, Tag):
            msg = f"no comic on {url}; is it a コミックエッセイ劇場 reading page?"
            raise NotAnEpisodePageError(msg)

        image_urls = [
            urljoin(str(res.url or url), str(img["src"]))
            for holder in detail.find_all("div", class_="episode-comic__image")
            if isinstance(holder, Tag)
            for img in holder.find_all("img")
            if isinstance(img, Tag) and img.get("src")
        ]

        series_title = _text(detail, "episode-detail__title") or series_id or episode_id
        number = _text(detail, "episode-detail__episode-num")
        heading = _text(detail, "episode-detail__episode-ttl")
        episode_title = "　".join(part for part in (number, heading) if part) or episode_id

        return self._dated_by_upload(
            Episode(
                url=url,
                series_title=series_title,
                episode_title=episode_title,
                pages=tuple(Page(url=src) for src in image_urls),
                prev_url=_pager_link(detail, "_btn-pager-left", url),
                next_url=_pager_link(detail, "_btn-pager-right", url),
                metadata={
                    "series_id": series_id,
                    "episode_id": episode_id,
                    "number": number,
                    "heading": heading,
                    "series_url": f"{BASE_URL}/episode/{series_id}/" if series_id else "",
                    "prev_url": _pager_link(detail, "_btn-pager-left", url),
                    "images": image_urls,
                },
                writer=_credits(soup),
                publisher=self.PUBLISHER,
            )
        )

    def _fetch_page(self, url: str, kind: str) -> Response:
        """GET a site page, turning a 404 into `NotAnEpisodePageError`."""
        res = self._session.get(url, headers=self.HEADERS, timeout=self.TIMEOUT)
        if res.status_code == HTTPStatus.NOT_FOUND:
            msg = f"no {kind} at {url}."
            raise NotAnEpisodePageError(msg)
        res.raise_for_status()
        return res


def _episode_links(soup: BeautifulSoup, url: str) -> list[str]:
    """The episode URLs a work page links to, in document order, deduplicated."""
    links: list[str] = []
    for listing in soup.find_all("ul", class_="episode-list"):
        if not isinstance(listing, Tag):
            continue
        for anchor in listing.find_all("a", href=True):
            absolute = urljoin(url, str(anchor["href"]).split("?", 1)[0].split("#", 1)[0])
            if _EPISODE_PATH.match(urlparse(absolute).path) and absolute not in links:
                links.append(absolute)
    return links


def _credits(soup: BeautifulSoup) -> str:
    """The book's credits below the comic, `役割：名前` each, as `名前 (役割)`."""
    credited = []
    for item in soup.select("div.book-detail-list__author span.book-detail-list__author--item"):
        role, sep, name = item.get_text(strip=True).partition("：")
        credited.append(f"{name.strip()} ({role.strip()})" if sep and name.strip() else role.strip())
    return ", ".join(credit for credit in credited if credit)


def _text(root: Tag, class_name: str) -> str:
    """The stripped text of the first element of `class_name` under `root`, or ""."""
    element = root.find(class_=class_name)
    return element.get_text(" ", strip=True) if isinstance(element, Tag) else ""


def _pager_link(root: Tag, class_name: str, url: str) -> str | None:
    """The episode a `前の話へ` / `次の話へ` button links to, if the page has one."""
    anchor = root.find("a", class_=class_name, href=True)
    if not isinstance(anchor, Tag):
        return None
    return urljoin(url, str(anchor["href"]))
