"""Ohta Web Comic (太田出版): work pages on the publisher's site, episodes on YONDEMILL's BinB reader.

The publisher's site has no episode pages of its own. A work page at
`https://webcomic.ohtabooks.com/<slug>/` lists its episodes newest first,
each free one as an `openBook('<id>')` button that opens
`https://www.yondemill.jp/contents/<id>?view=1` in a new window; an expired
one has no button at all (or points at a shop). YONDEMILL (Toko-Ai) is a
shared ebook platform, and `?view=1` on a content page is a stub that sends
the browser on to Voyager's SpeedBinb reader at `binb.bricks.pub`, with a
per-visit token in the URL. From there the dance is the one `gaugau.py`
describes: `bibGetCntntInfo` with a client-made key, `content.js` for the
page list, one tiled `M_H.jpg` per page to put back together. The images are
static files on S3: no cookie, no Referer check.

A content that YONDEMILL has taken down answers 404. A paid book still opens
the reader, on its free trial pages only; `metadata["shop_url"]` then names
the shop. Signing in to YONDEMILL sits behind reCAPTCHA, so there is no
`login()`.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from http import HTTPStatus
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from bs4.element import Tag

from getjmanga.errors import GetjmangaError, NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractor import Episode, Extractor, Page

from .gaugau import decode_table, descramble, parse_content, parse_pages, pick_tables, viewer_key
from .porta import split_title

if TYPE_CHECKING:
    from PIL import Image
    from requests import Session

#: Where the publisher's work pages are.
WORK_HOST = "webcomic.ohtabooks.com"
#: Where the episodes are read; `yondemill.jp` redirects here.
CONTENT_HOST = "www.yondemill.jp"

#: `ServerType` of a `bibGetCntntInfo` item whose content is static files.
_SERVER_TYPE_DIRECT = 1

# A work page: one path segment, but not the archive listing.
_WORK_PATH = re.compile(r"^/(?!list/?$)(?P<slug>[\w.-]+)/?$")
# A YONDEMILL content, with or without the `?view=1&u0=1` the work page adds.
_CONTENT_PATH = re.compile(r"^/contents/(?P<id>\d+)/?$")

# The `openBook('<id>')` handler the work page opens an episode with.
_OPEN_BOOK = re.compile(r"openBook\(\s*['\"](?P<id>\d+)['\"]\s*\)")
# The stub `?view=1` serves: a script that sends the browser to the reader.
_REDIRECT = re.compile(r"location\.href\s*=\s*['\"](?P<url>[^'\"]+)['\"]")
# The flags the content page hands its analytics: `sales = 'ON';read_right = 'no';...`.
_FLAG = re.compile(r"(?P<key>sales|read_right|layout_type|reader_type)\s*=\s*'(?P<value>[^']*)'")

# The element the reader mounts SpeedBinb on. `data-ptbinb` names the API endpoint.
_VIEWER_SELECTOR = "#content[data-ptbinb][data-ptbinb-cid]"


@dataclass(frozen=True)
class Work:
    """What a work page on the publisher's site says."""

    #: The work page URL.
    url: str
    #: `h2.contentTitle`: the work's title.
    title: str
    #: The listed episodes, oldest first, deduplicated: YONDEMILL content id -> episode title.
    episodes: dict[str, str]


@dataclass(frozen=True)
class Content:
    """What a YONDEMILL content page says."""

    #: The content page URL, redirects followed.
    url: str
    #: `h1.card-title`: `"<work>　<episode>"`.
    title: str
    #: The author line, `"<name> 著"`.
    author: str
    #: The label (publisher) link text.
    label: str
    #: The link back to the work page on the publisher's site, when there is one.
    work_url: str | None
    #: `sales`, `read_right`, `layout_type`, `reader_type` as the page sets them.
    flags: dict[str, str]


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


