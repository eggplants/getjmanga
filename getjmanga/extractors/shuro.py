"""SHURO (マガジンハウス), a WordPress site whose episodes are plain pages.

An episode page `/episode/<id>/` carries the whole reader in its HTML: one
`div.slide > img` per page, top to bottom, inside `div.viewer-vertical`. The
images are ordinary uploads on `img.shuro.world`: no scrambling, no Referer
or cookie check. A work page `/manga/<slug>/` lists the episodes currently
online, newest first; the same list is embedded in every episode page as a
`const mangaData = [...]` script, which is where the next episode comes from.
Everything is free and there is no account, so an episode that is no longer
online is simply gone (HTTP 404) rather than locked.
"""

from __future__ import annotations

import json
import re
import unicodedata
from http import HTTPStatus
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from bs4.element import Tag

from getjmanga.errors import NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractor import Episode, Extractor, Page

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

_EPISODE_PATH = re.compile(r"^/episode/(?P<id>\d+)/?$")
_SERIES_PATH = re.compile(r"^/manga/(?P<slug>[^/]+)/?$")

# The script an episode page embeds the works it belongs to in, `episodes` and all.
_MANGA_DATA = re.compile(r"\bmangaData\s*=\s*")

# The first run of digits in an episode label, once its full-width digits are folded to ASCII.
_NUMBER = re.compile(r"\d+")


def parse_manga_data(html: str) -> list[dict[str, Any]]:
    """Read the `const mangaData = [...]` script off an episode page.

    Args:
        html: The page.

    Returns:
        The works the page belongs to, each with its `episodes`; empty when
        the page has no such script.
    """
    match = _MANGA_DATA.search(html)
    if match is None:
        return []
    try:
        data, _ = json.JSONDecoder().raw_decode(html, match.end())
    except ValueError:
        return []
    return [work for work in data if isinstance(work, dict)] if isinstance(data, list) else []


def episode_number(label: str) -> int | None:
    """The episode number an episode label carries, if any.

    Args:
        label: An episode label as the site shows it: `第15話`, `#23`,
            `session0`, `後編`, `試し読み`, ... with full-width digits in
            some of them (the site mixes them freely, even inside one label).

    Returns:
        The first number in it, full-width digits folded, or None without one.
    """
    match = _NUMBER.search(unicodedata.normalize("NFKC", label))
    return int(match.group()) if match is not None else None


def reading_order(entries: Sequence[tuple[str, str]]) -> list[str]:
    """Put a work's episode listing into reading order.

    The site lists a work's episodes newest first by publication date, so
    reversing the list is right until several episodes share a date: those
    come out in the order they were entered, so reversing them puts them
    backwards. When every label carries a distinct number (`第1話`, `#23`,
    `session0`), that number settles the order instead.

    Args:
        entries: `(url, label)` pairs, in the order the site lists them.

    Returns:
        The URLs, oldest first, deduplicated.
    """
    urls: list[str] = []
    labels: dict[str, str] = {}
    for url, label in entries:
        if url not in labels:
            urls.append(url)
            labels[url] = label
    numbers = {url: episode_number(labels[url]) for url in urls}
    if urls and None not in numbers.values() and len(set(numbers.values())) == len(urls):
        return sorted(urls, key=lambda url: numbers[url] or 0)
    return urls[::-1]


def _text(tag: Tag | None) -> str:
    return tag.get_text(" ", strip=True) if isinstance(tag, Tag) else ""


