"""日刊月チャン (Nikkan Gecchan, Akita Shoten): one page per episode, all of them on one scrolling page.

The site is a plain Rails app, not a viewer platform. A work page,
`/comics/<slug>`, carries every episode of the work as a section holding one
`img.episode-page` whose `data-src` is the page image; an episode URL,
`/comics/<slug>/<n>`, serves the very same page and a little script scrolls
to the `n`-th section (and rewrites the URL as the reader scrolls on). There
is no API, the images are served plain and without a Referer, and nothing is
paywalled -- an episode that is not there answers 404.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from http import HTTPStatus
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from bs4.element import Tag

from getjmanga.errors import NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractor import Episode, Extractor, Page

if TYPE_CHECKING:
    from collections.abc import Iterator

_SERIES_PATH = re.compile(r"^/comics/(?P<slug>[^/]+)/?$")
_EPISODE_PATH = re.compile(r"^/comics/(?P<slug>[^/]+)/(?P<number>\d+)/?$")

#: What follows the episode's own heading in `data-title`: `<heading> | <work> | 日刊月チャン`.
_TITLE_SEPARATOR = "|"


@dataclass(frozen=True)
class Entry:
    """One episode as the work page lists it."""

    #: The `<n>` of `/comics/<slug>/<n>`: the section's position on the page, counted from 1,
    #: which is what the site's script puts in the URL as the reader scrolls.
    number: int
    #: The label over the section: `001`, `プロローグの1`, ...
    label: str
    #: The heading the script puts in the window title while the section is on screen.
    heading: str
    #: The page image, `/comics/<slug>/<n>/image`; empty when the section carries none.
    image: str

    @property
    def title(self) -> str:
        """The label and the heading together, which is how the site names an episode."""
        return f"{self.label} {self.heading}".strip()


def parse_work(html: str | bytes, base_url: str) -> tuple[str, list[Entry]]:
    """Read the work's title and its episode sections off a work or episode page.

    Args:
        html: The page.
        base_url: The page's URL, to resolve the image paths against.

    Returns:
        The work's title and its episodes, in the order the page lists them.
    """
    soup = BeautifulSoup(html, "html.parser")
    heading = soup.select_one("#comicDetail h3")
    series_title = heading.get_text(strip=True) if isinstance(heading, Tag) else ""
    if not series_title:
        series_title = _og_title(soup)
    return series_title, list(_entries(soup, base_url))


def _entries(soup: BeautifulSoup, base_url: str) -> Iterator[Entry]:
    for number, section in enumerate(soup.select(".episodeBox"), start=1):
        image = section.select_one("img.episode-page")
        src = str(image.get("data-src") or image.get("src") or "") if isinstance(image, Tag) else ""
        data_title = str(image.get("data-title") or "") if isinstance(image, Tag) else ""
        label = section.select_one(".episodeTitle")
        yield Entry(
            number=number,
            label=label.get_text(strip=True) if isinstance(label, Tag) else "",
            heading=data_title.split(_TITLE_SEPARATOR, 1)[0].strip(),
            image=urljoin(base_url, src) if src else "",
        )


def _og_title(soup: BeautifulSoup) -> str:
    meta = soup.select_one('meta[property="og:title"]')
    content = str(meta.get("content") or "") if isinstance(meta, Tag) else ""
    return content.rsplit(_TITLE_SEPARATOR, 1)[0].strip()


class Gecchan(Extractor):
    """Fetch episodes from 日刊月チャン."""

    NAME = "gecchan"
    HOSTS = ("nikkangecchan.jp",)
    URL_FORMS = (
        "https://nikkangecchan.jp/comics/<slug>/<n>",
        "https://nikkangecchan.jp/comics/<slug>",
    )

    @classmethod
    def suitable(cls, url: str) -> bool:
        """Report whether `url` is a work or an episode page of the site.

        Args:
            url: The URL to check.

        Returns:
            True for `/comics/<slug>` and `/comics/<slug>/<n>` on a known host.
        """
        if not super().suitable(url):
            return False
        path = urlparse(url).path
        return bool(_EPISODE_PATH.match(path) or _SERIES_PATH.match(path))

    @classmethod
    def is_series(cls, url: str) -> bool:
        """Report whether `url` is a work page, `/comics/<slug>`.

        Args:
            url: The URL to check.

        Returns:
            True for a work page.
        """
        return _SERIES_PATH.match(urlparse(url).path) is not None

    def series_urls(self, url: str) -> list[str]:
        """List every episode of a work, first to latest.

        Args:
            url: A `/comics/<slug>` URL.

        Returns:
            One `/comics/<slug>/<n>` URL per section of the page.

        Raises:
            UnsupportedUrlError: The URL is not a work page.
            NotAnEpisodePageError: The work is not there, or lists no episode.
        """
        match = _SERIES_PATH.match(urlparse(url).path)
        if match is None:
            msg = f"{url} is not a work page."
            raise UnsupportedUrlError(msg)
        _, entries = self._work(url)
        urls = [self._episode_url(url, match["slug"], entry.number) for entry in entries]
        if not urls:
            msg = f"the work at {url} lists no episode."
            raise NotAnEpisodePageError(msg)
        return list(dict.fromkeys(urls))

    def episode(self, url: str) -> Episode:
        """Read one episode: its one page, its titles and the episode after it.

        Args:
            url: A `/comics/<slug>/<n>` URL.

        Returns:
            The episode, with no pages should its section carry no image.

        Raises:
            UnsupportedUrlError: The URL is a work page rather than an episode.
            NotAnEpisodePageError: The episode is not there (404), or the page does not list it.
        """
        match = _EPISODE_PATH.match(urlparse(url).path)
        if match is None:
            msg = f"{url} is not an episode page (a work page is listed with series_urls())."
            raise UnsupportedUrlError(msg)
        slug, number = match["slug"], int(match["number"])

        series_title, entries = self._work(url)
        if not 1 <= number <= len(entries):
            msg = f"{url} lists no episode {number}."
            raise NotAnEpisodePageError(msg)
        entry = entries[number - 1]
        preceding = entries[number - 2] if number > 1 else None
        following = entries[number] if number < len(entries) else None

        return Episode(
            url=url,
            series_title=series_title,
            episode_title=entry.title,
            pages=(Page(url=entry.image),) if entry.image else (),
            prev_url=self._episode_url(url, slug, preceding.number) if preceding is not None else None,
            next_url=self._episode_url(url, slug, following.number) if following is not None else None,
            metadata={
                "slug": slug,
                "number": entry.number,
                "label": entry.label,
                "heading": entry.heading,
                "image": entry.image,
            },
        )

    def _work(self, url: str) -> tuple[str, list[Entry]]:
        """Fetch a work or episode page and read the listing off it."""
        res = self._session.get(url, headers=self.HEADERS, timeout=self.TIMEOUT)
        if res.status_code == HTTPStatus.NOT_FOUND:
            msg = f"{url} is not there (404)."
            raise NotAnEpisodePageError(msg)
        res.raise_for_status()
        series_title, entries = parse_work(res.content, str(res.url or url))
        if not entries and not series_title:
            msg = f"no work on {url}."
            raise NotAnEpisodePageError(msg)
        return series_title, entries

    def _episode_url(self, url: str, slug: str, number: int) -> str:
        return f"{self._origin(url)}/comics/{slug}/{number}"
