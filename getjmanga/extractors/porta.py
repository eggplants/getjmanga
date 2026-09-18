"""COMICポルタ (イースト・プレス): a WordPress catalogue in front of static SpeedBinb exports.

Every free episode is a directory `/p_data/<slug>/` holding Voyager's
SpeedBinb reader in its "PtBinb" form: the page HTML lists one
`<div data-ptimg="data/NNNN.ptimg.json">` per page, and each of those JSON
files names a scrambled JPEG next to it plus the list of rectangles to copy
out of it to rebuild the page. No API, no cookie, no Referer check.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from http import HTTPStatus
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from bs4.element import Tag
from PIL import Image
from requests import RequestException

from .common import Episode, Extractor, GetjmangaError, NotAnEpisodePageError, Page, UnsupportedUrlError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from requests import Session

# An episode: the directory of one SpeedBinb export.
_EPISODE_PATH = re.compile(r"^/p_data/(?P<slug>[^/]+)/?$")

# A work page, listing the episodes oldest first.
_SERIES_PATH = re.compile(r"^/series/(?P<id>\d+)/?$")

# The reader's `<title>` is `"<series>　<episode>"`, the two joined by an
# ideographic space -- or, on a hand-edited page, by a run of plain ones.
_TITLE_SEPARATOR = re.compile(r"　|[ \t]{2,}")

# Whatever separates the series from the episode once the series is known:
# whitespace, or a colon, slash, bar or dash in either width.
_TITLE_GAP = re.compile(r"^[\s　:/|\-\uff1a\uff0f\uff5c\uff0d]+")

# One entry of a ptimg `coords` list: `"<resource>:<x>,<y>+<w>,<h>><dx>,<dy>"`,
# a `w` x `h` rectangle at (`x`, `y`) of the resource, to land at (`dx`, `dy`).
_COORD = re.compile(r"^(?P<res>[^:]+):(?P<x>\d+),(?P<y>\d+)\+(?P<w>\d+),(?P<h>\d+)>(?P<dx>\d+),(?P<dy>\d+)$")

# The element the reader hangs its page list on.
_CONTAINER_CLASS = "ptbinb-container"

# The link back to the work page that the last-page frame of every episode carries.
_DETAIL_LINK_TEXT = "作品詳細"


@dataclass(frozen=True)
class Transfer:
    """One rectangle to copy out of a scrambled resource into the page."""

    resource: str
    x: int
    y: int
    width: int
    height: int
    dest_x: int
    dest_y: int


@dataclass(frozen=True)
class Listing:
    """What a work page says: its title and the episodes it links, oldest first."""

    title: str
    urls: tuple[str, ...]


@dataclass(frozen=True)
class Ptimg:
    """What one `NNNN.ptimg.json` says: the resources to fetch and how to lay them out."""

    #: Resource key -> absolute image URL.
    resources: dict[str, str]
    width: int
    height: int
    transfers: tuple[Transfer, ...]


def parse_ptimg(data: Mapping[str, Any], url: str) -> Ptimg:
    """Read a page's `ptimg.json`.

    Args:
        data: The decoded JSON.
        url: Where the JSON came from; its resources are named relative to it.

    Returns:
        The resources to fetch and the transfers to apply, as the reader would.

    Raises:
        GetjmangaError: The JSON is not a version-1 ptimg the reader would accept.
    """
    if data.get("ptimg-version") != 1:
        msg = f"{url} is not a ptimg-version 1 file: {data.get('ptimg-version')!r}."
        raise GetjmangaError(msg)
    resources = {
        str(key): urljoin(url, str(resource["src"]))
        for key, resource in (data.get("resources") or {}).items()
        if isinstance(resource, dict) and resource.get("src")
    }
    views = data.get("views") or []
    if not resources or not views:
        msg = f"{url} names no resources or no views."
        raise GetjmangaError(msg)
    view = views[0]
    transfers: list[Transfer] = []
    for coord in view.get("coords") or []:
        match = _COORD.match(str(coord))
        if match is None or match["res"] not in resources:
            msg = f"{url} holds an unreadable transfer: {coord!r}."
            raise GetjmangaError(msg)
        transfers.append(
            Transfer(
                resource=match["res"],
                x=int(match["x"]),
                y=int(match["y"]),
                width=int(match["w"]),
                height=int(match["h"]),
                dest_x=int(match["dx"]),
                dest_y=int(match["dy"]),
            ),
        )
    if not transfers:
        msg = f"{url} lists no transfers."
        raise GetjmangaError(msg)
    return Ptimg(resources=resources, width=int(view["width"]), height=int(view["height"]), transfers=tuple(transfers))


def descramble(ptimg: Ptimg, images: Mapping[str, Image.Image]) -> Image.Image:
    """Put a page back together from its scrambled resources.

    The reader draws onto a blank canvas of the view's size and copies every
    transfer's rectangle across; the resources are larger than the page,
    since the tiles sit in them with a gutter in between.

    Args:
        ptimg: The parsed `ptimg.json`.
        images: The decoded resource images, by resource key.

    Returns:
        A new image, the page in reading order.
    """
    mode = next(iter(images.values())).mode
    out = Image.new(mode if mode in ("L", "RGB") else "RGB", (ptimg.width, ptimg.height), "white")
    for transfer in ptimg.transfers:
        source = images[transfer.resource]
        tile = source.crop(
            (
                transfer.x,
                transfer.y,
                transfer.x + transfer.width,
                transfer.y + transfer.height,
            ),
        )
        out.paste(tile, (transfer.dest_x, transfer.dest_y))
    return out


def split_title(title: str, series: str = "") -> tuple[str, str]:
    """Split the reader's `<title>` into the series and the episode.

    Args:
        title: The `<title>` text, `"<series>　<episode>"`.
        series: The series title, when the work page said what it is; the
            episode is then whatever follows it, however the two are joined.

    Returns:
        The two halves. With no separator to split on, the episode is the
        whole title, and so is the series unless one was given.
    """
    title = title.strip()
    rest = title.removeprefix(series) if series else title
    if series and rest != title and (not rest or _TITLE_GAP.match(rest)):
        return series, _TITLE_GAP.sub("", rest) or title
    parts = _TITLE_SEPARATOR.split(title, maxsplit=1)
    head = parts[0].strip()
    episode = parts[1].strip() if len(parts) > 1 else ""
    if not episode:
        return series or title, title
    return series or head, episode


class Porta(Extractor):
    """Fetch episodes from COMICポルタ.

    Each `Page.url` is the page's `ptimg.json`; `image()` reads it, fetches the
    resource it names and rebuilds the page, so listing an episode costs one
    request however long it is.
    """

    NAME = "porta"
    HOSTS = ("comic-porta.com",)
    URL_FORMS = (
        "https://comic-porta.com/p_data/<slug>",
        "https://comic-porta.com/series/<id>",
    )

    def __init__(self, session: Session | None = None) -> None:
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
        next_url = _after(listing.urls, url) if listing else None
        return Episode(
            url=url,
            series_title=series_title,
            episode_title=episode_title,
            pages=tuple(Page(url=ptimg, extra={"spread": spread}) for ptimg, spread in pages),
            next_url=next_url,
            metadata={
                "title": title.strip(),
                "direction": str(container.get("data-binbsp-direction") or ""),
                "recommend": recommend,
                "ptimg": [ptimg for ptimg, _ in pages],
            },
        )

    def image(self, page: Page, episode: Episode) -> Image.Image:
        """Fetch one page: its `ptimg.json`, then the resource it names, put back together.

        Args:
            page: The page to fetch; `page.url` is its `ptimg.json`.
            episode: The episode the page belongs to.

        Returns:
            The page in reading order.
        """
        headers = {**self.HEADERS, "Referer": episode.url}
        ptimg = parse_ptimg(self._get(page.url, headers=headers).json(), page.url)
        images = {key: self._fetch_image(src, headers=headers) for key, src in ptimg.resources.items()}
        return descramble(ptimg, images)

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
        if not res.ok:
            return None
        soup = BeautifulSoup(res.content, "html.parser")
        series_url = next(
            (
                urljoin(res.url or url, str(anchor["href"]))
                for anchor in soup.find_all("a", href=True)
                if isinstance(anchor, Tag) and anchor.get_text(strip=True) == _DETAIL_LINK_TEXT
            ),
            None,
        )
        if series_url is None or not self.is_series(series_url):
            return None
        try:
            return self._listing(series_url)
        except RequestException:
            return None

    def _listing(self, series_url: str) -> Listing:
        """Read a work page, once per work."""
        parsed = urlparse(series_url)
        key = f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}/"
        if key not in self._listings:
            res = self._get(key)
            soup = BeautifulSoup(res.content, "html.parser")
            heading = soup.find("h2", class_="title")
            self._listings[key] = Listing(
                title=heading.get_text(strip=True) if isinstance(heading, Tag) else "",
                urls=tuple(self._episode_links(soup, key)),
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


def _after(urls: tuple[str, ...], url: str) -> str | None:
    """The URL that follows `url` in `urls`, trailing slash or not, or None."""
    wanted = url.rstrip("/")
    for index, candidate in enumerate(urls):
        if candidate.rstrip("/") == wanted:
            return urls[index + 1] if index + 1 < len(urls) else None
    return None
