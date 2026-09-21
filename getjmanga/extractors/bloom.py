""".Bloom (ドットブルーム, ホーム社): a WordPress work catalogue in front of MangaFactory's SpeedBinb reader.

A work page at `https://bloom.homesha.co.jp/webcomic/<slug>/` lists its free
episodes newest first, each as a link to `/cbs/c789/<content>/` -- the
reader directory MangaFactory's "CBS" (`r-cbs.mangafactory.jp`, the same
pages under the site's own host) keeps for that episode. That directory
serves a stub page that names the BinB content id (`input#binb_cid`), sets
the `BINBASP` session cookie, and sends the browser on to `speed_iv.php`,
Voyager's SpeedBinb reader mounted on `#content[data-ptbinb]`.

From there the dance is the one `gaugau.py` describes, in its `ServerType`
0 form, which no other extractor here speaks: `bibGetCntntInfo` (on the
site, `BINBASP` cookie required, 400 without it) answers the scramble
tables, a contents server and a per-session token `p`; the page list is
`<server>/sbcGetCntnt.php?cid&p&vm`, the same JSONP as `content.js`, and
every page is `<server>/sbcGetImg.php?cid&src&p&q&vm` (`q=0` for the full
quality file, `q=1` the lighter one the reader picks on small screens),
tiled and put back together by `gaugau.descramble()`. The images want the
cookie and a Referer on the site.

A gone episode (its free run over) answers 404 on the reader directory; the
work page lists only what is still readable. Nothing needs an account, so
there is no `login()`.
"""

from __future__ import annotations

import re
from http import HTTPStatus
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from bs4.element import Tag

from getjmanga.errors import NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractor import Episode, Extractor, Page, neighbours, ordinal
from getjmanga.viewers import speedbinb
from getjmanga.viewers.speedbinb import split_title

if TYPE_CHECKING:
    from httpx import Client
    from PIL import Image

HOST = "bloom.homesha.co.jp"

# A work page: `/webcomic/<slug>/`, but not the listing's own feed.
_WORK_PATH = re.compile(r"^/webcomic/(?!feed/?$)(?P<slug>[\w.-]+)/?$")
# An episode: the reader directory `/cbs/<site>/<content>/`, with or without the reader page itself.
_EPISODE_PATH = re.compile(r"^/cbs/(?P<site>c\d+)/(?P<id>c\d+-\d+)/?(?:(?:speed|main)_iv\.php)?$")

# The reader's element: `data-ptbinb` names the API endpoint.
_VIEWER_SELECTOR = "#content[data-ptbinb]"
# The stub page's content id.
_CID_SELECTOR = "input#binb_cid"
# The work page's episode list, and its title.
_LISTING_SELECTOR = "div.web__detail__item a[href]"
_WORK_TITLE_SELECTOR = "h1.web__detail__data__title"
# The reader's book description, where the link back to the work page is.
_DESCRIPTION_SELECTOR = "#cst_description a[href]"

# The brackets the work page puts around a title: 『なんか、花火』.
_TITLE_BRACKETS = re.compile(r"^『(?P<title>.+)』$")


def episode_url(site: str, content: str) -> str:
    """The canonical reader directory URL of an episode."""
    return f"https://{HOST}/cbs/{site}/{content}/"


def parse_listing(html: str | bytes, url: str) -> tuple[str, list[str]]:
    """Read a work page: its title and the episodes it links, oldest first.

    Args:
        html: The page.
        url: The URL it came from, to resolve links against.

    Returns:
        The work's title (brackets gone) and one canonical episode URL per
        listed episode, oldest first, deduplicated.

    Raises:
        NotAnEpisodePageError: The page is not a work page.
    """
    soup = BeautifulSoup(html, "html.parser")
    heading = soup.select_one(_WORK_TITLE_SELECTOR)
    if not isinstance(heading, Tag):
        msg = f"no work page at {url}."
        raise NotAnEpisodePageError(msg)
    title = heading.get_text(strip=True)
    match = _TITLE_BRACKETS.match(title)
    title = match["title"] if match else title

    urls: list[str] = []
    # The list runs newest first.
    for anchor in reversed(soup.select(_LISTING_SELECTOR)):
        href = urljoin(url, str(anchor["href"]))
        parsed = urlparse(href)
        match = _EPISODE_PATH.match(parsed.path)
        if match and parsed.hostname == HOST:
            canonical = episode_url(match["site"], match["id"])
            if canonical not in urls:
                urls.append(canonical)
    return title, urls


