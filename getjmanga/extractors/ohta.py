"""Ohta Web Comic (太田出版): work pages on the publisher's site, episodes on YONDEMILL's BinB reader.

The publisher's site has no episode pages of its own. A work page at
`https://webcomic.ohtabooks.com/<slug>/` lists its episodes newest first,
each free one as an `openBook('<id>')` button that opens
`https://www.yondemill.jp/contents/<id>?view=1` in a new window; an expired
one has no button at all (or points at a shop). Reading the content is
`viewers/yondemill.py`'s: the content page, the `?view=1` stub that opens the
SpeedBinb reader, the pages. A paid book opens on its free trial pages only,
and `metadata["shop_url"]` then names the shop. Signing in to YONDEMILL sits
behind reCAPTCHA, so there is no `login()`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from http import HTTPStatus
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from bs4.element import Tag

from getjmanga.errors import NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractor import Episode, Extractor, Page, neighbours
from getjmanga.viewers import yondemill
from getjmanga.viewers.speedbinb import split_title
from getjmanga.viewers.yondemill import CONTENT_HOST, Content, content_url

if TYPE_CHECKING:
    from httpx import Client
    from PIL import Image

#: Where the publisher's work pages are.
WORK_HOST = "webcomic.ohtabooks.com"
# A work page: one path segment, but not the archive listing.
_WORK_PATH = re.compile(r"^/(?!list/?$)(?P<slug>[\w.-]+)/?$")
# The `openBook('<id>')` handler the work page opens an episode with.
_OPEN_BOOK = re.compile(r"openBook\(\s*['\"](?P<id>\d+)['\"]\s*\)")


@dataclass(frozen=True)
class Work:
    """What a work page on the publisher's site says."""

    #: The work page URL.
    url: str
    #: `h2.contentTitle`: the work's title.
    title: str
    #: The listed episodes, oldest first, deduplicated: YONDEMILL content id -> episode title.
    episodes: dict[str, str]


def parse_work(html: str | bytes, url: str) -> Work:
    """Read a work page into a `Work`.

    Args:
        html: The page.
        url: The URL it came from.

    Returns:
        The work's title and its listed episodes, oldest first.

    Raises:
        NotAnEpisodePageError: The page is not a work page.
    """
    soup = BeautifulSoup(html, "html.parser")
    heading = soup.select_one("h2.contentTitle")
    listing = soup.select_one("ul.backnumberList")
    if not isinstance(heading, Tag) and not isinstance(listing, Tag):
        msg = f"no work page at {url}."
        raise NotAnEpisodePageError(msg)

    episodes: dict[str, str] = {}
    # The backnumber list runs newest first; without one, the header's
    # "latest" and "first" buttons stand in, in that order.
    scope = listing if isinstance(listing, Tag) else soup
    for anchor in reversed(scope.select("a[onclick]")):
        match = _OPEN_BOOK.search(str(anchor.get("onclick", "")))
        if match is None:
            continue
        title = anchor.select_one("div.title")
        episodes.setdefault(match["id"], title.get_text(strip=True) if isinstance(title, Tag) else "")
    return Work(
        url=url,
        title=heading.get_text(strip=True) if isinstance(heading, Tag) else "",
        episodes=episodes,
    )


