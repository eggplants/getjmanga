"""Gaugau Monster Plus (がうがうモンスター, Futabasha), whose episodes open in Voyager's SpeedBinb.

What is specific to the site is the page around the viewer: the
`#content[data-ptbinb]` element that names the API endpoint and the content
id, the episode listing and the titles. The viewer itself -- the key, the
tables, the page list off `content.js` (`ServerType` 1) and the tiled
`M_H.jpg` per page -- is `viewers/speedbinb.py`'s.
"""

from __future__ import annotations

import re
from http import HTTPStatus
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from bs4.element import Tag

from getjmanga.errors import NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractor import Episode, Extractor, neighbours, published_on
from getjmanga.viewers import speedbinb

if TYPE_CHECKING:
    from httpx import Client
    from PIL import Image

    from getjmanga.extractor import Page

BASE_URL = "https://gaugau.futabanet.jp"

#: Element the site mounts the viewer on. `data-ptbinb` names the API endpoint.
_VIEWER_ID = "content"

_WORK_PATH = re.compile(r"^/list/work/(?P<id>[^/]+)(?:/(?P<kind>episodes|comics))?/?$")
_EPISODE_PATH = re.compile(r"^/list/work/(?P<id>[^/]+)/episodes/(?P<order>\d+)/?$")
_READER_PATH = re.compile(r"^/list/work/(?P<id>[^/]+)/reader/comics/(?P<cid>[^/]+)/?$")


class Gaugau(Extractor):
    """Fetch episodes from Gaugau Monster Plus (gaugau.futabanet.jp)."""

    NAME = "gaugau"
    HOSTS = ("gaugau.futabanet.jp",)
    PUBLISHER = "双葉社"
    URL_FORMS = (
        "https://gaugau.futabanet.jp/list/work/<work-id>/episodes/<n>",
        "https://gaugau.futabanet.jp/list/work/<work-id>/reader/comics/<content-id>",
        "https://gaugau.futabanet.jp/list/work/<work-id>",
        "https://gaugau.futabanet.jp/list/work/<work-id>/episodes",
        "https://gaugau.futabanet.jp/list/work/<work-id>/comics",
    )

    def __init__(self, session: Client | None = None) -> None:
        """Build an extractor.

        Args:
            session: A session to reuse. A retrying one is made when omitted.
        """
        super().__init__(session)
        self._listings: dict[tuple[str, str], list[str]] = {}

    @classmethod
    def suitable(cls, url: str) -> bool:
        """Report whether `url` is an episode, a volume trial or a work page.

        Args:
            url: The URL to check.

        Returns:
            True for the URL shapes in `URL_FORMS`.
        """
        if not super().suitable(url):
            return False
        path = urlparse(url).path
        return bool(_EPISODE_PATH.match(path) or _READER_PATH.match(path) or _WORK_PATH.match(path))

    @classmethod
    def is_series(cls, url: str) -> bool:
        """Report whether `url` is a work page or one of its listings.

        Args:
            url: The URL to check.

        Returns:
            True for `/list/work/<id>`, `/list/work/<id>/episodes` and `/list/work/<id>/comics`.
        """
        return cls.suitable(url) and _WORK_PATH.match(urlparse(url).path) is not None

    def series_urls(self, url: str) -> list[str]:
        """List a work's episodes, oldest first, or its volume trials with `/comics`.

        Args:
            url: A work URL, with or without `/episodes` or `/comics`.

        Returns:
            One URL per listed episode (or volume trial), in reading order.

        Raises:
            UnsupportedUrlError: The URL is not a work page.
            NotAnEpisodePageError: The listing is empty.
        """
        match = _WORK_PATH.match(urlparse(url).path)
        if match is None:
            msg = f"{url} is not a work page."
            raise UnsupportedUrlError(msg)
        kind = match["kind"] or "episodes"
        urls = self.listing(match["id"], kind)
        if not urls:
            msg = f"the work at {url} lists no {kind}."
            raise NotAnEpisodePageError(msg)
        return urls

    def episode(self, url: str) -> Episode:
        """Read one episode: its titles, its pages and the episode after it.

        Args:
            url: An episode or volume trial URL.

        Returns:
            The episode. `pages` is empty when the free run of the episode is
            over and the site sends readers to its app instead.

        Raises:
            UnsupportedUrlError: The URL is not an episode URL.
            NotAnEpisodePageError: The page is a 404 or has no episode on it.
        """
        parsed = urlparse(url)
        match = _EPISODE_PATH.match(parsed.path) or _READER_PATH.match(parsed.path)
        if match is None:
            msg = f"{url} is not an episode page."
            raise UnsupportedUrlError(msg)
        work_id = match["id"]
        kind = "episodes" if "order" in match.groupdict() else "comics"

        res = self._session.get(url, headers=self.HEADERS, timeout=self.TIMEOUT)
        if res.status_code == HTTPStatus.NOT_FOUND:
            msg = f"{url} is not there (404)."
            raise NotAnEpisodePageError(msg)
        res.raise_for_status()
        soup = BeautifulSoup(res.content, "html.parser")

        series_title = _series_title(soup, work_id)
        episode_title = _episode_title(soup)
        writer = _credits(soup)
        dated = soup.select_one("div.detailHead__body")
        published = published_on(dated.get_text(" ", strip=True)) if isinstance(dated, Tag) else None
        viewer = soup.select_one(f"#{_VIEWER_ID}[data-ptbinb][data-ptbinb-cid]")
        prev_url, next_url = self._neighbours(url, work_id, kind)

        if not isinstance(viewer, Tag):
            if not episode_title:
                msg = f"no viewer and no episode heading on {url}."
                raise NotAnEpisodePageError(msg)
            return Episode(
                url=url,
                series_title=series_title,
                episode_title=episode_title,
                prev_url=prev_url,
                next_url=next_url,
                metadata={"work_id": work_id, "locked": True},
                writer=writer,
                publisher=self.PUBLISHER,
                published=published,
            )

        content_id = str(viewer.attrs["data-ptbinb-cid"])
        info_url = urljoin(url, str(viewer.attrs["data-ptbinb"]))
        content = speedbinb.content_info(
            self,
            info_url,
            content_id,
            referer=url,
            server_types=frozenset({speedbinb.SERVER_TYPE_DIRECT}),
        )
        if content is None:
            msg = f"{info_url} did not describe {content_id}."
            raise NotAnEpisodePageError(msg)
        book = speedbinb.page_list(self, content, referer=url)

        return Episode(
            url=url,
            series_title=series_title,
            episode_title=episode_title or content_id,
            pages=book.pages,
            prev_url=prev_url,
            next_url=next_url,
            metadata={
                "work_id": work_id,
                "content_id": content_id,
                "contents_server": content.server,
                "view_mode": content.item.get("ViewMode"),
                "title": content.item.get("Title"),
            },
            writer=writer,
            publisher=self.PUBLISHER,
            published=published,
        )

    def image(self, page: Page, episode: Episode) -> Image.Image:
        """Fetch one page and put its tiles back where they belong.

        Args:
            page: The page to fetch.
            episode: The episode the page belongs to.

        Returns:
            The page in reading order, padding gone.
        """
        return speedbinb.fetch_page(self, page, referer=episode.url)

    def listing(self, work_id: str, kind: str = "episodes") -> list[str]:
        """List a work's episodes or volume trials, in reading order.

        The site lists episodes newest first and skips numbers, so the list is
        what says which episode follows which. It is fetched once per work.

        Args:
            work_id: The work's id.
            kind: `"episodes"` for the episode list, `"comics"` for the volume list.

        Returns:
            The episode (or volume trial) URLs, oldest first.
        """
        cached = self._listings.get((work_id, kind))
        if cached is not None:
            return cached

        page_url = f"{BASE_URL}/list/work/{work_id}/{kind}"
        soup = BeautifulSoup(self._get(page_url).content, "html.parser")
        pattern = _EPISODE_PATH if kind == "episodes" else _READER_PATH
        urls: list[str] = []
        for anchor in soup.find_all("a", href=True):
            href = urljoin(page_url, str(anchor["href"]).split("?", 1)[0].split("#", 1)[0])
            parsed = urlparse(href)
            match = pattern.match(parsed.path)
            if match and match["id"] == work_id and parsed.hostname in self.HOSTS and href not in urls:
                urls.append(href)
        if kind == "episodes":
            urls.reverse()
        self._listings[work_id, kind] = urls
        return urls

    def _neighbours(self, url: str, work_id: str, kind: str) -> tuple[str | None, str | None]:
        """The listing entries either side of `url`, None at either end (or both when unlisted)."""
        urls = self.listing(work_id, kind)
        key = url.rstrip("/")
        listed = next((candidate for candidate in urls if candidate.rstrip("/") == key), None)
        return neighbours(urls, listed) if listed is not None else (None, None)


