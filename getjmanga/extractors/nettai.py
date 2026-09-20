"""COMIC熱帯 (光文社), a Crefar CMS site reading through ACCESS's PUBLUS Reader for Browser.

The storefront is server-rendered HTML. A work page, `/book/<id>`, lists its
episodes twenty per page (`?sort_type=priority_asc&page=<n>` walks them first
episode first); an episode that is still open links straight to the viewer,
`/publus/viewer.html?cid=<token>`, and one whose run has ended
(「公開は終了しました」) links nowhere at all. The `cid` is an opaque Laravel
cipher text naming the episode and the visitor; the site mints one per
visitor, and takes the ones minted for other visitors just the same. The viewer page
itself answers 401 without a same-site `Referer`, but the extractor never
needs it: the viewer's own API does the work.

- `/api/viewer/c?cid=` is the license call. It names the episode (`cti`) and
  its content directory on the CDN (`url`), with `{"status": 400}` for a
  `cid` the site does not know.
- `/api/viewer/lpi?cid=` names the end-of-episode page,
  `/colophon?book_content_id=<id>`, which holds the "next episode" viewer
  link and the way back to the work page. The work page's `h1` is the only
  place the series title is written down.

The content directory is a PUBLUS one, exactly as `viewers/publus.py`
describes it: `configuration_pack.json` under the viewer's home-grown cipher,
hashed page file names and xorshift-shuffled tiles. Everything after the
license call is that port's; the CDN wants neither cookie nor `Referer`. There is no
account to sign in to -- bookmarks and history go to `book.crefar.com` under
an anonymous token -- so `login()` is the default.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

from bs4 import BeautifulSoup
from bs4.element import Tag

from getjmanga.errors import NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractor import Episode, Extractor, Page
from getjmanga.viewers.publus import decode_pack, descramble, pages

if TYPE_CHECKING:
    from collections.abc import Mapping

    from PIL import Image

BASE_URL = "https://www.comicnettai.com"
VIEWER_PATH = "/publus/viewer.html"
#: The viewer's license call and its last-page call; the `cid` goes in as is.
LICENSE_URL = f"{BASE_URL}/api/viewer/c"
LAST_PAGE_URL = f"{BASE_URL}/api/viewer/lpi"
#: What the license call says when it takes the `cid`.
_LICENSE_OK = "200"

_BOOK_PATH = re.compile(r"^/book/(?P<id>\d+)/?$")
# The `book_contents/<id>/` directory of an episode's thumbnail on the work page.
_THUMB_ID = re.compile(r"/book_contents/(?P<id>\d+)/")


@dataclass(frozen=True)
class Colophon:
    """What the end-of-episode page says about where to go next."""

    #: The viewer URL of the next open episode, None at the end of the run.
    next_url: str | None
    #: The work page the episode belongs to.
    book_url: str


@dataclass(frozen=True)
class Listing:
    """One page of a work page's episode list."""

    title: str
    #: The viewer URL of every open episode, in page order.
    urls: tuple[str, ...]
    #: Episode title by `book_content_id`, open and closed episodes alike.
    titles: Mapping[str, str]
    #: Whether the pager offers a page after this one.
    has_next: bool


def viewer_url(cid: str) -> str:
    """The canonical viewer URL of an episode."""
    return f"{BASE_URL}{VIEWER_PATH}?{urlencode({'cid': cid})}"


def book_url(book_id: str) -> str:
    """The canonical URL of a work page."""
    return f"{BASE_URL}/book/{book_id}"


def viewer_cid(url: str) -> str | None:
    """The `cid` of a viewer URL, or None for any other page."""
    parsed = urlparse(url)
    if parsed.path != VIEWER_PATH:
        return None
    cids = parse_qs(parsed.query).get("cid")
    return cids[0] if cids and cids[0] else None


