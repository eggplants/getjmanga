"""COMICポルタ (イースト・プレス): a WordPress catalogue in front of static SpeedBinb exports.

Every free episode is a directory `/p_data/<slug>/` holding Voyager's
SpeedBinb reader in its "PtBinb" form: the page HTML lists one
`<div data-ptimg="data/NNNN.ptimg.json">` per page, and each of those JSON
files names a scrambled JPEG next to it plus the list of rectangles to copy
out of it to rebuild the page -- the static form `viewers/speedbinb.py`
reads. No API, no cookie, no Referer check.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from http import HTTPStatus
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from bs4.element import Tag
from httpx import HTTPError

from getjmanga.errors import NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractor import Episode, Extractor, Page, neighbours
from getjmanga.viewers import speedbinb
from getjmanga.viewers.speedbinb import split_title

if TYPE_CHECKING:
    from httpx import Client
    from PIL import Image

# An episode: the directory of one SpeedBinb export.
_EPISODE_PATH = re.compile(r"^/p_data/(?P<slug>[^/]+)/?$")

# A work page, listing the episodes oldest first.
_SERIES_PATH = re.compile(r"^/series/(?P<id>\d+)/?$")

# The element the reader hangs its page list on.
_CONTAINER_CLASS = "ptbinb-container"

# The link back to the work page that the last-page frame of every episode carries.
_DETAIL_LINK_TEXT = "作品詳細"


@dataclass(frozen=True)
class Listing:
    """What a work page says: its title and the episodes it links, oldest first."""

    title: str
    urls: tuple[str, ...]
    #: `p.authors`, as the page writes it.
    writer: str = ""


class Porta(Extractor):
    """Fetch episodes from COMICポルタ.

    Each `Page.url` is the page's `ptimg.json`; `image()` reads it, fetches the
    resource it names and rebuilds the page, so listing an episode costs one
    request however long it is.
    """

    NAME = "porta"
    HOSTS = ("comic-porta.com",)
    PUBLISHER = "イースト・プレス"
    URL_FORMS = (
        "https://comic-porta.com/p_data/<slug>",
        "https://comic-porta.com/series/<id>",
    )

    def __init__(self, session: Client | None = None) -> None:
        """Build an extractor.

        Args:
            session: A session to reuse. A retrying one is made when omitted.
        """
        super().__init__(session)
        # Work page URL -> what it lists, so a `-b` walk reads each work page once.
        self._listings: dict[str, Listing] = {}

    @classmethod
    def suitable(cls, url: str) -> bool:
        """Report whether the extractor takes `url`.

        Args:
            url: The URL to check.

        Returns:
            True for an https episode or work page on a known host.
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
            True for a `/series/<id>/` work page.
        """
        return _SERIES_PATH.match(urlparse(url).path) is not None

    def series_urls(self, url: str) -> list[str]:
        """List every readable episode a work page links to.

        The page's バックナンバー list runs oldest first and only links the
        episodes still free to read; the others carry a note instead of a
        button and are left out, since there is nothing to fetch for them.

        Args:
            url: A `/series/<id>/` URL.

        Returns:
            One episode URL per linked episode, oldest first.

        Raises:
            UnsupportedUrlError: The URL is not a work page.
            NotAnEpisodePageError: The page links no episode.
        """
        if not self.is_series(url):
            msg = f"{url} is not a series page."
            raise UnsupportedUrlError(msg)
        urls = list(self._listing(url).urls)
        if not urls:
            msg = f"the series at {url} lists no readable episode."
            raise NotAnEpisodePageError(msg)
        return urls

    def episode(self, url: str) -> Episode:
        """Read one episode and list its pages.

        An episode whose free run has ended is gone from the site altogether
        -- its directory answers 404 -- which is reported as not an episode
        page, the same as a URL that never was one.

        Args:
            url: The episode URL.

        Returns:
            The episode. `pages` is empty when the reader lists none.

        Raises:
            NotAnEpisodePageError: The page carries no SpeedBinb reader.
        """
        res = self._session.get(url, headers=self.HEADERS, timeout=self.TIMEOUT)
        if res.status_code == HTTPStatus.NOT_FOUND:
            msg = f"{url} is gone (HTTP 404): not an episode, or its free run has ended."
            raise NotAnEpisodePageError(msg)
        res.raise_for_status()
        url = str(res.url or url)
        soup = BeautifulSoup(res.content, "html.parser")

        container = soup.find(class_=_CONTAINER_CLASS)
        if not isinstance(container, Tag):
            msg = f"no SpeedBinb reader on {url}."
            raise NotAnEpisodePageError(msg)

        pages = [
            (urljoin(url, str(div["data-ptimg"])), str(div.get("data-binbsp-spread") or ""))
            for div in container.select("[data-ptimg]")
        ]
        title = soup.title.get_text() if soup.title else ""
        recommend = str(container.get("data-binbsp-recommend") or "")
        listing = self._listing_of(url, recommend)
        series_title, episode_title = split_title(title, listing.title if listing else "")
        prev_url, next_url = _either_side(listing.urls, url) if listing else (None, None)
        return Episode(
            url=url,
            series_title=series_title,
            episode_title=episode_title,
            pages=tuple(Page(url=ptimg, extra={"spread": spread}) for ptimg, spread in pages),
            prev_url=prev_url,
            next_url=next_url,
            metadata={
                "title": title.strip(),
                "direction": str(container.get("data-binbsp-direction") or ""),
                "recommend": recommend,
                "ptimg": [ptimg for ptimg, _ in pages],
            },
            writer=listing.writer if listing else "",
            publisher=self.PUBLISHER,
        )

    def image(self, page: Page, episode: Episode) -> Image.Image:
        """Fetch one page: its `ptimg.json`, then the resource it names, put back together.

        Args:
            page: The page to fetch; `page.url` is its `ptimg.json`.
            episode: The episode the page belongs to.

        Returns:
            The page in reading order.
        """
        return speedbinb.fetch_ptimg_page(self, page.url, referer=episode.url)

    def _listing_of(self, url: str, recommend: str) -> Listing | None:
        """Find the work page an episode belongs to and read it, or None.

        The reader itself does not know: `data-binbsp-recommend` names the
        frame shown past the last page, which links back to the work page
        (`作品詳細`), whose listing gives the series title and the order of
        the episodes. The "next episode" button some of those frames carry
        is hand-written and often stale, so it is not trusted. Anything
        missing along the way means no work page.
        """
        frames = recommend.split(maxsplit=1)
        frame = frames[0].split("#", 1)[0] if frames else ""
        if not frame:
            return None
        res = self._session.get(urljoin(url, frame), headers=self.HEADERS, timeout=self.TIMEOUT)
        if not res.is_success:
            return None
        soup = BeautifulSoup(res.content, "html.parser")
        series_url = next(
            (
                urljoin(str(res.url or url), str(anchor["href"]))
                for anchor in soup.find_all("a", href=True)
                if isinstance(anchor, Tag) and anchor.get_text(strip=True) == _DETAIL_LINK_TEXT
            ),
            None,
        )
        if series_url is None or not self.is_series(series_url):
            return None
        try:
            return self._listing(series_url)
        except HTTPError:
            return None

    def _listing(self, series_url: str) -> Listing:
        """Read a work page, once per work."""
        parsed = urlparse(series_url)
        key = f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}/"
        if key not in self._listings:
            res = self._get(key)
            soup = BeautifulSoup(res.content, "html.parser")
            heading = soup.find("h2", class_="title")
            authors = soup.find("p", class_="authors")
            self._listings[key] = Listing(
                title=heading.get_text(strip=True) if isinstance(heading, Tag) else "",
                urls=tuple(self._episode_links(soup, key)),
                writer=authors.get_text(strip=True) if isinstance(authors, Tag) else "",
            )
        return self._listings[key]

    @staticmethod
    def _episode_links(soup: BeautifulSoup, url: str) -> list[str]:
        """The `/p_data/` links of a work page's episode list, in document order, deduplicated.

        Only the `li.episode` entries count: the pickup block above the list
        repeats the newest episodes, newest first.
        """
        links: list[str] = []
        for anchor in soup.select("li.episode a[href]"):
            href = str(anchor["href"]).split("?", 1)[0].split("#", 1)[0]
            absolute = urljoin(url, href)
            if _EPISODE_PATH.match(urlparse(absolute).path) and absolute not in links:
                links.append(absolute)
        return links


def _either_side(urls: tuple[str, ...], url: str) -> tuple[str | None, str | None]:
    """The URLs either side of `url` in `urls`, trailing slash or not; None at either end."""
    wanted = url.rstrip("/")
    listed = next((candidate for candidate in urls if candidate.rstrip("/") == wanted), None)
    return neighbours(urls, listed) if listed is not None else (None, None)
