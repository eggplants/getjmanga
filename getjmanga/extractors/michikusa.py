"""路草 (トゥーヴァージンズ): a WordPress catalogue in front of static SpeedBinb exports.

The site is the same shape as COMICポルタ (`porta.py`). A work page at
`https://michikusacomics.jp/product/<slug>` lists the episodes that are
public right now, oldest first, each as a link into a directory
`/wp-content/uploads/data/<work>/<episode>/index.html` holding Voyager's
SpeedBinb reader in its static "PtBinb" form: the page HTML lists one
`<div data-ptimg="data/NNNN.ptimg.json">` per page, and each JSON names a
scrambled JPEG next to it plus the rectangles to copy out of it to rebuild
the page. `parse_ptimg()` and `descramble_ptimg()` are `viewers/speedbinb.py`'s. No API, no
cookie, no Referer check, no account.

An episode whose run has ended is deleted (its directory answers 404), so
there is no locked state to report. The reader's `<title>` is hand-written
and inconsistent (`"べじたぶるサンドイッチ　その1　つくしとわらび"`,
`"88KOKURAボーイズ 第２話"`, `"Roaming"`), so the titles come from the work
page whenever the frame past the last page (`last.html`) links back to one.
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
from getjmanga.extractor import Episode, Extractor, Page, neighbours, ordinal
from getjmanga.viewers import speedbinb
from getjmanga.viewers.speedbinb import split_title

if TYPE_CHECKING:
    from httpx import Client
    from PIL import Image

HOST = "michikusacomics.jp"

# An episode: the directory of one SpeedBinb export, with or without its `index.html`.
_EPISODE_PATH = re.compile(r"^/wp-content/uploads/data/(?P<work>[^/]+)/(?P<episode>[^/]+)/?(?:index\.html)?$")

# A work page. `/product` itself, its pagination and its feed are not works.
_SERIES_PATH = re.compile(r"^/product/(?!(?:page|feed)/?$)(?P<slug>[\w-]+)/?$")

# The element the reader hangs its page list on.
_CONTAINER_CLASS = "ptbinb-container"

# The ideographic space the site joins an episode's number and its subtitle with.
_TITLE_JOIN = "　"


@dataclass(frozen=True)
class Listing:
    """What a work page says: its title and the episodes it links, oldest first."""

    title: str
    #: Episode URL -> the episode's title (`"<number>　<subtitle>"`), oldest first.
    episodes: dict[str, str]
    #: The `作者プロフィール` name.
    writer: str = ""

    @property
    def urls(self) -> tuple[str, ...]:
        """The episode URLs, oldest first."""
        return tuple(self.episodes)


def episode_url(url: str) -> str | None:
    """The canonical `.../<work>/<episode>/index.html` of an episode URL, or None for another shape."""
    parsed = urlparse(url)
    match = _EPISODE_PATH.match(parsed.path)
    if match is None:
        return None
    return f"https://{HOST}/wp-content/uploads/data/{match['work']}/{match['episode']}/index.html"


def parse_listing(html: str | bytes, url: str) -> Listing:
    """Read a work page into a `Listing`.

    The page lists its public episodes twice: a plain list for desktop, in
    order, and a sortable card list for phones carrying each episode's
    number, subtitle and date. The URLs are taken in document order
    (desktop first, oldest first) and the titles from the cards.

    Args:
        html: The page.
        url: The URL it came from, to resolve links against.

    Returns:
        The work's title and its episodes, oldest first, deduplicated.
    """
    soup = BeautifulSoup(html, "html.parser")
    heading = soup.select_one("h1.entry-title")
    title = heading.get_text(strip=True) if isinstance(heading, Tag) else ""

    labels: dict[str, str] = {}
    for anchor in soup.select(".released_episodes a[href]"):
        canonical = episode_url(urljoin(url, str(anchor["href"])))
        if canonical is None:
            continue
        info = anchor.select_one(".info")
        if isinstance(info, Tag):
            # `<div>その１</div><div class="title">つくしとわらび</div><div class="publication_date">…</div>`
            number = next((div.get_text(strip=True) for div in info.find_all("div") if not div.get("class")), "")
            subtitle_div = info.select_one(".title")
            subtitle = subtitle_div.get_text(strip=True) if isinstance(subtitle_div, Tag) else ""
            label = f"{number}{_TITLE_JOIN}{subtitle}" if number and subtitle else number or subtitle
        else:
            label = anchor.get_text(strip=True)
        # The desktop list comes first and sets the order; the cards say more.
        labels.setdefault(canonical, "")
        if len(label) > len(labels[canonical]):
            labels[canonical] = label
    author = soup.select_one("#authorName")
    return Listing(title=title, episodes=labels, writer=author.get_text(strip=True) if author is not None else "")


class Michikusa(Extractor):
    """Fetch episodes from 路草.

    Each `Page.url` is the page's `ptimg.json`; `image()` reads it, fetches
    the resource it names and rebuilds the page, so listing an episode costs
    one request (plus the last-page frame and the work page) however long
    it is.
    """

    NAME = "michikusa"
    HOSTS = (HOST,)
    PUBLISHER = "トゥーヴァージンズ"
    URL_FORMS = (
        "https://michikusacomics.jp/wp-content/uploads/data/<work>/<episode>/index.html",
        "https://michikusacomics.jp/product/<slug>",
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
            True for an https episode directory or work page on the site.
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
            True for a `/product/<slug>` work page.
        """
        return _SERIES_PATH.match(urlparse(url).path) is not None

    def series_urls(self, url: str) -> list[str]:
        """List every public episode a work page links to, oldest first.

        Args:
            url: A `/product/<slug>` URL.

        Returns:
            One episode URL per linked episode, oldest first.

        Raises:
            UnsupportedUrlError: The URL is not a work page.
            NotAnEpisodePageError: The page links no episode (or is not there).
        """
        if not self.is_series(url):
            msg = f"{url} is not a work page."
            raise UnsupportedUrlError(msg)
        urls = list(self._listing(url).urls)
        if not urls:
            msg = f"the work at {url} lists no public episode."
            raise NotAnEpisodePageError(msg)
        return urls

    def episode(self, url: str) -> Episode:
        """Read one episode and list its pages.

        An episode whose run has ended is gone from the site altogether --
        its directory answers 404 -- which is reported as not an episode
        page, the same as a URL that never was one.

        Args:
            url: The episode URL.

        Returns:
            The episode. `pages` is empty when the reader lists none.

        Raises:
            UnsupportedUrlError: The URL is not an episode directory.
            NotAnEpisodePageError: The page carries no SpeedBinb reader.
        """
        canonical = episode_url(url)
        if canonical is None:
            msg = f"{url} is not an episode page."
            raise UnsupportedUrlError(msg)
        res = self._session.get(canonical, headers=self.HEADERS, timeout=self.TIMEOUT)
        if res.status_code == HTTPStatus.NOT_FOUND:
            msg = f"{url} is gone (HTTP 404): not an episode, or its run has ended."
            raise NotAnEpisodePageError(msg)
        res.raise_for_status()
        soup = BeautifulSoup(res.content, "html.parser")

        container = soup.find(class_=_CONTAINER_CLASS)
        if not isinstance(container, Tag):
            msg = f"no SpeedBinb reader on {canonical}."
            raise NotAnEpisodePageError(msg)

        pages = [
            (urljoin(canonical, str(div["data-ptimg"])), str(div.get("data-binbsp-spread") or ""))
            for div in container.select("[data-ptimg]")
        ]
        title = soup.title.get_text().strip() if soup.title else ""
        recommend = str(container.get("data-binbsp-recommend") or "")
        work_url, frame_next = self._frame_links(canonical, recommend)
        listing = self._listing_or_none(work_url) if work_url else None

        if listing is not None and listing.episodes.get(canonical):
            series_title, episode_title = listing.title, listing.episodes[canonical]
        else:
            series_title, episode_title = split_title(title, listing.title if listing else "")
        # The work page orders the episodes; the frame's own "next" button
        # only stands in when the page is unavailable or does not list this one.
        prev_url, next_url = neighbours(listing.urls, canonical) if listing else (None, None)
        if next_url is None and (listing is None or canonical not in listing.episodes):
            next_url = frame_next
        return self._dated_by_upload(
            Episode(
                url=canonical,
                series_title=series_title,
                episode_title=episode_title,
                pages=tuple(Page(url=ptimg, extra={"spread": spread}) for ptimg, spread in pages),
                prev_url=prev_url,
                next_url=next_url,
                metadata={
                    "title": title,
                    "direction": str(container.get("data-binbsp-direction") or ""),
                    "recommend": recommend,
                    "work_url": work_url,
                    "ptimg": [ptimg for ptimg, _ in pages],
                },
                writer=listing.writer if listing else "",
                publisher=self.PUBLISHER,
                number=ordinal(listing.urls, canonical) if listing else None,
            )
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

    def _frame_links(self, url: str, recommend: str) -> tuple[str | None, str | None]:
        """Read the frame past the last page: the work page it links back to, and its "next" button.

        `data-binbsp-recommend` names the frame (`last.html[next]`), a
        WordPress-made page whose `home` button links the work page and
        whose `next_story` button, when there is one, the episode after.
        Anything missing along the way means None.
        """
        frames = recommend.split(maxsplit=1)
        frame = frames[0].split("[", 1)[0].split("#", 1)[0] if frames else ""
        if not frame:
            return None, None
        try:
            res = self._session.get(urljoin(url, frame), headers=self.HEADERS, timeout=self.TIMEOUT)
        except HTTPError:
            return None, None
        if not res.is_success:
            return None, None
        soup = BeautifulSoup(res.content, "html.parser")
        base = str(res.url or url)
        work_url = None
        next_url = None
        for anchor in soup.select("a[href]"):
            href = urljoin(base, str(anchor["href"]))
            parsed = urlparse(href)
            if parsed.hostname != HOST:
                continue
            if work_url is None and _SERIES_PATH.match(parsed.path):
                work_url = f"https://{HOST}{parsed.path.rstrip('/')}"
            elif next_url is None and episode_url(href) is not None and _in_next_button(anchor):
                next_url = episode_url(href)
        return work_url, next_url

    def _listing_or_none(self, work_url: str) -> Listing | None:
        """Read a work page, or None when the site will not serve it."""
        try:
            return self._listing(work_url)
        except (HTTPError, NotAnEpisodePageError):
            return None

    def _listing(self, series_url: str) -> Listing:
        """Read a work page, once per work."""
        parsed = urlparse(series_url)
        key = f"https://{HOST}{parsed.path.rstrip('/')}"
        if key not in self._listings:
            res = self._session.get(key, headers=self.HEADERS, timeout=self.TIMEOUT)
            if res.status_code == HTTPStatus.NOT_FOUND:
                msg = f"{series_url} is gone (HTTP 404): not a work page."
                raise NotAnEpisodePageError(msg)
            res.raise_for_status()
            self._listings[key] = parse_listing(res.content, str(res.url or key))
        return self._listings[key]


def _in_next_button(anchor: Tag) -> bool:
    """Whether `anchor` sits in the frame's `next_story` button."""
    return anchor.find_parent(class_="next_story") is not None