def parse_colophon(html: str | bytes) -> Colophon | None:
    """Read the next episode and the work page off a colophon page.

    Args:
        html: The page.

    Returns:
        None when the page names no work, which is what an unknown id gets.
    """
    soup = BeautifulSoup(html, "html.parser")
    back = soup.select_one("a.btn-colophon-back")
    if not isinstance(back, Tag):
        return None
    href = back.attrs.get("data-href") or back.attrs.get("href")
    if not href:
        return None
    next_link = soup.select_one("a.btn-colophon-nextepisode[href]")
    return Colophon(
        next_url=urljoin(BASE_URL, str(next_link.attrs["href"])) if isinstance(next_link, Tag) else None,
        book_url=urljoin(BASE_URL, str(href)),
    )


def parse_listing(html: str | bytes) -> Listing | None:
    """Read the title and the open episodes off one page of a work page.

    Args:
        html: The page.

    Returns:
        None when the page names no work, which is what an unknown id gets.
    """
    soup = BeautifulSoup(html, "html.parser")
    heading = soup.select_one("h1.detail--title")
    if not isinstance(heading, Tag):
        return None
    urls: list[str] = []
    titles: dict[str, str] = {}
    for item in soup.select(".detail--product__item"):
        href = item.attrs.get("href")
        if href and viewer_cid(urljoin(BASE_URL, str(href))) is not None:
            urls.append(urljoin(BASE_URL, str(href)))
        title = item.select_one(".detail--product__item__title")
        thumb = item.select_one("img[data-src]")
        found = _THUMB_ID.search(str(thumb.attrs["data-src"])) if isinstance(thumb, Tag) else None
        if found and isinstance(title, Tag):
            titles[found["id"]] = " ".join(title.get_text().split())
    # The pager's "next" is always written out; on the last page its `li` is hidden.
    pager_next = soup.select_one("li.pagenation__item:not(.is-hidde) > a.pagenation__item__link--next")
    has_next = isinstance(pager_next, Tag)
    return Listing(
        title=" ".join(heading.get_text().split()),
        urls=tuple(urls),
        titles=titles,
        has_next=has_next,
    )