class Shuro(Extractor):
    """Fetch episodes from SHURO (shuro.world)."""

    NAME = "shuro"
    HOSTS = ("shuro.world",)
    URL_FORMS = (
        "https://shuro.world/episode/<id>/",
        "https://shuro.world/manga/<slug>/",
    )

    @classmethod
    def suitable(cls, url: str) -> bool:
        """Report whether the extractor takes `url`.

        Args:
            url: The URL to check.

        Returns:
            True for an https episode or work URL on shuro.world.
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
            True for `/manga/<slug>/`, the work page listing the episodes.
        """
        return _SERIES_PATH.match(urlparse(url).path) is not None

    def series_urls(self, url: str) -> list[str]:
        """List the episodes a work page has online, oldest first.

        Only the episodes currently online are listed: on a running series
        that is typically the first one and the newest few.

        Args:
            url: A work page URL.

        Returns:
            One episode URL per listed episode, in reading order.

        Raises:
            UnsupportedUrlError: The URL is not a work page.
            NotAnEpisodePageError: The page lists no episode, or is gone.
        """
        if _SERIES_PATH.match(urlparse(url).path) is None:
            msg = f"{url} is not a work page."
            raise UnsupportedUrlError(msg)
        soup = BeautifulSoup(self._page(url), "html.parser")
        listing = [
            (self._key(urljoin(url, str(anchor["href"]))), _text(anchor.find("b")) or _text(anchor))
            for anchor in soup.select("a.line-box[href]")
            if isinstance(anchor, Tag)
        ]
        urls = reading_order([(link, label) for link, label in listing if _EPISODE_PATH.match(urlparse(link).path)])
        if not urls:
            msg = f"the work at {url} lists no episode."
            raise NotAnEpisodePageError(msg)
        return urls

    def episode(self, url: str) -> Episode:
        """Read one episode and list its pages.

        Args:
            url: The episode URL.

        Returns:
            The episode. `pages` is empty when the reader stands on the page
            with nothing in it.

        Raises:
            NotAnEpisodePageError: The page carries no reader, or is gone.
        """
        html = self._page(url)
        soup = BeautifulSoup(html, "html.parser")
        viewer = soup.find("div", class_="viewer-vertical")
        if not isinstance(viewer, Tag):
            msg = f"no viewer on {url}."
            raise NotAnEpisodePageError(msg)

        headings = [_text(element) for element in soup.select("a.viewer-top .marquee-element")]
        series_title = headings[0] if headings else ""
        episode_title = headings[1] if len(headings) > 1 else ""
        work_link = soup.select_one("a.viewer-top[href]")
        work_url = urljoin(url, str(work_link["href"])) if isinstance(work_link, Tag) else None

        works = parse_manga_data(html)
        entry, next_url = self._locate(url, works)
        if not episode_title and entry is not None:
            parts = (str(entry.get("title", "")), str(entry.get("titleSub", "")))
            episode_title = " ".join(part for part in parts if part)
        if not series_title and works:
            series_title = str(works[0].get("title", ""))

        slides = [slide for slide in viewer.find_all("div", class_="slide") if isinstance(slide, Tag)]
        pages = tuple(page for page in (self._page_of(slide, url) for slide in slides) if page is not None)
        return Episode(
            url=url,
            series_title=series_title,
            episode_title=episode_title,
            pages=pages,
            next_url=next_url,
            metadata={
                "title": _text(soup.title),
                "work_url": work_url,
                "episode": entry,
                "works": [{key: value for key, value in work.items() if key != "episodes"} for work in works],
                "images": [page.url for page in pages],
            },
        )

    def _page(self, url: str) -> str:
        """GET a page of the site, or say it is gone."""
        res = self._session.get(url, headers=self.HEADERS, timeout=self.TIMEOUT)
        if res.status_code == HTTPStatus.NOT_FOUND:
            msg = f"{url} is gone (HTTP 404): not an episode nor a work page."
            raise NotAnEpisodePageError(msg)
        res.raise_for_status()
        return res.text

    @staticmethod
    def _locate(url: str, works: Iterable[dict[str, Any]]) -> tuple[dict[str, Any] | None, str | None]:
        """Find `url` in the works' episode listings: its entry and what follows it."""
        wanted = Shuro._key(url)
        for work in works:
            episodes = [entry for entry in work.get("episodes") or [] if isinstance(entry, dict)]
            by_url = {Shuro._key(str(entry.get("permalink", ""))): entry for entry in episodes}
            ordered = reading_order([(key, str(entry.get("title", ""))) for key, entry in by_url.items()])
            if wanted in by_url:
                position = ordered.index(wanted)
                following = ordered[position + 1 :]
                return by_url[wanted], str(by_url[following[0]].get("permalink")) if following else None
        return None, None

    @staticmethod
    def _page_of(slide: Tag, url: str) -> Page | None:
        """The page a `div.slide` holds, or None for a slide without an image."""
        img = slide.find("img", src=True)
        if not isinstance(img, Tag):
            return None
        return Page(url=urljoin(url, str(img["src"])), width=_int(img.get("width")), height=_int(img.get("height")))

    @staticmethod
    def _key(url: str) -> str:
        """`url` without query, fragment or a missing trailing slash, as WordPress serves it."""
        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}/"


def _int(value: object) -> int:
    try:
        return int(str(value))
    except ValueError:
        return 0
