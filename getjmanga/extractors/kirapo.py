"""きら星ポータル (きらポ, フレックスコミックス): one catalogue for its imprints, in front of static SpeedBinb exports.

The portal took over COMICメテオ (`/meteor/`) and COMICポラリス (`/polaris/`)
and also carries ambre, astir, etoile, zulet and whatever imprint comes next,
each as one path prefix of the same Laravel app. A work page
`/<imprint>/titles/<slug>` lists the episodes newest first, and every one
still free to read links its reader at `/pt/<imprint>/<slug>/<id>/viewer`:
Voyager's SpeedBinb in its "PtBinb" form, the same static export COMICポルタ
serves, so the `ptimg.json` parsing and the descrambling are `viewers/speedbinb.py`'s.
No API, no cookie, no Referer check, no account. An episode whose free run
has ended is dropped from the listing and its reader answers 404.
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

# The reader of one episode: the imprint, the work's slug and the episode's id.
_EPISODE_PATH = re.compile(r"^/pt/(?P<imprint>[a-z0-9_-]+)/(?P<slug>[^/]+)/(?P<id>\d+)/viewer/?$")

# A work page, listing the episodes newest first.
_SERIES_PATH = re.compile(r"^/(?P<imprint>[a-z0-9_-]+)/titles/(?P<slug>[^/]+)/?$")

# The element the reader hangs its page list on.
_CONTAINER_CLASS = "ptbinb-container"

# The work page's episode list and its entries.
_LISTING_SELECTOR = ".episodes-container .episode-item"
_READ_LINK_SELECTOR = "a.episode-read[href]"
_ENTRY_TITLE_SELECTOR = ".episode-item-left"


@dataclass(frozen=True)
class Listing:
    """What a work page says: its title and the readable episodes, oldest first."""

    title: str
    #: Reader URL -> the episode's name as the list shows it (`第1話`).
    episodes: dict[str, str]
    #: The `作者` links, `役割：名前` each, as `名前 (役割)`.
    writer: str = ""


class Kirapo(Extractor):
    """Fetch episodes from きら星ポータル, whichever imprint they belong to.

    Each `Page.url` is the page's `ptimg.json`; `image()` reads it, fetches the
    resource it names and rebuilds the page, so listing an episode costs one
    request however long it is -- plus the work page, read once per work,
    for the series title, the episode's name and what follows it.
    """

    NAME = "kirapo"
    HOSTS = ("kirapo.jp",)
    PUBLISHER = "フレックスコミックス"
    URL_FORMS = (
        "https://kirapo.jp/pt/<imprint>/<slug>/<id>/viewer",
        "https://kirapo.jp/<imprint>/titles/<slug>",
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
            True for an https reader or work page on the portal.
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
            True for a `/<imprint>/titles/<slug>` work page.
        """
        return _SERIES_PATH.match(urlparse(url).path) is not None

    def series_urls(self, url: str) -> list[str]:
        """List every readable episode a work page links to.

        The list runs newest first and only links the episodes still free to
        read; the others say their run has ended, or want a purchase from
        another store, and are left out since there is nothing to fetch.

        Args:
            url: A `/<imprint>/titles/<slug>` URL.

        Returns:
            One reader URL per linked episode, oldest first.

        Raises:
            UnsupportedUrlError: The URL is not a work page.
            NotAnEpisodePageError: The page links no episode.
        """
        if not self.is_series(url):
            msg = f"{url} is not a series page."
            raise UnsupportedUrlError(msg)
        urls = list(self._listing(url).episodes)
        if not urls:
            msg = f"the series at {url} lists no readable episode."
            raise NotAnEpisodePageError(msg)
        return urls

    def episode(self, url: str) -> Episode:
        """Read one episode and list its pages.

        An episode whose free run has ended is gone from the site altogether
        -- its reader answers 404 -- which is reported as not an episode
        page, the same as a URL that never was one.

        Args:
            url: The reader URL.

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
        listing = self._listing_of(url)
        series_title, episode_title = split_title(title, listing.title if listing else "")
        prev_url = next_url = None
        if listing:
            episode_title = listing.episodes.get(_reader_key(url)) or episode_title
            prev_url, next_url = neighbours(list(listing.episodes), _reader_key(url))
        match = _EPISODE_PATH.match(urlparse(url).path)
        return Episode(
            url=url,
            series_title=series_title,
            episode_title=episode_title,
            pages=tuple(Page(url=ptimg, extra={"spread": spread}) for ptimg, spread in pages),
            prev_url=prev_url,
            next_url=next_url,
            metadata={
                "title": title.strip(),
                "imprint": match["imprint"] if match else "",
                "slug": match["slug"] if match else "",
                "episode_id": match["id"] if match else "",
                "direction": str(container.get("data-binbsp-direction") or ""),
                "recommend": str(container.get("data-binbsp-recommend") or ""),
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

    def _listing_of(self, url: str) -> Listing | None:
        """Read the work page a reader URL belongs to, or None when it cannot be read.

        The reader does not name its work; the URL does: `/pt/<imprint>/<slug>/...`
        is read at `/<imprint>/titles/<slug>`. A work page that fails to load
        costs the episode its series title and its `next_url`, nothing more.
        """
        match = _EPISODE_PATH.match(urlparse(url).path)
        if match is None:
            return None
        series_url = f"{self._origin(url)}/{match['imprint']}/titles/{match['slug']}"
        try:
            return self._listing(series_url)
        except HTTPError:
            return None

    def _listing(self, series_url: str) -> Listing:
        """Read a work page, once per work."""
        parsed = urlparse(series_url)
        key = f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}"
        if key not in self._listings:
            res = self._get(key)
            soup = BeautifulSoup(res.content, "html.parser")
            heading = soup.find("h2")
            self._listings[key] = Listing(
                title=heading.get_text(strip=True) if isinstance(heading, Tag) else "",
                episodes=_episode_entries(soup, key),
                writer=_credits(soup),
            )
        return self._listings[key]


def _credits(soup: BeautifulSoup) -> str:
    """The links under the `作者` heading, `役割：名前` each, as `名前 (役割)`."""
    credited = []
    for anchor in soup.select('a[href*="/authors/"]'):
        role, sep, name = anchor.get_text(strip=True).partition("：")
        credited.append(f"{name} ({role})" if sep and name else role)
    return ", ".join(credit for credit in credited if credit)


def _episode_entries(soup: BeautifulSoup, url: str) -> dict[str, str]:
    """The reader links of a work page's episode list with their names, oldest first, deduplicated.

    Only the entries of the list count -- the header repeats the newest and
    the first episode -- and only those with a `読む` button: an ended
    episode carries a note instead, a purchase entry links other stores.
    """
    entries: dict[str, str] = {}
    for item in soup.select(_LISTING_SELECTOR):
        anchor = item.select_one(_READ_LINK_SELECTOR)
        if anchor is None:
            continue
        href = str(anchor["href"]).split("?", 1)[0].split("#", 1)[0]
        absolute = _reader_key(urljoin(url, href))
        if _EPISODE_PATH.match(urlparse(absolute).path) is None or absolute in entries:
            continue
        name = item.select_one(_ENTRY_TITLE_SELECTOR)
        entries[absolute] = name.get_text(strip=True) if name is not None else ""
    return dict(reversed(entries.items()))


def _reader_key(url: str) -> str:
    """`url` as the listing spells it: without a trailing slash."""
    return url.rstrip("/")