def _series_title(soup: BeautifulSoup, work_id: str) -> str:
    """The work's title, off the breadcrumb, else off the page title, else the id."""
    for anchor in soup.select('ol.breadcrumb a[itemprop="item"]'):
        if _WORK_PATH.match(urlparse(str(anchor.get("href", ""))).path):
            name = anchor.get_text(strip=True)
            if name:
                return name
    og = soup.find("meta", property="og:title")
    heading = str(og.attrs.get("content", "")) if isinstance(og, Tag) else ""
    if not heading and soup.title:
        heading = soup.title.get_text()
    # "公式-<series> <episode> | 作品詳細 | <site>"
    heading = heading.split(" | ", 1)[0].strip().removeprefix("公式-").strip()
    return heading or work_id


def _credits(soup: BeautifulSoup) -> str:
    """The `役割：<a>名前</a>` runs under the episode heading, as `名前 (役割)` each."""
    body = soup.select_one("div.detailHead__body span")
    if not isinstance(body, Tag):
        return ""
    credited = []
    for anchor in body.find_all("a"):
        label = anchor.previous_sibling
        role = str(label).strip().rstrip("：:") if isinstance(label, str) else ""
        name = anchor.get_text(strip=True)
        if name:
            credited.append(f"{name} ({role})" if role else name)
    return ", ".join(credited)


def _episode_title(soup: BeautifulSoup) -> str:
    """The episode's heading, `<h1 class="detailHead__title">`, or "" without one."""
    heading = soup.select_one("h1.detailHead__title")
    return heading.get_text(strip=True).strip("　 ") if heading else ""