def parse_content_page(html: str | bytes, url: str) -> Content:
    """Read a YONDEMILL content page into a `Content`.

    Args:
        html: The page.
        url: The URL it came from, to resolve links against.

    Returns:
        The titles, the author, the label and the link back to the work page.

    Raises:
        NotAnEpisodePageError: The page describes no content.
    """
    soup = BeautifulSoup(html, "html.parser")
    heading = soup.select_one("h1.card-title")
    if not isinstance(heading, Tag):
        msg = f"no content on {url}."
        raise NotAnEpisodePageError(msg)
    blocks = soup.select("div.card-summary-block")
    author = ""
    label = ""
    if blocks:
        first = blocks[0].find("p")
        author = first.get_text(strip=True) if isinstance(first, Tag) else ""
        label_link = blocks[0].select_one('a[href^="/labels/"]')
        label = label_link.get_text(strip=True) if isinstance(label_link, Tag) else ""
    work_url = None
    for anchor in soup.select("a[href]"):
        href = urljoin(url, str(anchor["href"]))
        parsed = urlparse(href)
        if parsed.hostname == WORK_HOST and _WORK_PATH.match(parsed.path):
            work_url = href
            break
    scripts = "\n".join(script.get_text() for script in soup.find_all("script"))
    flags = {match["key"]: match["value"] for match in _FLAG.finditer(scripts)}
    return Content(
        url=url,
        title=heading.get_text(strip=True),
        author=author,
        label=label,
        work_url=work_url,
        flags=flags,
    )


def content_url(url: str) -> str | None:
    """The canonical `https://www.yondemill.jp/contents/<id>` of a content URL, or None for another shape."""
    parsed = urlparse(url)
    match = _CONTENT_PATH.match(parsed.path)
    if match is None:
        return None
    return f"https://{CONTENT_HOST}/contents/{match['id']}"