class Nettai(Extractor):
    """Fetch episodes from COMIC熱帯."""

    NAME = "nettai"
    HOSTS = ("www.comicnettai.com",)
    URL_FORMS = (
        "https://www.comicnettai.com/publus/viewer.html?cid=<cid>",
        "https://www.comicnettai.com/book/<id>",
    )
    #: How many listing pages a work is allowed to have before the walk gives up.
    MAX_LISTING_PAGES: ClassVar[int] = 200

    @classmethod
    def suitable(cls, url: str) -> bool:
        """Report whether the extractor takes `url`.

        Args:
            url: The URL to check.

        Returns:
            True for a viewer URL with a `cid` or a work page on the known host.
        """
        if not super().suitable(url):
            return False
        return viewer_cid(url) is not None or _BOOK_PATH.match(urlparse(url).path) is not None

    @classmethod
    def is_series(cls, url: str) -> bool:
        """Report whether `url` is a work page rather than an episode.

        Args:
            url: The URL to check.

        Returns:
            True for `/book/<id>`.
        """
        return _BOOK_PATH.match(urlparse(url).path) is not None

    def series_urls(self, url: str) -> list[str]:
        """List every open episode of a work, first episode first.

        Episodes whose run has ended have no viewer link, so they are not
        listed; the work page walks twenty episodes a page.

        Args:
            url: A work URL.

        Returns:
            One viewer URL per open episode.

        Raises:
            UnsupportedUrlError: The URL is not a work page.
            NotAnEpisodePageError: The work lists no open episode.
        """
        match = _BOOK_PATH.match(urlparse(url).path)
        if match is None:
            msg = f"{url} is not a work page."
            raise UnsupportedUrlError(msg)
        urls: list[str] = []
        for page in range(1, self.MAX_LISTING_PAGES + 1):
            listing = self._listing(match["id"], page)
            if listing is None:
                break
            fresh = False
            for episode_url in listing.urls:
                if episode_url not in urls:
                    urls.append(episode_url)
                    fresh = True
            if not fresh or not listing.has_next:
                break
        if not urls:
            msg = f"the work at {url} lists no open episode."
            raise NotAnEpisodePageError(msg)
        return urls

    def episode(self, url: str) -> Episode:
        """Read one episode and list its pages.

        Args:
            url: A viewer URL.

        Returns:
            The episode. `pages` is empty when the license call refuses the
            `cid` of an episode the site still knows.

        Raises:
            UnsupportedUrlError: The URL is not a viewer URL.
            NotAnEpisodePageError: The site knows no such episode.
        """
        cid = viewer_cid(url)
        if cid is None:
            msg = f"{url} is not an episode page."
            raise UnsupportedUrlError(msg)
        canonical = viewer_url(cid)
        headers = {**self.HEADERS, "Referer": canonical}

        license_ = self._json(LICENSE_URL, cid, headers)
        last_page = self._json(LAST_PAGE_URL, cid, headers)
        colophon_url = last_page.get("iframe")
        colophon = parse_colophon(self._get(str(colophon_url), headers=headers).content) if colophon_url else None
        licensed = str(license_.get("status")) == _LICENSE_OK and bool(license_.get("url"))
        if colophon is None and not licensed:
            msg = f"no episode behind {canonical}."
            raise NotAnEpisodePageError(msg)

        content_id = _content_id(str(colophon_url or ""))
        series_title = episode_title = str(license_.get("cti") or "")
        book_id = _book_id(colophon.book_url) if colophon is not None else ""
        listing = self._listing(book_id, 1) if book_id else None
        if listing is not None:
            series_title = listing.title
            episode_title = episode_title or listing.titles.get(content_id, "")
        # The colophon points forward only; the work page lists the episode before.
        prev_url = self._listed_neighbours(book_url(book_id), canonical)[0] if book_id else None
        metadata: dict[str, Any] = {
            "book_content_id": content_id,
            "license": license_,
            "last_page": last_page,
        }
        if not licensed:
            return Episode(
                url=canonical,
                series_title=series_title or content_id,
                episode_title=episode_title or content_id,
                prev_url=prev_url,
                next_url=colophon.next_url if colophon is not None else None,
                metadata=metadata,
            )

        content_url = str(license_["url"])
        pack = decode_pack(self._get(urljoin(content_url, "configuration_pack.json")).text)
        return Episode(
            url=canonical,
            series_title=series_title or episode_title or content_id,
            episode_title=episode_title or content_id,
            pages=tuple(pages(pack, content_url)),
            prev_url=prev_url,
            next_url=colophon.next_url if colophon is not None else None,
            metadata={**metadata, "configuration": pack.content["configuration"]},
        )

    def image(self, page: Page, episode: Episode) -> Image.Image:
        """Fetch one page, unshuffle its tiles and trim the padding.

        Args:
            page: The page to fetch.
            episode: The episode the page belongs to.

        Returns:
            The page image, cut down to the size the pack declares.
        """
        image = self._fetch_image(page.url, headers={**self.HEADERS, "Referer": episode.url})
        if "pattern" in page.extra:
            seeds = page.extra["seeds"]
            block = page.extra["block"]
            image = descramble(image, int(page.extra["pattern"]), (seeds[0], seeds[1], seeds[2]), (block[0], block[1]))
        if (
            0 < page.width <= image.width
            and 0 < page.height <= image.height
            and (page.width, page.height) != image.size
        ):
            image = image.crop((0, 0, page.width, page.height))
        return image

    def _json(self, url: str, cid: str, headers: Mapping[str, str]) -> dict[str, Any]:
        """GET one of the viewer's API calls for `cid`; a non-object answer counts as nothing."""
        body = self._get(f"{url}?{urlencode({'cid': cid})}", headers=headers).json()
        return body if isinstance(body, dict) else {}

    def _listing(self, book_id: str, page: int) -> Listing | None:
        """One page of a work's episode list, first episode first."""
        res = self._get(f"{book_url(book_id)}?{urlencode({'sort_type': 'priority_asc', 'page': page})}")
        return parse_listing(res.content)


def _content_id(colophon_url: str) -> str:
    """The `book_content_id` of a colophon URL, or an empty string."""
    ids = parse_qs(urlparse(colophon_url).query).get("book_content_id")
    return ids[0] if ids else ""


def _book_id(url: str) -> str:
    """The id of a work URL, or an empty string."""
    match = _BOOK_PATH.match(urlparse(url).path)
    return match["id"] if match else ""
