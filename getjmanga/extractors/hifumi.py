"""コミックポルカ and コミックノヴァ (一二三書房): WordPress work pages in front of static SpeedBinb exports.

The two imprints share one domain and one theme: a work page at
`https://www.123hon.com/<imprint>/web-comic/<slug>/` lists the episodes
oldest first, each one still free to read as a `読む` button that opens
`https://www.123hon.com/vw/<slug>/<id>/` in a new tab. That directory is
Voyager's SpeedBinb reader in its static "PtBinb" form -- the one
`viewers/speedbinb.py` reads: one `<div data-ptimg="data/NNNN.ptimg.json">` per page,
each JSON naming a scrambled JPEG next to it and the rectangles to copy out
of it. No API, no cookie, no Referer check; the files are on S3 behind
CloudFront.

The reader does not know which imprint it belongs to (its `<title>` is the
series title alone), but the frame shown past the last page links back to
the work page, whose listing gives the episode titles and the order to walk
them in. Older frames link the retired `<imprint>.123hon.com` hosts, which
are mapped onto the current shape.

An episode whose free run has ended loses its button (the listing shows
`公開終了しました。` and a `購入` link instead), but its directory stays up,
so one whose URL is still known reads as before. A directory that never
existed answers 403 (S3's AccessDenied), which is reported as not an
episode page.
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

#: The one host both imprints live on.
HOST = "www.123hon.com"
#: The imprints, as their path prefix on `HOST` (and the subdomain they once had).
IMPRINTS = ("nova", "polca")

# A work page: `/<imprint>/web-comic/<slug>/`, or its old `/book_series/` shape that redirects there.
_WORK_PATH = re.compile(r"^/(?P<imprint>nova|polca)/(?:web-comic|book_series)/(?P<slug>[^/]+)/?$")
# The same on a retired `<imprint>.123hon.com` host, where the imprint was the host.
_OLD_WORK_PATH = re.compile(r"^/(?:web-comic|book_series)/(?P<slug>[^/]+)/?$")
# An episode: the directory of one SpeedBinb export, with or without its `index.html`.
_EPISODE_PATH = re.compile(r"^/vw/(?P<slug>[^/]+)/(?P<id>[^/]+)(?:/(?:index\.html)?)?$")

# The element the reader hangs its page list on.
_CONTAINER_CLASS = "ptbinb-container"


@dataclass(frozen=True)
class Listing:
    """What a work page says: its title and the episodes it links, oldest first."""

    #: The canonical work page URL.
    url: str
    #: `div.title-area h2`: the work's title.
    title: str
    #: Canonical episode URL -> the episode's title (`第1話`, `予告編`, ...), oldest first.
    episodes: dict[str, str]


def episode_url(url: str) -> str | None:
    """The canonical `https://www.123hon.com/vw/<slug>/<id>/` of an episode URL, or None for another shape."""
    parsed = urlparse(url)
    if parsed.hostname != HOST:
        return None
    match = _EPISODE_PATH.match(parsed.path)
    if match is None:
        return None
    return f"https://{HOST}/vw/{match['slug']}/{match['id']}/"


def work_url(url: str) -> str | None:
    """The canonical `https://www.123hon.com/<imprint>/web-comic/<slug>/` of a work page URL, or None.

    Args:
        url: A link, on the current host or on a retired `<imprint>.123hon.com` one.

    Returns:
        The canonical work page URL, or None when the link is not a work page.
    """
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if host == HOST:
        match = _WORK_PATH.match(parsed.path)
        if match is None:
            return None
        imprint, slug = match["imprint"], match["slug"]
    else:
        imprint = host.removesuffix(".123hon.com")
        match = _OLD_WORK_PATH.match(parsed.path)
        if imprint == host or imprint not in IMPRINTS or match is None:
            return None
        slug = match["slug"]
    return f"https://{HOST}/{imprint}/web-comic/{slug}/"


def parse_work(html: str | bytes, url: str) -> Listing:
    """Read a work page into a `Listing`.

    Only the episode list counts: the header repeats the newest and the
    first episode above it, and an expired episode has a `購入` anchor in
    place of its `読む` button, which is not an episode link.

    Args:
        html: The page.
        url: The canonical work page URL.

    Returns:
        The work's title and the episodes it links, oldest first, deduplicated.

    Raises:
        NotAnEpisodePageError: The page is not a work page.
    """
    soup = BeautifulSoup(html, "html.parser")
    heading = soup.select_one("div.title-area h2")
    listing = soup.select_one("ul.item-list")
    if not isinstance(heading, Tag) and not isinstance(listing, Tag):
        msg = f"no work page at {url}."
        raise NotAnEpisodePageError(msg)
    episodes: dict[str, str] = {}
    for item in listing.select("li") if isinstance(listing, Tag) else []:
        anchor = item.select_one("div.btn a[href]")
        if anchor is None:
            continue
        canonical = episode_url(urljoin(url, str(anchor["href"])))
        if canonical is None:
            continue
        number = item.select_one("span.story-num")
        episodes.setdefault(canonical, number.get_text(strip=True) if isinstance(number, Tag) else "")
    return Listing(
        url=url,
        title=heading.get_text(strip=True) if isinstance(heading, Tag) else "",
        episodes=episodes,
    )


class Hifumi(Extractor):
    """Fetch episodes from コミックポルカ and コミックノヴァ.

    Each `Page.url` is the page's `ptimg.json`; `image()` reads it, fetches
    the resource it names and rebuilds the page, so listing an episode costs
    one request however long it is. A work page is read once per run.
    """

    NAME = "hifumi"
    HOSTS = (HOST,)
    PUBLISHER = "一二三書房"
    URL_FORMS = (
        "https://www.123hon.com/vw/<slug>/<id>",
        "https://www.123hon.com/polca/web-comic/<slug>/",
        "https://www.123hon.com/nova/web-comic/<slug>/",
    )

    def __init__(self, session: Client | None = None) -> None:
        """Build an extractor.

        Args:
            session: A session to reuse. A retrying one is made when omitted.
        """
        super().__init__(session)
        # Canonical work page URL -> what it lists.
        self._listings: dict[str, Listing] = {}

    @classmethod
    def suitable(cls, url: str) -> bool:
        """Report whether `url` is an episode or a work page of either imprint.

        Args:
            url: The URL to check.

        Returns:
            True for `https://www.123hon.com/vw/<slug>/<id>` and
            `https://www.123hon.com/<polca|nova>/web-comic/<slug>/`.
        """
        if not super().suitable(url):
            return False
        return episode_url(url) is not None or work_url(url) is not None

    @classmethod
    def is_series(cls, url: str) -> bool:
        """Report whether `url` is a work page.

        Args:
            url: The URL to check.

        Returns:
            True for `https://www.123hon.com/<polca|nova>/web-comic/<slug>/`.
        """
        return urlparse(url).hostname == HOST and work_url(url) is not None

    def series_urls(self, url: str) -> list[str]:
        """List the episodes a work page links, oldest first.

        Args:
            url: A work page URL.

        Returns:
            One episode URL per `読む` button, oldest first.

        Raises:
            UnsupportedUrlError: The URL is not a work page.
            NotAnEpisodePageError: The page links no episode.
        """
        canonical = work_url(url) if urlparse(url).hostname == HOST else None
        if canonical is None:
            msg = f"{url} is not a work page."
            raise UnsupportedUrlError(msg)
        listing = self._listing(canonical)
        if not listing.episodes:
            msg = f"the work at {url} lists no readable episode."
            raise NotAnEpisodePageError(msg)
        return list(listing.episodes)

    def episode(self, url: str) -> Episode:
        """Read one episode and list its pages.

        Args:
            url: The episode URL.

        Returns:
            The episode. `pages` is empty when the reader lists none.

        Raises:
            UnsupportedUrlError: The URL is not an episode URL.
            NotAnEpisodePageError: There is no such directory (403 or 404),
                or it carries no SpeedBinb reader.
        """
        canonical = episode_url(url)
        if canonical is None:
            msg = f"{url} is not an episode page."
            raise UnsupportedUrlError(msg)
        slug, viewer_id = canonical.rstrip("/").split("/")[-2:]

        res = self._session.get(canonical, headers=self.HEADERS, timeout=self.TIMEOUT)
        if res.status_code in (HTTPStatus.FORBIDDEN, HTTPStatus.NOT_FOUND):
            msg = f"{url} is gone (HTTP {res.status_code}): no such episode."
            raise NotAnEpisodePageError(msg)
        res.raise_for_status()
        soup = BeautifulSoup(res.content, "html.parser")
        container = soup.find(class_=_CONTAINER_CLASS)
        if not isinstance(container, Tag):
            msg = f"no SpeedBinb reader on {url}."
            raise NotAnEpisodePageError(msg)

        pages = [
            (urljoin(canonical, str(div["data-ptimg"])), str(div.get("data-binbsp-spread") or ""))
            for div in container.select("[data-ptimg]")
        ]
        title = soup.title.get_text().strip() if soup.title else ""
        recommend = str(container.get("data-binbsp-recommend") or "")
        listing = self._listing_of(canonical, recommend)
        series_title, episode_title = _titles(title, listing, canonical, slug=slug, viewer_id=viewer_id)
        return Episode(
            url=canonical,
            series_title=series_title,
            episode_title=episode_title,
            pages=tuple(Page(url=ptimg, extra={"spread": spread}) for ptimg, spread in pages),
            prev_url=_either_side(listing, canonical)[0],
            next_url=_either_side(listing, canonical)[1],
            metadata={
                "title": title,
                "slug": slug,
                "viewer_id": viewer_id,
                "work_url": listing.url if listing else None,
                "listed": bool(listing and canonical in listing.episodes),
                "direction": str(container.get("data-binbsp-direction") or ""),
                "recommend": recommend,
                "ptimg": [ptimg for ptimg, _ in pages],
            },
            # Neither imprint credits an author anywhere but the copyright line.
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

        `data-binbsp-recommend` names the frame shown past the last page,
        which links back to the work page (`シリーズ一覧`). Anything missing
        along the way means no work page.
        """
        frames = recommend.split(maxsplit=1)
        frame = frames[0].split("#", 1)[0] if frames else ""
        if not frame:
            return None
        try:
            res = self._session.get(urljoin(url, frame), headers=self.HEADERS, timeout=self.TIMEOUT)
        except HTTPError:
            return None
        if not res.is_success:
            return None
        soup = BeautifulSoup(res.content, "html.parser")
        series_url = next(
            (
                canonical
                for anchor in soup.find_all("a", href=True)
                if isinstance(anchor, Tag) and (canonical := work_url(urljoin(url, str(anchor["href"]))))
            ),
            None,
        )
        if series_url is None:
            return None
        try:
            return self._listing(series_url)
        except (HTTPError, NotAnEpisodePageError):
            return None

    def _listing(self, canonical: str) -> Listing:
        """Read a work page, once per work."""
        if canonical not in self._listings:
            res = self._session.get(canonical, headers=self.HEADERS, timeout=self.TIMEOUT)
            if res.status_code == HTTPStatus.NOT_FOUND:
                msg = f"{canonical} is gone (HTTP 404): not a work page."
                raise NotAnEpisodePageError(msg)
            res.raise_for_status()
            self._listings[canonical] = parse_work(res.content, canonical)
        return self._listings[canonical]


def _titles(title: str, listing: Listing | None, canonical: str, *, slug: str, viewer_id: str) -> tuple[str, str]:
    """The series and episode titles: the work page's when it lists the episode, else the reader's `<title>`.

    The reader's `<title>` is the series title alone, so an episode the work
    page does not list (an expired one) has nothing to be called by but its
    id; a reader without a title at all is named after its directory.

    Args:
        title: The reader's `<title>`.
        listing: The work page, when one was found.
        canonical: The episode URL, as the listing keys it.
        slug: The work's directory under `/vw/`.
        viewer_id: The episode's directory under it.

    Returns:
        The two titles, never empty.
    """
    series = listing.title if listing else ""
    if listing and canonical in listing.episodes and series:
        return series, listing.episodes[canonical]
    series, episode = split_title(title, series)
    if episode == title.strip():
        episode = viewer_id
    return series or slug, episode


def _either_side(listing: Listing | None, canonical: str) -> tuple[str | None, str | None]:
    """The listing's episodes before and after `canonical`, None at either end (or both when unlisted)."""
    return neighbours(list(listing.episodes), canonical) if listing is not None else (None, None)