class Ohta(Extractor):
    """Fetch episodes from Ohta Web Comic, through YONDEMILL's SpeedBinb reader.

    An episode URL is a YONDEMILL content URL; a series URL is a work page on
    the publisher's site, which is also where the episode titles and the
    order of the episodes come from. A work page is fetched once per run.
    """

    NAME = "ohta"
    HOSTS = (WORK_HOST, CONTENT_HOST, "yondemill.jp")
    URL_FORMS = (
        "https://www.yondemill.jp/contents/<id>",
        "https://webcomic.ohtabooks.com/<slug>/",
    )

    def __init__(self, session: Session | None = None) -> None:
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
        return _CONTENT_PATH.match(parsed.path) is not None

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
        content_id = canonical.rsplit("/", 1)[-1]

        res = self._session.get(canonical, headers=self.HEADERS, timeout=self.TIMEOUT)
        if res.status_code == HTTPStatus.NOT_FOUND:
            msg = f"{url} is gone (HTTP 404): YONDEMILL no longer serves it."
            raise NotAnEpisodePageError(msg)
        res.raise_for_status()
        content = parse_content_page(res.content, str(res.url or canonical))

        work = self._work(content.work_url) if content.work_url else None
        series_title, episode_title = self._titles(content, work, content_id)
        next_url = _next_url(work, content_id)
        metadata: dict[str, Any] = {
            "content_id": content_id,
            "title": content.title,
            "author": content.author,
            "label": content.label,
            "work_url": content.work_url,
            "flags": content.flags,
        }

        stub = self._get(f"{canonical}?view=1", headers={**self.HEADERS, "Referer": canonical})
        redirect = _REDIRECT.search(stub.text)
        if redirect is None:
            # No reader to go to: the content wants a purchase or a login first.
            return Episode(
                url=canonical,
                series_title=series_title,
                episode_title=episode_title,
                next_url=next_url,
                metadata={**metadata, "locked": True},
            )
        reader_url = urljoin(canonical, redirect["url"])

        reader = self._get(reader_url, headers={**self.HEADERS, "Referer": canonical})
        viewer = BeautifulSoup(reader.content, "html.parser").select_one(_VIEWER_SELECTOR)
        if not isinstance(viewer, Tag):
            msg = f"no SpeedBinb viewer on {reader_url}."
            raise NotAnEpisodePageError(msg)
        binb_id = str(viewer.attrs["data-ptbinb-cid"])
        info_url = urljoin(reader_url, str(viewer.attrs["data-ptbinb"]))
        key = viewer_key(binb_id)
        item = self._content_info(info_url, binb_id, key, referer=reader_url)
        server = str(item["ContentsServer"]).rstrip("/")
        ctbl = decode_table(binb_id, key, str(item.get("ctbl", "")))
        ptbl = decode_table(binb_id, key, str(item.get("ptbl", "")))
        if not isinstance(ctbl, list) or not isinstance(ptbl, list):
            msg = f"{info_url} carried no scramble tables for {binb_id}."
            raise GetjmangaError(msg)

        content_res = self._get(
            f"{server}/content.js",
            params={"dmytime": _now_ms()},
            headers={**self.HEADERS, "Referer": reader_url},
        )
        book = parse_content(content_res.text)
        file_name = "M.jpg" if book.get("ImageClass") == "singlequality" else "M_H.jpg"
        pages = []
        for attrs in parse_pages(book["ttx"]):
            page_table, served_table = pick_tables(attrs["src"], ctbl, ptbl)
            pages.append(
                Page(
                    url=f"{server}/{attrs['src']}/{file_name}",
                    width=int(attrs.get("orgwidth") or 0),
                    height=int(attrs.get("orgheight") or 0),
                    extra={"ctbl": page_table, "ptbl": served_table},
                ),
            )

        return Episode(
            url=canonical,
            series_title=series_title,
            episode_title=episode_title,
            pages=tuple(pages),
            next_url=next_url,
            metadata={
                **metadata,
                "binb_id": binb_id,
                "contents_server": server,
                "reader_title": item.get("Title"),
                "view_mode": item.get("ViewMode"),
                "shop_url": item.get("ShopURL") or None,
                "address_list": book.get("AddressList"),
            },
        )

    def image(self, page: Page, episode: Episode) -> Image.Image:
        """Fetch one page and put its tiles back where they belong.

        Args:
            page: The page to fetch.
            episode: The episode the page belongs to.

        Returns:
            The page in reading order, padding gone.
        """
        image = self._fetch_image(page.url, headers={**self.HEADERS, "Referer": episode.url})
        return descramble(image, str(page.extra.get("ctbl", "")), str(page.extra.get("ptbl", "")))

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

    def _content_info(self, info_url: str, binb_id: str, key: str, *, referer: str) -> dict[str, Any]:
        """Call `bibGetCntntInfo` and return its first item."""
        res = self._get(
            info_url,
            params={"cid": binb_id, "k": key, "dmytime": _now_ms()},
            headers={**self.HEADERS, "Referer": referer},
        )
        body = res.json()
        items = body.get("items") if isinstance(body, dict) and body.get("result") == 1 else None
        if not items or not isinstance(items[0], dict) or not items[0].get("ContentsServer"):
            msg = f"{info_url} did not describe {binb_id}: {str(body)[:200]}"
            raise NotAnEpisodePageError(msg)
        item: dict[str, Any] = items[0]
        if int(item.get("ServerType", 0)) != _SERVER_TYPE_DIRECT:
            msg = f"{binb_id} is on a SpeedBinb ServerType {item.get('ServerType')} backend, which is not supported."
            raise GetjmangaError(msg)
        return item


def _next_url(work: Work | None, content_id: str) -> str | None:
    """The work's episode after `content_id`, or None when it is the last (or unlisted)."""
    if work is None:
        return None
    ids = list(work.episodes)
    if content_id not in ids:
        return None
    index = ids.index(content_id) + 1
    return f"https://{CONTENT_HOST}/contents/{ids[index]}" if index < len(ids) else None


def _now_ms() -> int:
    """The cache-busting timestamp the viewer sends as `dmytime`."""
    return int(time.time() * 1000)