class Bloom(Extractor):
    """Fetch episodes from .Bloom (bloom.homesha.co.jp).

    An episode URL is its reader directory; a series URL is a work page,
    which is also where the order of the episodes comes from. A work page
    is fetched once per run.
    """

    NAME = "bloom"
    HOSTS = (HOST,)
    PUBLISHER = "ホーム社"
    URL_FORMS = (
        "https://bloom.homesha.co.jp/cbs/<site>/<content-id>/",
        "https://bloom.homesha.co.jp/webcomic/<slug>/",
    )

    def __init__(self, session: Client | None = None) -> None:
        """Build an extractor.

        Args:
            session: A session to reuse. A retrying one is made when omitted.
        """
        super().__init__(session)
        # Work page URL -> (title, episode URLs oldest first), so a work page is read once.
        self._works: dict[str, tuple[str, list[str]]] = {}

    @classmethod
    def suitable(cls, url: str) -> bool:
        """Report whether `url` is an episode's reader directory or a work page.

        Args:
            url: The URL to check.

        Returns:
            True for `https://bloom.homesha.co.jp/cbs/<site>/<content>/` (also
            with `speed_iv.php` / `main_iv.php`) and `https://bloom.homesha.co.jp/webcomic/<slug>/`.
        """
        if not super().suitable(url):
            return False
        path = urlparse(url).path
        return bool(_EPISODE_PATH.match(path) or _WORK_PATH.match(path))

    @classmethod
    def is_series(cls, url: str) -> bool:
        """Report whether `url` is a work page.

        Args:
            url: The URL to check.

        Returns:
            True for `https://bloom.homesha.co.jp/webcomic/<slug>/`.
        """
        return cls.suitable(url) and _WORK_PATH.match(urlparse(url).path) is not None

    def series_urls(self, url: str) -> list[str]:
        """List the episodes a work page links, oldest first.

        Args:
            url: A work page URL.

        Returns:
            One reader directory URL per listed episode, oldest first.

        Raises:
            UnsupportedUrlError: The URL is not a work page.
            NotAnEpisodePageError: There is no such work, or it lists no episode.
        """
        if _WORK_PATH.match(urlparse(url).path) is None:
            msg = f"{url} is not a work page."
            raise UnsupportedUrlError(msg)
        _, urls = self._work(url)
        if not urls:
            msg = f"the work at {url} lists no episode."
            raise NotAnEpisodePageError(msg)
        return list(urls)

    def episode(self, url: str) -> Episode:
        """Read one episode: its titles, its pages and the episode after it.

        Args:
            url: An episode's reader directory URL.

        Returns:
            The episode. `pages` is empty when the reader's API refuses the
            content; `next_url` comes from the work page the reader links.

        Raises:
            UnsupportedUrlError: The URL is not an episode URL.
            NotAnEpisodePageError: The episode is gone (404), or the pages
                describe no reader.
        """
        match = _EPISODE_PATH.match(urlparse(url).path)
        if match is None:
            msg = f"{url} is not an episode page."
            raise UnsupportedUrlError(msg)
        canonical = episode_url(match["site"], match["id"])

        # The stub: the content id, the title, and the `BINBASP` cookie the API wants.
        res = self._session.get(canonical, headers=self.HEADERS, timeout=self.TIMEOUT)
        if res.status_code == HTTPStatus.NOT_FOUND:
            msg = f"{url} is gone (HTTP 404): its free run is over, or it never was."
            raise NotAnEpisodePageError(msg)
        res.raise_for_status()
        stub = BeautifulSoup(res.content, "html.parser")
        cid_input = stub.select_one(_CID_SELECTOR)
        content_id = str(cid_input.get("value") or "") if isinstance(cid_input, Tag) else ""
        if not content_id:
            msg = f"no reader content on {url}."
            raise NotAnEpisodePageError(msg)
        page_title = stub.title.get_text(strip=True) if stub.title else ""

        # The reader: the API endpoint, and the link back to the work page.
        reader_url = urljoin(canonical, "speed_iv.php")
        reader = BeautifulSoup(
            self._get(reader_url, headers={**self.HEADERS, "Referer": canonical}).content,
            "html.parser",
        )
        viewer = reader.select_one(_VIEWER_SELECTOR)
        if not isinstance(viewer, Tag):
            msg = f"no SpeedBinb reader on {reader_url}."
            raise NotAnEpisodePageError(msg)
        info_url = urljoin(reader_url, str(viewer["data-ptbinb"]))
        work_url = _work_link(reader, reader_url)
        author = reader.select_one("div.cst_author")
        writer = author.get_text(strip=True) if isinstance(author, Tag) else ""

        series_title, episode_title, urls = self._titles_and_listing(work_url, page_title)
        prev_url, next_url = neighbours(urls, canonical)
        metadata: dict[str, Any] = {
            "site": match["site"],
            "episode_id": match["id"],
            "content_id": content_id,
            "work_url": work_url,
        }

        content = speedbinb.content_info(
            self,
            info_url,
            content_id,
            referer=reader_url,
            server_types=frozenset({speedbinb.SERVER_TYPE_SBC}),
        )
        if content is None:
            return Episode(
                url=canonical,
                series_title=series_title,
                episode_title=episode_title,
                prev_url=prev_url,
                next_url=next_url,
                metadata={**metadata, "locked": True},
                writer=writer,
                publisher=self.PUBLISHER,
                number=ordinal(urls, canonical),
            )

        book = speedbinb.page_list(self, content, referer=reader_url)

        return self._dated_by_upload(
            Episode(
                url=canonical,
                series_title=series_title,
                episode_title=str(episode_title or content.item.get("Title") or match["id"]),
                pages=book.pages,
                prev_url=prev_url,
                next_url=next_url,
                metadata={
                    **metadata,
                    "locked": False,
                    "contents_server": content.server,
                    "info": content.info,
                },
                writer=writer,
                publisher=self.PUBLISHER,
                number=ordinal(urls, canonical),
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
        return speedbinb.fetch_page(self, page, referer=urljoin(episode.url, "speed_iv.php"))

    def _work(self, url: str) -> tuple[str, list[str]]:
        """Read a work page, once per URL."""
        parsed = urlparse(url)
        key = f"https://{HOST}{parsed.path.rstrip('/')}/"
        if key not in self._works:
            res = self._session.get(key, headers=self.HEADERS, timeout=self.TIMEOUT)
            if res.status_code == HTTPStatus.NOT_FOUND:
                msg = f"{url} is gone (HTTP 404): not a work page."
                raise NotAnEpisodePageError(msg)
            res.raise_for_status()
            self._works[key] = parse_listing(res.content, key)
        return self._works[key]

    def _titles_and_listing(self, work_url: str | None, page_title: str) -> tuple[str, str, list[str]]:
        """The series and episode titles, and the work's episode list, off the work page when there is one."""
        if work_url is None:
            series, episode = split_title(page_title)
            return series, episode, []
        series, urls = self._work(work_url)
        _, episode = split_title(page_title, series)
        return series, episode, list(urls)


def _work_link(reader: BeautifulSoup, reader_url: str) -> str | None:
    """The work page the reader's description links, or None when it links none (a volume trial)."""
    for anchor in reader.select(_DESCRIPTION_SELECTOR):
        href = urljoin(reader_url, str(anchor["href"]))
        parsed = urlparse(href)
        if parsed.hostname == HOST and _WORK_PATH.match(parsed.path):
            return f"https://{HOST}{parsed.path.rstrip('/')}/"
    return None
