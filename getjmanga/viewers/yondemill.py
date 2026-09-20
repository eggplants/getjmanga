"""YONDEMILL (Toko-Ai), the shared ebook platform a few publishers put their free episodes on.

A content page, `https://www.yondemill.jp/contents/<id>`, describes a book:
its title, its author, its label and, in a link somewhere on the page, the
publisher's own page for it. `?view=1` on that URL is a stub that sends the
browser on to Voyager's SpeedBinb reader at `binb.bricks.pub`, with a
per-visit token in the URL; from there the dance is `viewers/speedbinb.py`'s,
on the static backend (`ServerType` 1). The images are files on S3: no
cookie, no Referer check.

A content that YONDEMILL has taken down answers 404. A paid book still opens
the reader, on its free trial pages only; the reader's `ShopURL` then names
the shop. A book that wants a purchase or a login first serves a stub with
no reader to go to. Signing in sits behind reCAPTCHA, so there is no way in
for an account.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from http import HTTPStatus
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from bs4.element import Tag

from getjmanga.errors import NotAnEpisodePageError
from getjmanga.viewers import speedbinb

if TYPE_CHECKING:
    from PIL import Image

    from getjmanga.extractor import Extractor, Page

#: Where the contents are read; `yondemill.jp` redirects here.
CONTENT_HOST = "www.yondemill.jp"

# A content, with or without the `?view=1&u0=1` a publisher's page adds.
_CONTENT_PATH = re.compile(r"^/contents/(?P<id>\d+)/?$")
# The stub `?view=1` serves: a script that sends the browser to the reader.
_REDIRECT = re.compile(r"location\.href\s*=\s*['\"](?P<url>[^'\"]+)['\"]")
# The flags the content page hands its analytics: `sales = 'ON';read_right = 'no';...`.
_FLAG = re.compile(r"(?P<key>sales|read_right|layout_type|reader_type)\s*=\s*'(?P<value>[^']*)'")
# The element the reader mounts SpeedBinb on. `data-ptbinb` names the API endpoint.
_VIEWER_SELECTOR = "#content[data-ptbinb][data-ptbinb-cid]"


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
    #: Every link on the page, absolute, in order; the publisher's own page is among them.
    links: tuple[str, ...]
    #: `sales`, `read_right`, `layout_type`, `reader_type` as the page sets them.
    flags: dict[str, str]


@dataclass(frozen=True)
class Opened:
    """The reader a content opened, and what it served."""

    #: The reader the stub sent the browser to.
    reader_url: str
    #: The reader's own content id, off `data-ptbinb-cid`.
    binb_id: str
    info: speedbinb.Content
    book: speedbinb.Book


@dataclass(frozen=True)
class Reading:
    """A content read through the reader: what the page said, and the pages when it opened."""

    content: Content
    content_id: str
    #: None when the stub sent the browser nowhere: a locked book.
    opened: Opened | None = None

    @property
    def locked(self) -> bool:
        """Whether the content wants a purchase or a login before it opens the reader."""
        return self.opened is None

    @property
    def pages(self) -> tuple[Page, ...]:
        """The pages in reading order; none for a locked content."""
        return self.opened.book.pages if self.opened else ()


def content_url(url: str) -> str | None:
    """The canonical `https://www.yondemill.jp/contents/<id>` of a content URL, or None for another shape."""
    parsed = urlparse(url)
    match = _CONTENT_PATH.match(parsed.path)
    if match is None:
        return None
    return f"https://{CONTENT_HOST}/contents/{match['id']}"


def parse_content_page(html: str | bytes, url: str) -> Content:
    """Read a content page into a `Content`.

    Args:
        html: The page.
        url: The URL it came from, to resolve links against.

    Returns:
        The titles, the author, the label and the page's links.

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
    links: list[str] = []
    for anchor in soup.select("a[href]"):
        href = urljoin(url, str(anchor["href"]))
        if href not in links:
            links.append(href)
    scripts = "\n".join(script.get_text() for script in soup.find_all("script"))
    flags = {match["key"]: match["value"] for match in _FLAG.finditer(scripts)}
    return Content(
        url=url,
        title=heading.get_text(strip=True),
        author=author,
        label=label,
        links=tuple(links),
        flags=flags,
    )


def read(extractor: Extractor, canonical: str) -> Reading:
    """Read a content: its page, then the reader it opens, then the pages.

    Args:
        extractor: Whose session and headers to use.
        canonical: The content URL, as `content_url()` returns it.

    Returns:
        The reading; locked when the stub sends the browser nowhere.

    Raises:
        NotAnEpisodePageError: YONDEMILL has no such content (404), or the
            reader does not describe it.
    """
    content_id = canonical.rsplit("/", 1)[-1]
    res = extractor.session.get(canonical, headers=extractor.HEADERS, timeout=extractor.TIMEOUT)
    if res.status_code == HTTPStatus.NOT_FOUND:
        msg = f"{canonical} is gone (HTTP 404): YONDEMILL no longer serves it."
        raise NotAnEpisodePageError(msg)
    res.raise_for_status()
    content = parse_content_page(res.content, str(res.url or canonical))

    stub = extractor._get(f"{canonical}?view=1", headers={**extractor.HEADERS, "Referer": canonical})
    redirect = _REDIRECT.search(stub.text)
    if redirect is None:
        # No reader to go to: the content wants a purchase or a login first.
        return Reading(content=content, content_id=content_id)
    reader_url = urljoin(canonical, redirect["url"])

    reader = extractor._get(reader_url, headers={**extractor.HEADERS, "Referer": canonical})
    viewer = BeautifulSoup(reader.content, "html.parser").select_one(_VIEWER_SELECTOR)
    if not isinstance(viewer, Tag):
        msg = f"no SpeedBinb viewer on {reader_url}."
        raise NotAnEpisodePageError(msg)
    binb_id = str(viewer.attrs["data-ptbinb-cid"])
    info_url = urljoin(reader_url, str(viewer.attrs["data-ptbinb"]))
    info = speedbinb.content_info(
        extractor,
        info_url,
        binb_id,
        referer=reader_url,
        server_types=frozenset({speedbinb.SERVER_TYPE_DIRECT}),
    )
    if info is None:
        msg = f"{info_url} did not describe {binb_id}."
        raise NotAnEpisodePageError(msg)
    book = speedbinb.page_list(extractor, info, referer=reader_url)
    return Reading(content, content_id, Opened(reader_url=reader_url, binb_id=binb_id, info=info, book=book))


def fetch_page(extractor: Extractor, page: Page, *, referer: str) -> Image.Image:
    """Fetch one page of a reading and put its tiles back where they belong.

    Args:
        extractor: Whose session and headers to use.
        page: The page to fetch.
        referer: What to send as the Referer.

    Returns:
        The page in reading order, padding gone.
    """
    return speedbinb.fetch_page(extractor, page, referer=referer)