class Ohta(Extractor):
    """Fetch episodes from Ohta Web Comic, through YONDEMILL's SpeedBinb reader.

    An episode URL is a YONDEMILL content URL; a series URL is a work page on
    the publisher's site, which is also where the episode titles and the
    order of the episodes come from. A work page is fetched once per run.
    """

    NAME = "ohta"
    HOSTS = (WORK_HOST, CONTENT_HOST, "yondemill.jp")
    #: The site's own; a YONDEMILL content page names its label, which wins.
    PUBLISHER = "太田出版"
    URL_FORMS = (
        "https://www.yondemill.jp/contents/<id>",
        "https://webcomic.ohtabooks.com/<slug>/",
    )

    def __init__(self, session: Client | None = None) -> None:
        """Build an extractor.

        Args:
            session: A session to reuse. A retrying one is made when omitted.
        """
        super().__init__(session)
        # Work page URL -> what it listed, so a work page is read once.
        self._works: dict[str, Work] = {}

    @classmethod
    def suitable(cls, url: str) -> bool:
        """Report whether `url` is a YONDEMILL content or a work page.

        Args:
            url: The URL to check.

        Returns:
            True for `https://www.yondemill.jp/contents/<id>` (also on
            `yondemill.jp`) and `https://webcomic.ohtabooks.com/<slug>/`.
        """
        if not super().suitable(url):
            return False
        parsed = urlparse(url)
        if parsed.hostname == WORK_HOST:
            return _WORK_PATH.match(parsed.path) is not None
        return content_url(url) is not None

    @classmethod
    def is_series(cls, url: str) -> bool:
        """Report whether `url` is a work page on the publisher's site.

        Args:
            url: The URL to check.

        Returns:
            True for `https://webcomic.ohtabooks.com/<slug>/`.
        """
        parsed = urlparse(url)
        return parsed.hostname == WORK_HOST and _WORK_PATH.match(parsed.path) is not None

    def series_urls(self, url: str) -> list[str]:
        """List the episodes a work page opens, oldest first.

        Args:
            url: A work page URL.

        Returns:
            One YONDEMILL content URL per free episode, oldest first.

        Raises:
            UnsupportedUrlError: The URL is not a work page.
            NotAnEpisodePageError: The page lists no readable episode.
        """
        if _WORK_PATH.match(urlparse(url).path) is None:
            msg = f"{url} is not a work page."
            raise UnsupportedUrlError(msg)
        work = self._work(url)
        if not work.episodes:
            msg = f"the work at {url} lists no readable episode."
            raise NotAnEpisodePageError(msg)
        return [f"https://{CONTENT_HOST}/contents/{content_id}" for content_id in work.episodes]

    def episode(self, url: str) -> Episode:
        """Read one episode: its titles, its pages and the episode after it.

        Args:
            url: A YONDEMILL content URL.

        Returns:
            The episode. `pages` is empty when the content page does not send
            the browser on to the reader.

        Raises:
            UnsupportedUrlError: The URL is not a content URL.
            NotAnEpisodePageError: YONDEMILL has no such content (404), or the
                reader does not describe it.
        """
        canonical = content_url(url)
        if canonical is None:
            msg = f"{url} is not an episode page."
            raise UnsupportedUrlError(msg)
        reading = yondemill.read(self, canonical)
        content = reading.content
        content_id = reading.content_id
        work_url = _work_link(content)

        work = self._work(work_url) if work_url else None
        series_title, episode_title = self._titles(content, work, content_id)
        prev_url, next_url = _neighbour_urls(work, content_id)
        metadata: dict[str, Any] = {
            "content_id": content_id,
            "title": content.title,
            "author": content.author,
            "label": content.label,
            "work_url": work_url,
            "flags": content.flags,
        }

        opened = reading.opened
        if opened is None:
            # No reader to go to: the content wants a purchase or a login first.
            return Episode(
                url=canonical,
                series_title=series_title,
                episode_title=episode_title,
                prev_url=prev_url,
                next_url=next_url,
                metadata={**metadata, "locked": True},
                writer=_credit(content.author),
                publisher=content.label or self.PUBLISHER,
            )
        return self._dated_by_upload(
            Episode(
                url=canonical,
                series_title=series_title,
                episode_title=episode_title,
                pages=opened.book.pages,
                prev_url=prev_url,
                next_url=next_url,
                metadata={
                    **metadata,
                    "binb_id": opened.binb_id,
                    "contents_server": opened.info.server,
                    "reader_title": opened.info.item.get("Title"),
                    "view_mode": opened.info.item.get("ViewMode"),
                    "shop_url": opened.info.item.get("ShopURL") or None,
                    "address_list": opened.book.body.get("AddressList"),
                },
                writer=_credit(content.author),
                publisher=content.label or self.PUBLISHER,
            )
        )

    def image(self, page: Page, episode: Episode) -> Image.Image:
        """Fetch one page and put its tiles back where they belong.

        Args:
            page: The page to fetch.
            episode: The episode the page belongs to.

        Returns:
            The page in reading order, padding gone.
        """
        return yondemill.fetch_page(self, page, referer=episode.url)

    def _work(self, url: str) -> Work:
        """Read a work page, once per URL."""
        key = urljoin(url, urlparse(url).path.rstrip("/") + "/")
        if key not in self._works:
            res = self._session.get(key, headers=self.HEADERS, timeout=self.TIMEOUT)
            if res.status_code == HTTPStatus.NOT_FOUND:
                msg = f"{url} is gone (HTTP 404): not a work page."
                raise NotAnEpisodePageError(msg)
            res.raise_for_status()
            self._works[key] = parse_work(res.content, str(res.url or key))
        return self._works[key]

    @staticmethod
    def _titles(content: Content, work: Work | None, content_id: str) -> tuple[str, str]:
        """The series and episode titles: the work page's when it lists the episode, else the content's."""
        if work is not None and work.title:
            listed = work.episodes.get(content_id, "")
            if listed:
                return work.title, listed
            return split_title(content.title, work.title)
        return split_title(content.title)


def _credit(author: str) -> str:
    """YONDEMILL's `<name> <role>` author line (`雁須磨子 著`) as `名前 (役割)`."""
    return _AUTHOR_LINE.sub(r"\1 (\2)", author.strip())


#: A name, then the role after the last run of whitespace.
_AUTHOR_LINE = re.compile(r"^(.*\S)\s+(\S+)$")


def _neighbour_urls(work: Work | None, content_id: str) -> tuple[str | None, str | None]:
    """The work's episodes either side of `content_id`, None at either end (or both when unlisted)."""
    if work is None:
        return None, None
    before, after = neighbours(list(work.episodes), content_id)
    return (
        f"https://{CONTENT_HOST}/contents/{before}" if before else None,
        f"https://{CONTENT_HOST}/contents/{after}" if after else None,
    )


def _work_link(content: Content) -> str | None:
    """The link back to a work page on the publisher's site the content page carries, or None."""
    for href in content.links:
        parsed = urlparse(href)
        if parsed.hostname == WORK_HOST and _WORK_PATH.match(parsed.path):
            return href
    return None
