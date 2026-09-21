"""Link-U's platform: マンガワン, フラコミlike!, ガンガンONLINE, マンガPark and マンガラボ!, five sites on one backend.

The five share the backend's vocabulary -- a title has chapters, both go by
numeric ids, every image URL carries a hash and an expiry -- and the two
Shogakukan sites serve their pages as `.webp.enc` files under the same
AES-CBC scheme COMIC FUZ uses, with one key and iv per chapter. What differs
is the front-end: マンガワン's viewer talks protobuf to `/api/client`,
フラコミlike! renders the viewer's props into the page as React flight data,
and ガンガンONLINE bakes the whole chapter into `__NEXT_DATA__` at build time.
The two 白泉社 sites are older: マンガPark is server-rendered HTML whose
Minobi viewer fetches a JSON chapter and XORs each `.jpg.enc` page with its
own key, and マンガラボ! is a Nuxt app over a protobuf API serving plain
JPEGs. A locked chapter is an empty page list on マンガワン, a redirect to
the title page on フラコミlike! and ガンガンONLINE, and a 401 on マンガPark;
マンガラボ! locks nothing.
"""

from __future__ import annotations

import base64
import json
import re
import struct
from dataclasses import dataclass
from http import HTTPStatus
from io import BytesIO
from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from PIL import Image

from getjmanga.cipher import aes_cbc_decrypt, xor_unmask
from getjmanga.errors import LoginError, NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractor import Episode, Extractor, Page, neighbours, ordinal, published_on
from getjmanga.protobuf import integer, message, messages, raw, string

if TYPE_CHECKING:
    from collections.abc import Iterator

    from httpx2 import Client, Response

MANGAONE_URL = "https://manga-one.com"
FLOWERCOMICS_URL = "https://flowercomics.jp"
GANGANONLINE_URL = "https://www.ganganonline.com"
MANGAPARK_URL = "https://manga-park.com"
MANGALAB_URL = "https://manga-lab.net"

#: A chapter page whose JSON was not built yet is served as a fallback shell
#: on ガンガンONLINE; its data then comes from `/_next/data/<build>/...json`.
_NEXT_DATA_PATTERN = re.compile(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.DOTALL)
#: One `self.__next_f.push([1, "..."])` chunk of a React flight stream.
_FLIGHT_PATTERN = re.compile(r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)', re.DOTALL)

#: `Chapter.status` on a ガンガンONLINE title page: a chapter still to come.
_GANGAN_STATUS_UPCOMING = 3
#: `Chapter.chapterType` on フラコミlike!: a chapter with no price on it.
_FLOWER_TYPE_FREE = 0
#: How many chapters `viewer/chapter_list` hands over per page on マンガワン.
_MANGAONE_PAGE_SIZE = 100
#: `consume_type` of a マンガPark chapter the site opens without asking for coins.
_PARK_OPEN_TYPES = frozenset({"free", "resume"})


@dataclass(frozen=True)
class Chapter:
    """One row of a title's chapter list, as any of the five sites lists it."""

    id: int
    title: str
    #: Free to read without points, a ticket or the app.
    free: bool = True
    #: The day it came out, as the site writes it, when the list says.
    released: str = ""


# ---------------------------------------------------------------------------
# Next.js payloads
# ---------------------------------------------------------------------------


def next_data(html: str) -> dict[str, Any]:
    """The `__NEXT_DATA__` JSON of a pages-router Next.js page.

    Args:
        html: The page.

    Returns:
        The parsed JSON, or `{}` when the page carries none.
    """
    match = _NEXT_DATA_PATTERN.search(html)
    if match is None:
        return {}
    try:
        data = json.loads(match[1])
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def flight_text(html: str) -> str:
    """The React flight stream an app-router Next.js page pushes, joined.

    Each chunk is a JavaScript string literal, whose escapes are the JSON ones.

    Args:
        html: The page.

    Returns:
        The stream as one string.
    """
    return "".join(json.loads(f'"{chunk}"') for chunk in _FLIGHT_PATTERN.findall(html))


def flight_object(text: str, key: str) -> dict[str, Any] | None:
    """The first JSON object a flight stream keys as `"<key>":{...}`.

    Args:
        text: The stream from `flight_text()`.
        key: The property name.

    Returns:
        The object, or None when the stream has no such property.
    """
    found = re.search(rf'"{re.escape(key)}"\s*:\s*{{', text)
    if found is None:
        return None
    try:
        value, _ = json.JSONDecoder().raw_decode(text, found.end() - 1)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


# ---------------------------------------------------------------------------
# The shared machinery
# ---------------------------------------------------------------------------


class LinkU(Extractor):
    """What the five sites share: the chapter vocabulary and the page encryption."""

    def __init__(self, session: Client | None = None) -> None:
        """Build an extractor.

        Args:
            session: A session to reuse. A retrying one is made when omitted.
        """
        super().__init__(session)
        #: Title id to its name and chapter list, for the sites whose viewer
        #: answer names neither, so a bulk run reads each title once.
        self._titles: dict[int, tuple[str, list[Chapter]]] = {}
        #: Title id to its credits, for the sites whose title page names them.
        self._credits: dict[int, str] = {}

    @classmethod
    def chapter_url(cls, title_id: int, chapter_id: int) -> str:
        """The viewer URL of a chapter.

        Args:
            title_id: The title's id.
            chapter_id: The chapter's id.

        Returns:
            The URL `episode()` reads the chapter from.
        """
        raise NotImplementedError

    def image(self, page: Page, episode: Episode) -> Image.Image:
        """Fetch one page and decrypt it when the site served it as `.enc`.

        Args:
            page: The page to fetch.
            episode: The chapter the page belongs to.

        Returns:
            The page image.
        """
        res = self._get(page.url, headers={**self.HEADERS, "Referer": episode.url}, timeout=self.IMAGE_TIMEOUT)
        key, iv = str(page.extra.get("key") or ""), str(page.extra.get("iv") or "")
        data = aes_cbc_decrypt(res.content, key, iv) if key and iv else res.content
        return Image.open(BytesIO(data))

    @classmethod
    def _neighbour_urls(cls, title_id: int, chapters: list[Chapter], chapter_id: int) -> tuple[str | None, str | None]:
        """The chapters either side of `chapter_id` in an oldest-first list, None at either end."""
        chapter = next((chapter for chapter in chapters if chapter.id == chapter_id), None)
        if chapter is None:
            return None, None
        before, after = neighbours(chapters, chapter)
        return (
            cls.chapter_url(title_id, before.id) if before else None,
            cls.chapter_url(title_id, after.id) if after else None,
        )

    @staticmethod
    def _chapter_number(chapters: list[Chapter], chapter_id: int) -> int | None:
        """Where `chapter_id` stands in an oldest-first list, counted from 1; None when unlisted."""
        return ordinal([chapter.id for chapter in chapters], chapter_id)

    def _locked(self, url: str, title_id: int, series_title: str, chapters: list[Chapter], chapter_id: int) -> Episode:
        """Describe a chapter the site would not open, from the title's chapter list."""
        title = next((chapter.title for chapter in chapters if chapter.id == chapter_id), "") or str(chapter_id)
        return Episode(
            url=url,
            series_title=series_title or str(title_id),
            episode_title=title,
            prev_url=self._neighbour_urls(title_id, chapters, chapter_id)[0],
            next_url=self._neighbour_urls(title_id, chapters, chapter_id)[1],
            metadata={
                "title_id": title_id,
                "chapter_id": chapter_id,
                "chapters": [{"id": c.id, "title": c.title, "free": c.free} for c in chapters],
            },
            writer=self._credits.get(title_id, ""),
            publisher=self.PUBLISHER,
            published=published_on(next((c.released for c in chapters if c.id == chapter_id), "")),
            number=self._chapter_number(chapters, chapter_id),
        )

    @staticmethod
    def _series_urls(urls: list[str], url: str) -> list[str]:
        """Deduplicate a listing, refusing an empty one."""
        if not urls:
            msg = f"the title at {url} lists no chapter."
            raise NotAnEpisodePageError(msg)
        return list(dict.fromkeys(urls))


# ---------------------------------------------------------------------------
# マンガワン
# ---------------------------------------------------------------------------

_MANGAONE_CHAPTER_PATH = re.compile(r"^/manga/(?P<title>\d+)/chapter/(?P<chapter>\d+|first)/?$")
_MANGAONE_TITLE_PATH = re.compile(r"^/manga/(?P<title>\d+)/?$")


class MangaOne(LinkU):
    """Fetch chapters from マンガワン (小学館).

    The site has no title page of its own: a title's entry point is
    `/manga/<title-id>/chapter/first`, which opens its first chapter. The
    viewer POSTs to `/api/client?rq=viewer_v2`, a proxy onto the backend at
    `app.manga-one.com/web_api/v4`, and gets a protobuf `WebViewerResponse`
    back: the pages with the chapter's AES key and iv, the title, the current
    and the next chapter. A chapter that wants points comes back the same way
    with no pages in it.
    """

    NAME = "mangaone"
    HOSTS = ("manga-one.com",)
    PUBLISHER = "小学館"
    URL_FORMS = (
        "https://manga-one.com/manga/<title-id>/chapter/<chapter-id>",
        "https://manga-one.com/manga/<title-id>/chapter/first",
        "https://manga-one.com/manga/<title-id>",
    )
    HEADERS: ClassVar[dict[str, str]] = {**Extractor.HEADERS, "Referer": f"{MANGAONE_URL}/"}

    @classmethod
    def suitable(cls, url: str) -> bool:
        """Report whether `url` is a chapter viewer or a title id.

        Args:
            url: The URL to check.

        Returns:
            True for `/manga/<id>/chapter/<id|first>` and `/manga/<id>` on manga-one.com.
        """
        if not super().suitable(url):
            return False
        path = urlparse(url).path
        return bool(_MANGAONE_CHAPTER_PATH.match(path) or _MANGAONE_TITLE_PATH.match(path))

    @classmethod
    def is_series(cls, url: str) -> bool:
        """Report whether `url` names a whole title.

        Args:
            url: The URL to check.

        Returns:
            True for `/manga/<id>`.
        """
        return cls.suitable(url) and _MANGAONE_TITLE_PATH.match(urlparse(url).path) is not None

    @classmethod
    def chapter_url(cls, title_id: int, chapter_id: int) -> str:
        """The viewer URL of a chapter.

        Args:
            title_id: The title's id.
            chapter_id: The chapter's id.

        Returns:
            `/manga/<title-id>/chapter/<chapter-id>`.
        """
        return f"{MANGAONE_URL}/manga/{title_id}/chapter/{chapter_id}"

    def series_urls(self, url: str) -> list[str]:
        """List every chapter of a title, oldest first.

        Args:
            url: A `/manga/<id>` URL.

        Returns:
            The viewer URL of every chapter, locked ones included.

        Raises:
            UnsupportedUrlError: The URL is no title.
            NotAnEpisodePageError: The title lists no chapter.
        """
        match = _MANGAONE_TITLE_PATH.match(urlparse(url).path)
        if match is None:
            msg = f"{url} is not a title url."
            raise UnsupportedUrlError(msg)
        title_id = int(match["title"])
        return self._series_urls([self.chapter_url(title_id, c.id) for c in self._chapter_list(title_id)], url)

    def episode(self, url: str) -> Episode:
        """Read one chapter: its pages, with the key the chapter is encrypted under.

        Args:
            url: A `/manga/<id>/chapter/<id|first>` URL.

        Returns:
            The chapter. `pages` is empty when it wants points or a login.

        Raises:
            UnsupportedUrlError: The URL is no chapter viewer.
            NotAnEpisodePageError: The site knows no such chapter.
        """
        match = _MANGAONE_CHAPTER_PATH.match(urlparse(url).path)
        if match is None:
            msg = f"{url} is not a chapter viewer url."
            raise UnsupportedUrlError(msg)
        params: dict[str, str | int] = {"title_id": int(match["title"])}
        if match["chapter"] != "first":
            params["chapter_id"] = int(match["chapter"])
        res = self._session.post(
            f"{MANGAONE_URL}/api/client",
            params={"rq": "viewer_v2", **params},
            headers=self.HEADERS,
            timeout=self.TIMEOUT,
        )
        if res.status_code == HTTPStatus.NOT_FOUND:
            msg = f"no chapter at {url}."
            raise NotAnEpisodePageError(msg)
        res.raise_for_status()
        viewer = message(res.content)

        title = message(raw(message(raw(viewer, 5)), 1))
        current, following = message(raw(viewer, 7)), message(raw(viewer, 8))
        title_id, chapter_id = integer(title, 1) or int(match["title"]), integer(current, 1)
        key, iv = string(viewer, 3), string(viewer, 4)
        chapters = [_mangaone_chapter(c) for c in messages(message(raw(viewer, 11)), 1)]
        return Episode(
            url=url,
            series_title=string(title, 2) or str(title_id),
            episode_title=string(current, 2) or str(chapter_id),
            pages=tuple(self._pages(viewer, key, iv)),
            # The viewer names the next chapter and lists only the newest few; the title lists them all.
            prev_url=self._listed_neighbours(
                f"{MANGAONE_URL}/manga/{title_id}", self.chapter_url(title_id, chapter_id)
            )[0],
            next_url=self.chapter_url(title_id, integer(following, 1)) if integer(following, 1) else None,
            metadata={
                "title_id": title_id,
                "chapter_id": chapter_id,
                "description": string(current, 3),
                "author": string(title, 5),
                "free": not raw(current, 16),
                "chapters": [{"id": c.id, "title": c.title, "free": c.free} for c in chapters],
            },
            writer=string(title, 5),
            publisher=self.PUBLISHER,
            published=published_on(string(current, 5)),
            number=self._listed_number(f"{MANGAONE_URL}/manga/{title_id}", self.chapter_url(title_id, chapter_id)),
        )

    def _chapter_list(self, title_id: int) -> list[Chapter]:
        """Walk `viewer/chapter_list`, oldest first, page by page."""
        chapters: list[Chapter] = []
        page = 1
        while True:
            # `rq` sits in the URL so that the call can be told from the viewer's (also `/api/client`).
            res = self._get(
                f"{MANGAONE_URL}/api/client?rq=viewer/chapter_list",
                params={
                    "title_id": title_id,
                    "type": "chapter",
                    "sort_type": "asc",
                    "page": page,
                    "limit": _MANGAONE_PAGE_SIZE,
                },
            )
            listing = message(raw(message(res.content), 1))
            chapters.extend(_mangaone_chapter(c) for c in messages(listing, 1))
            paging = message(raw(listing, 2))
            if not chapters or integer(paging, 1) >= integer(paging, 2):
                return chapters
            page += 1

    @staticmethod
    def _pages(viewer: dict[int, list[int | bytes]], key: str, iv: str) -> Iterator[Page]:
        """The manga pages of a `WebViewerResponse`; promos and purchase cards are skipped."""
        for entry in messages(viewer, 1):
            image = message(raw(message(entry), 1))
            if not image:
                continue
            yield Page(
                url=string(image, 1),
                width=integer(image, 2),
                height=integer(image, 3),
                extra={"key": key, "iv": iv},
            )


def _mangaone_chapter(encoded: bytes) -> Chapter:
    """A `proto.v4.Chapter`: its id, name, and whether `requiredPoints` is set."""
    fields = message(encoded)
    return Chapter(integer(fields, 1), string(fields, 2) or str(integer(fields, 1)), free=not raw(fields, 16))


# ---------------------------------------------------------------------------
# フラコミlike!
# ---------------------------------------------------------------------------

_FLOWER_CHAPTER_PATH = re.compile(r"^/chapter/(?P<chapter>\d+)/?$")
_FLOWER_TITLE_PATH = re.compile(r"^/title/(?P<title>\d+)/?$")


class FlowerComics(LinkU):
    """Fetch chapters from フラコミlike! (小学館).

    A chapter page carries the viewer's props in its React flight stream: a
    `viewerSection` with every page's URL and AES key and iv, the title, and
    the next chapter. A chapter that wants points or a ticket is a 307 to the
    title page instead, whose `chapters` object names it.
    """

    NAME = "flowercomics"
    HOSTS = ("flowercomics.jp",)
    PUBLISHER = "小学館"
    URL_FORMS = (
        "https://flowercomics.jp/chapter/<chapter-id>",
        "https://flowercomics.jp/title/<title-id>",
    )

    @classmethod
    def suitable(cls, url: str) -> bool:
        """Report whether `url` is a chapter or a title page.

        Args:
            url: The URL to check.

        Returns:
            True for `/chapter/<id>` and `/title/<id>` on flowercomics.jp.
        """
        if not super().suitable(url):
            return False
        path = urlparse(url).path
        return bool(_FLOWER_CHAPTER_PATH.match(path) or _FLOWER_TITLE_PATH.match(path))

    @classmethod
    def is_series(cls, url: str) -> bool:
        """Report whether `url` is a title page.

        Args:
            url: The URL to check.

        Returns:
            True for `/title/<id>`.
        """
        return cls.suitable(url) and _FLOWER_TITLE_PATH.match(urlparse(url).path) is not None

    @classmethod
    def chapter_url(cls, title_id: int, chapter_id: int) -> str:  # noqa: ARG003 (a chapter URL names no title)
        """The viewer URL of a chapter.

        Args:
            title_id: Ignored; a chapter stands on its own here.
            chapter_id: The chapter's id.

        Returns:
            `/chapter/<chapter-id>`.
        """
        return f"{FLOWERCOMICS_URL}/chapter/{chapter_id}"

    def series_urls(self, url: str) -> list[str]:
        """List every chapter of a title, oldest first.

        Args:
            url: A `/title/<id>` URL.

        Returns:
            The URL of every chapter, locked ones included.

        Raises:
            UnsupportedUrlError: The URL is no title page.
            NotAnEpisodePageError: The title lists no chapter.
        """
        match = _FLOWER_TITLE_PATH.match(urlparse(url).path)
        if match is None:
            msg = f"{url} is not a title page."
            raise UnsupportedUrlError(msg)
        _, chapters = _flower_title(self._get(url).text)
        return self._series_urls([self.chapter_url(0, c.id) for c in chapters], url)

    def episode(self, url: str) -> Episode:
        """Read one chapter off its page.

        Args:
            url: A `/chapter/<id>` URL.

        Returns:
            The chapter. `pages` is empty when the site sent the title page instead.

        Raises:
            UnsupportedUrlError: The URL is no chapter page.
            NotAnEpisodePageError: The site knows no such chapter.
        """
        match = _FLOWER_CHAPTER_PATH.match(urlparse(url).path)
        if match is None:
            msg = f"{url} is not a chapter url."
            raise UnsupportedUrlError(msg)
        chapter_id = int(match["chapter"])
        res = self._session.get(url, headers=self.HEADERS, timeout=self.TIMEOUT)
        if not res.is_success:
            msg = f"no chapter at {url} (HTTP {res.status_code})."
            raise NotAnEpisodePageError(msg)

        landed = _FLOWER_TITLE_PATH.match(urlparse(str(res.url)).path)
        if landed is not None:
            series_title, chapters = _flower_title(res.text)
            self._credits[int(landed["title"])] = _flower_credits(res.text)
            return self._locked(url, int(landed["title"]), series_title, chapters, chapter_id)

        viewer = flight_object(flight_text(res.text), "viewerSection")
        if viewer is None:
            msg = f"no viewer on {url}."
            raise NotAnEpisodePageError(msg)
        title_id = int(viewer.get("titleID") or 0)
        following = viewer.get("nextChapter")
        next_id = int(following.get("id") or 0) if isinstance(following, dict) else 0
        # The viewer names the next chapter only; the page's chapter list has the one before, and the dates.
        _, chapters = _flower_title(res.text)
        released = next((c.released for c in chapters if c.id == chapter_id), "")
        return Episode(
            url=url,
            series_title=str(viewer.get("titleName") or title_id),
            episode_title=str(viewer.get("currentChapterName") or chapter_id),
            pages=tuple(self._pages(viewer)),
            prev_url=self._neighbour_urls(title_id, chapters, chapter_id)[0],
            next_url=self.chapter_url(title_id, next_id) if next_id else None,
            metadata={
                "title_id": title_id,
                "chapter_id": chapter_id,
                "orientation": viewer.get("orientation"),
                "right_to_left": viewer.get("directionRightToLeft"),
            },
            writer=self._writer(title_id),
            publisher=self.PUBLISHER,
            published=published_on(released),
            number=self._chapter_number(chapters, chapter_id),
        )

    def _writer(self, title_id: int) -> str:
        """The authors the title page credits, read once per title; a chapter page names none."""
        if title_id not in self._credits:
            self._credits[title_id] = _flower_credits(self._get(f"{FLOWERCOMICS_URL}/title/{title_id}").text)
        return self._credits[title_id]

    @staticmethod
    def _pages(viewer: dict[str, Any]) -> Iterator[Page]:
        """The manga pages of a `viewerSection`; the banner it appends is skipped."""
        for entry in viewer.get("pages") or []:
            if not isinstance(entry, dict) or entry.get("type") != "image" or "anchor" in entry or not entry.get("src"):
                continue
            crypto = entry.get("crypto") if isinstance(entry.get("crypto"), dict) else {}
            yield Page(
                url=str(entry["src"]), extra={"key": str(crypto.get("key") or ""), "iv": str(crypto.get("iv") or "")}
            )


def _flower_credits(html: str) -> str:
    """The authors a title page's `fanLetter` block names, comma-separated."""
    fan_letter = flight_object(flight_text(html), "fanLetter") or {}
    names = [str(a.get("name") or "") for a in fan_letter.get("authors") or [] if isinstance(a, dict)]
    return ", ".join(name for name in names if name)


def _flower_title(html: str) -> tuple[str, list[Chapter]]:
    """The name and the oldest-first chapter list of a title page.

    The page lists the chapters in three runs -- the newest few, the middle
    and the oldest few -- each newest first; `priority` numbers them from 1.
    """
    soup = BeautifulSoup(html, "html.parser")
    heading = soup.find("h1")
    listing = flight_object(flight_text(html), "chapters") or {}
    rows = [
        row
        for key in ("earlyChapters", "omittedMiddleChapters", "latestChapters")
        for row in (listing.get(key) or [])
        if isinstance(row, dict) and row.get("id")
    ]
    rows.sort(key=lambda row: int(row.get("priority") or 0))
    chapters = [
        Chapter(
            int(row["id"]),
            str(row.get("title") or row["id"]),
            free=row.get("chapterType") == _FLOWER_TYPE_FREE,
            released=str(row.get("updated") or ""),
        )
        for row in rows
    ]
    return heading.get_text(strip=True) if heading is not None else "", chapters


# ---------------------------------------------------------------------------
# ガンガンONLINE
# ---------------------------------------------------------------------------

_GANGAN_CHAPTER_PATH = re.compile(r"^/title/(?P<title>\d+)/chapter/(?P<chapter>\d+)/?$")
_GANGAN_TITLE_PATH = re.compile(r"^/title/(?P<title>\d+)/?$")


class GanganOnline(LinkU):
    """Fetch chapters from ガンガンONLINE (スクウェア・エニックス).

    Every page is built statically: a chapter page's `__NEXT_DATA__` holds
    the page URLs -- plain WebP files under `/secure/`, unencrypted -- the
    title and the next chapter. A chapter the web does not serve (app-only,
    or not out yet) is a 307 to the title page; a chapter the build has not
    rendered yet is a fallback shell, whose data comes from `/_next/data/`.
    """

    NAME = "ganganonline"
    HOSTS = ("www.ganganonline.com",)
    PUBLISHER = "スクウェア・エニックス"
    URL_FORMS = (
        "https://www.ganganonline.com/title/<title-id>/chapter/<chapter-id>",
        "https://www.ganganonline.com/title/<title-id>",
    )

    @classmethod
    def suitable(cls, url: str) -> bool:
        """Report whether `url` is a chapter or a title page.

        Args:
            url: The URL to check.

        Returns:
            True for `/title/<id>/chapter/<id>` and `/title/<id>` on www.ganganonline.com.
        """
        if not super().suitable(url):
            return False
        path = urlparse(url).path
        return bool(_GANGAN_CHAPTER_PATH.match(path) or _GANGAN_TITLE_PATH.match(path))

    @classmethod
    def is_series(cls, url: str) -> bool:
        """Report whether `url` is a title page.

        Args:
            url: The URL to check.

        Returns:
            True for `/title/<id>`.
        """
        return cls.suitable(url) and _GANGAN_TITLE_PATH.match(urlparse(url).path) is not None

    @classmethod
    def chapter_url(cls, title_id: int, chapter_id: int) -> str:
        """The viewer URL of a chapter.

        Args:
            title_id: The title's id.
            chapter_id: The chapter's id.

        Returns:
            `/title/<title-id>/chapter/<chapter-id>`.
        """
        return f"{GANGANONLINE_URL}/title/{title_id}/chapter/{chapter_id}"

    def series_urls(self, url: str) -> list[str]:
        """List every chapter the title page shows, oldest first.

        Args:
            url: A `/title/<id>` URL.

        Returns:
            The URL of every listed chapter, app-only ones included.

        Raises:
            UnsupportedUrlError: The URL is no title page.
            NotAnEpisodePageError: The title lists no chapter.
        """
        match = _GANGAN_TITLE_PATH.match(urlparse(url).path)
        if match is None:
            msg = f"{url} is not a title page."
            raise UnsupportedUrlError(msg)
        title_id = int(match["title"])
        props, _ = self._page_props(url)
        _, chapters = _gangan_title(props)
        return self._series_urls([self.chapter_url(title_id, c.id) for c in chapters], url)

    def episode(self, url: str) -> Episode:
        """Read one chapter off its page.

        Args:
            url: A `/title/<id>/chapter/<id>` URL.

        Returns:
            The chapter. `pages` is empty when the site sent the title page instead.

        Raises:
            UnsupportedUrlError: The URL is no chapter page.
            NotAnEpisodePageError: The page describes no chapter.
        """
        match = _GANGAN_CHAPTER_PATH.match(urlparse(url).path)
        if match is None:
            msg = f"{url} is not a chapter url."
            raise UnsupportedUrlError(msg)
        title_id, chapter_id = int(match["title"]), int(match["chapter"])
        props, redirected = self._page_props(url)
        if redirected:
            series_title, chapters = _gangan_title(props)
            return self._locked(url, title_id, series_title, chapters, chapter_id)

        data = props.get("data")
        if not isinstance(data, dict) or "pages" not in data:
            msg = f"no viewer on {url}."
            raise NotAnEpisodePageError(msg)
        last_page = data.get("lastPage") if isinstance(data.get("lastPage"), dict) else {}
        next_id = int(last_page.get("nextChapterId") or 0)
        # The page names the next chapter only; the title page lists the one before.
        prev_url = self._listed_neighbours(
            f"{GANGANONLINE_URL}/title/{title_id}", self.chapter_url(title_id, chapter_id)
        )[0]
        return self._dated_by_upload(
            Episode(
                url=url,
                series_title=str(data.get("titleName") or title_id),
                episode_title=str(data.get("chapterName") or chapter_id),
                pages=tuple(self._pages(data)),
                prev_url=prev_url,
                next_url=self.chapter_url(title_id, next_id) if next_id else None,
                metadata={
                    "title_id": title_id,
                    "chapter_id": chapter_id,
                    "author": data.get("author"),
                    "left_start": data.get("ifLeftStart"),
                },
                writer=str(data.get("author") or ""),
                publisher=self.PUBLISHER,
                number=self._listed_number(
                    f"{GANGANONLINE_URL}/title/{title_id}", self.chapter_url(title_id, chapter_id)
                ),
            )
        )

    def _page_props(self, url: str) -> tuple[dict[str, Any], bool]:
        """The `pageProps` of a page, and whether the site sent the title page instead.

        A page the build has not rendered yet is a fallback shell; its props
        then come from the build's `/_next/data/` JSON, where a redirect is
        spelled `__N_REDIRECT`.
        """
        res = self._session.get(url, headers=self.HEADERS, timeout=self.TIMEOUT)
        if not res.is_success:
            msg = f"no page at {url} (HTTP {res.status_code})."
            raise NotAnEpisodePageError(msg)
        requested, landed = urlparse(url).path.rstrip("/"), urlparse(str(res.url)).path.rstrip("/")
        data = next_data(res.text)
        props = data.get("props", {}).get("pageProps")
        props = props if isinstance(props, dict) else {}
        if not data.get("isFallback"):
            return props, landed != requested
        json_res = self._get(f"{GANGANONLINE_URL}/_next/data/{data.get('buildId')}{requested}.json")
        payload = _json_or_none(json_res)
        props = payload.get("pageProps") if isinstance(payload, dict) else None
        props = props if isinstance(props, dict) else {}
        if "__N_REDIRECT" not in props:
            return props, False
        title_res = self._get(GANGANONLINE_URL + str(props["__N_REDIRECT"]))
        title_props = next_data(title_res.text).get("props", {}).get("pageProps")
        return title_props if isinstance(title_props, dict) else {}, True

    @staticmethod
    def _pages(data: dict[str, Any]) -> Iterator[Page]:
        """The manga pages of a chapter; the promo `linkImage` is skipped."""
        for entry in data.get("pages") or []:
            image = entry.get("image") if isinstance(entry, dict) else None
            if not isinstance(image, dict) or not image.get("imageUrl"):
                continue
            yield Page(url=GANGANONLINE_URL + str(image["imageUrl"]))


def _gangan_title(props: dict[str, Any]) -> tuple[str, list[Chapter]]:
    """The name and the oldest-first chapter list of a title page's `pageProps`."""
    data = props.get("data")
    default = data.get("default") if isinstance(data, dict) else None
    default = default if isinstance(default, dict) else {}
    chapters: list[Chapter] = []
    # Listed newest first; a chapter still to come carries its date as `mainText` and its name as `subText`.
    for row in reversed(default.get("chapters") or []):
        if not isinstance(row, dict) or not row.get("id"):
            continue
        upcoming = row.get("status") == _GANGAN_STATUS_UPCOMING
        name = str((row.get("subText") if upcoming else row.get("mainText")) or "").strip()
        chapters.append(Chapter(int(row["id"]), name or str(row["id"]), free="status" not in row))
    return str(default.get("titleName") or ""), chapters


# ---------------------------------------------------------------------------
# マンガPark
# ---------------------------------------------------------------------------

_PARK_CHAPTER_PATH = re.compile(r"^/title/(?P<title>\d+)/(?P<chapter>\d+)/?$")
_PARK_TITLE_PATH = re.compile(r"^/title/(?P<title>\d+)/?$")


class MangaPark(LinkU):
    """Fetch chapters from マンガPark (白泉社).

    A title page lists every chapter as `li[data-chapter-id]`, oldest first,
    and the site's own share link `/title/<title-id>/<chapter-id>` is that
    page with the Minobi viewer opened on one of them (the site serves it
    only for a chapter it would open, so the extractor reads the title page
    itself). The viewer GETs `/api/chapter/<id>`: one `images` entry per
    page, each a `.jpg.enc` URL with a base64 key the file is XORed with.
    A chapter that wants a login is a 401 there, one that wants coins comes
    back without pages; either way its names come from the title page.
    Signing in is the `/login` form with the CSRF token `/api/csrf_token`
    hands out.
    """

    NAME = "mangapark"
    HOSTS = ("manga-park.com",)
    PUBLISHER = "白泉社"
    URL_FORMS = (
        "https://manga-park.com/title/<title-id>/<chapter-id>",
        "https://manga-park.com/title/<title-id>",
    )
    CONFIG_KEY = "mangapark"

    @classmethod
    def suitable(cls, url: str) -> bool:
        """Report whether `url` is a chapter or a title page.

        Args:
            url: The URL to check.

        Returns:
            True for `/title/<id>/<id>` and `/title/<id>` on manga-park.com.
        """
        if not super().suitable(url):
            return False
        path = urlparse(url).path
        return bool(_PARK_CHAPTER_PATH.match(path) or _PARK_TITLE_PATH.match(path))

    @classmethod
    def is_series(cls, url: str) -> bool:
        """Report whether `url` is a title page.

        Args:
            url: The URL to check.

        Returns:
            True for `/title/<id>`.
        """
        return cls.suitable(url) and _PARK_TITLE_PATH.match(urlparse(url).path) is not None

    @classmethod
    def chapter_url(cls, title_id: int, chapter_id: int) -> str:
        """The viewer URL of a chapter.

        Args:
            title_id: The title's id.
            chapter_id: The chapter's id.

        Returns:
            `/title/<title-id>/<chapter-id>`.
        """
        return f"{MANGAPARK_URL}/title/{title_id}/{chapter_id}"

    def series_urls(self, url: str) -> list[str]:
        """List every chapter of a title, oldest first.

        Args:
            url: A `/title/<id>` URL.

        Returns:
            The viewer URL of every chapter, locked ones included.

        Raises:
            UnsupportedUrlError: The URL is no title page.
            NotAnEpisodePageError: The title lists no chapter.
        """
        match = _PARK_TITLE_PATH.match(urlparse(url).path)
        if match is None:
            msg = f"{url} is not a title page."
            raise UnsupportedUrlError(msg)
        title_id = int(match["title"])
        _, chapters = self._title(title_id)
        return self._series_urls([self.chapter_url(title_id, c.id) for c in chapters], url)

    def episode(self, url: str) -> Episode:
        """Read one chapter: its names off the title page, its pages off the API.

        Args:
            url: A `/title/<id>/<id>` URL.

        Returns:
            The chapter. `pages` is empty when it wants a login or coins.

        Raises:
            UnsupportedUrlError: The URL is no chapter page.
            NotAnEpisodePageError: The title does not exist or lists no such chapter.
        """
        match = _PARK_CHAPTER_PATH.match(urlparse(url).path)
        if match is None:
            msg = f"{url} is not a chapter url."
            raise UnsupportedUrlError(msg)
        title_id, chapter_id = int(match["title"]), int(match["chapter"])
        series_title, chapters = self._title(title_id)
        if all(c.id != chapter_id for c in chapters):
            msg = f"the title at {url} lists no chapter {chapter_id}."
            raise NotAnEpisodePageError(msg)

        res = self._session.get(
            f"{MANGAPARK_URL}/api/chapter/{chapter_id}",
            headers={**self.HEADERS, "Referer": url},
            timeout=self.TIMEOUT,
        )
        if not res.is_success and res.status_code != HTTPStatus.UNAUTHORIZED:
            msg = f"no chapter at {url} (HTTP {res.status_code})."
            raise NotAnEpisodePageError(msg)
        payload = _json_or_none(res)
        payload = payload if isinstance(payload, dict) else {}
        data: dict[str, Any] = payload["data"] if isinstance(payload.get("data"), dict) else {}
        # A 401 wants a login; any other `consume_type` wants coins the viewer would ask for first.
        readable = res.is_success and data.get("consume_type") in _PARK_OPEN_TYPES
        locked = self._locked(url, title_id, series_title, chapters, chapter_id)
        return Episode(
            url=url,
            series_title=locked.series_title,
            episode_title=locked.episode_title,
            pages=tuple(self._pages(data)) if readable else (),
            prev_url=locked.prev_url,
            next_url=locked.next_url,
            metadata={
                **locked.metadata,
                "consume_type": data.get("consume_type"),
                "page_start": data.get("page_start"),
                "logged_in": bool(payload.get("is_logged_in")),
            },
            writer=locked.writer,
            publisher=self.PUBLISHER,
            published=locked.published,
            number=locked.number,
        )

    def image(self, page: Page, episode: Episode) -> Image.Image:
        """Fetch one page and undo the XOR it was served under.

        Args:
            page: The page to fetch.
            episode: The chapter the page belongs to.

        Returns:
            The page image.
        """
        res = self._get(page.url, headers={**self.HEADERS, "Referer": episode.url}, timeout=self.IMAGE_TIMEOUT)
        key = str(page.extra.get("key") or "")
        data = xor_unmask(res.content, base64.b64decode(key).hex()) if key else res.content
        return Image.open(BytesIO(data))

    def login(self, url: str, username: str, password: str) -> None:
        """Sign in through the `/login` form.

        Args:
            url: Any URL on the site; only there for the interface.
            username: The account's email address.
            password: The account's password.

        Raises:
            LoginError: The site did not log the session in.
        """
        self._session.post(
            f"{MANGAPARK_URL}/login",
            data={"csrf_token": self._status().get("csrf_token", ""), "address": username, "password": password},
            headers=self.HEADERS,
            timeout=self.TIMEOUT,
        ).raise_for_status()
        if not self._status().get("is_logged_in"):
            msg = f"manga-park.com refused the credentials for {username} ({url})."
            raise LoginError(msg)

    def _title(self, title_id: int) -> tuple[str, list[Chapter]]:
        """The name and the oldest-first chapter list of a title page, read once.

        Raises:
            NotAnEpisodePageError: There is no such title.
        """
        if title_id not in self._titles:
            res = self._session.get(f"{MANGAPARK_URL}/title/{title_id}", headers=self.HEADERS, timeout=self.TIMEOUT)
            if not res.is_success:
                msg = f"no title {title_id} on manga-park.com (HTTP {res.status_code})."
                raise NotAnEpisodePageError(msg)
            self._titles[title_id] = _park_title(res.text)
            self._credits[title_id] = _park_credits(res.text)
        return self._titles[title_id]

    def _status(self) -> dict[str, Any]:
        """`/api/csrf_token`: the form token and whether the session is signed in."""
        payload = _json_or_none(self._get(f"{MANGAPARK_URL}/api/csrf_token"))
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _pages(data: dict[str, Any]) -> Iterator[Page]:
        """The manga pages of an `/api/chapter` answer; an HTML page (`src`) is skipped."""
        for entry in data.get("chapter") or []:
            for image in (entry.get("images") or []) if isinstance(entry, dict) else []:
                if isinstance(image, dict) and image.get("path"):
                    yield Page(
                        url=str(image["path"]),
                        width=int(image.get("width") or 0),
                        height=int(image.get("height") or 0),
                        extra={"key": str(image.get("key") or "")},
                    )


def _park_credits(html: str) -> str:
    """The title header's `.author` line, as the page writes it (`原作：X　作画：Y`)."""
    soup = BeautifulSoup(html, "html.parser")
    author = soup.select_one("h1.txtColorSubject + p.author")
    return author.get_text(strip=True) if author is not None else ""


def _park_title(html: str) -> tuple[str, list[Chapter]]:
    """The name and the oldest-first chapter list of a マンガPark title page.

    A chapter's display name is its `.chapterTitle` (the `SubName` of the
    API, `#１①`), falling back to `data-chapter-name` (the `Name`, `1`);
    a free badge on the row marks a chapter free to read.
    """
    soup = BeautifulSoup(html, "html.parser")
    named = soup.find("div", attrs={"data-title-name": True})
    chapters: list[Chapter] = []
    for row in soup.select(".title .chapter li[data-chapter-id]"):
        chapter_id = str(row.get("data-chapter-id") or "")
        if not chapter_id.isdigit():
            continue
        heading = row.select_one(".chapterTitle")
        name = (heading.get_text(strip=True) if heading is not None else "") or str(row.get("data-chapter-name") or "")
        free = row.select_one(".free-badge img") is not None
        dated = row.select_one(".date")
        released = dated.get_text(strip=True) if dated is not None else ""
        chapters.append(Chapter(int(chapter_id), name or chapter_id, free=free, released=released))
    return str(named.get("data-title-name") or "") if named is not None else "", chapters


# ---------------------------------------------------------------------------
# マンガラボ!
# ---------------------------------------------------------------------------

_LAB_CHAPTER_PATH = re.compile(r"^/title/viewer/(?P<chapter>\d+)/?$")
_LAB_TITLE_PATH = re.compile(r"^/title/(?P<title>\d+)/?$")


class MangaLab(LinkU):
    """Fetch chapters from マンガラボ! (白泉社), the reader-submission site behind マンガPark.

    The Nuxt app is empty HTML; `/title/viewer/<chapter-id>` GETs
    `/api/title/chapter/<id>/`, a protobuf `ShowChapterResponse` with the
    title's name and id, the chapter's number and its page URLs -- plain
    JPEGs -- and `/title/<title-id>` GETs `/api/title/<id>/`, a
    `GetTitleResponse` listing the chapters newest first. The site's own
    `nextChapterId` walks that way too, so the next chapter is taken from
    the list instead. Nothing needs an account.
    """

    NAME = "mangalab"
    HOSTS = ("manga-lab.net",)
    PUBLISHER = "白泉社"
    URL_FORMS = (
        "https://manga-lab.net/title/viewer/<chapter-id>",
        "https://manga-lab.net/title/<title-id>",
    )

    @classmethod
    def suitable(cls, url: str) -> bool:
        """Report whether `url` is a viewer or a title page.

        Args:
            url: The URL to check.

        Returns:
            True for `/title/viewer/<id>` and `/title/<id>` on manga-lab.net.
        """
        if not super().suitable(url):
            return False
        path = urlparse(url).path
        return bool(_LAB_CHAPTER_PATH.match(path) or _LAB_TITLE_PATH.match(path))

    @classmethod
    def is_series(cls, url: str) -> bool:
        """Report whether `url` is a title page.

        Args:
            url: The URL to check.

        Returns:
            True for `/title/<id>`.
        """
        return cls.suitable(url) and _LAB_TITLE_PATH.match(urlparse(url).path) is not None

    @classmethod
    def chapter_url(cls, title_id: int, chapter_id: int) -> str:  # noqa: ARG003 (a viewer URL names no title)
        """The viewer URL of a chapter.

        Args:
            title_id: Ignored; a chapter stands on its own here.
            chapter_id: The chapter's id.

        Returns:
            `/title/viewer/<chapter-id>`.
        """
        return f"{MANGALAB_URL}/title/viewer/{chapter_id}"

    def series_urls(self, url: str) -> list[str]:
        """List every chapter of a title, lowest number first.

        Args:
            url: A `/title/<id>` URL.

        Returns:
            The viewer URL of every chapter.

        Raises:
            UnsupportedUrlError: The URL is no title page.
            NotAnEpisodePageError: The title lists no chapter.
        """
        match = _LAB_TITLE_PATH.match(urlparse(url).path)
        if match is None:
            msg = f"{url} is not a title page."
            raise UnsupportedUrlError(msg)
        _, chapters = self._title(int(match["title"]))
        return self._series_urls([self.chapter_url(0, c.id) for c in chapters], url)

    def episode(self, url: str) -> Episode:
        """Read one chapter off the API.

        Args:
            url: A `/title/viewer/<id>` URL.

        Returns:
            The chapter.

        Raises:
            UnsupportedUrlError: The URL is no viewer.
            NotAnEpisodePageError: The site knows no such chapter.
        """
        match = _LAB_CHAPTER_PATH.match(urlparse(url).path)
        if match is None:
            msg = f"{url} is not a chapter url."
            raise UnsupportedUrlError(msg)
        chapter_id = int(match["chapter"])
        res = self._session.get(
            f"{MANGALAB_URL}/api/title/chapter/{chapter_id}/", headers=self.HEADERS, timeout=self.TIMEOUT
        )
        if not res.is_success:
            msg = f"no chapter at {url} (HTTP {res.status_code})."
            raise NotAnEpisodePageError(msg)
        answer = message(res.content)
        chapter, author = message(raw(answer, 2)), message(raw(answer, 4))
        title_id = integer(answer, 7)
        series_title, chapters = self._title(title_id) if title_id else ("", [])
        number = _lab_number(chapter, 3)
        return self._dated_by_upload(
            Episode(
                url=url,
                series_title=string(answer, 1) or series_title or str(title_id),
                episode_title=string(chapter, 2) or _lab_chapter_name(number, chapter_id),
                pages=tuple(Page(url=value.decode()) for value in messages(chapter, 4) if value),
                prev_url=self._neighbour_urls(title_id, chapters, chapter_id)[0],
                next_url=self._neighbour_urls(title_id, chapters, chapter_id)[1],
                metadata={
                    "title_id": title_id,
                    "chapter_id": chapter_id,
                    "number": number,
                    "author": string(author, 2),
                    "begin_with_blank_page": bool(integer(chapter, 5)),
                    "chapters": [{"id": c.id, "title": c.title, "free": c.free} for c in chapters],
                },
                writer=string(author, 2),
                publisher=self.PUBLISHER,
                number=self._chapter_number(chapters, chapter_id),
            )
        )

    def _title(self, title_id: int) -> tuple[str, list[Chapter]]:
        """The name and the lowest-number-first chapter list of a title, read once.

        Raises:
            NotAnEpisodePageError: There is no such title.
        """
        if title_id not in self._titles:
            res = self._session.get(f"{MANGALAB_URL}/api/title/{title_id}/", headers=self.HEADERS, timeout=self.TIMEOUT)
            if not res.is_success:
                msg = f"no title {title_id} on manga-lab.net (HTTP {res.status_code})."
                raise NotAnEpisodePageError(msg)
            self._titles[title_id] = _lab_title(message(res.content))
        return self._titles[title_id]


def _lab_number(fields: dict[int, list[int | bytes]], number: int) -> float:
    """A `double` field, or 0 when absent."""
    value = raw(fields, number)
    return struct.unpack("<d", value)[0] if len(value) == struct.calcsize("<d") else 0.0


def _lab_chapter_name(number: float, chapter_id: int) -> str:
    """What to call a chapter that has no name: its number, else its id."""
    return f"{number:g}" if number else str(chapter_id)


def _lab_title(answer: dict[int, list[int | bytes]]) -> tuple[str, list[Chapter]]:
    """The name and the chapter list of a `GetTitleResponse`, lowest number first.

    The API lists the chapters newest first; the sort is stable, so
    chapters sharing a number keep their oldest-first order.
    """
    title = message(raw(answer, 1))
    rows = [message(row) for row in messages(answer, 2)]
    rows = [row for row in rows if integer(row, 1)]
    rows.reverse()
    rows.sort(key=lambda row: _lab_number(row, 8))
    chapters = [
        Chapter(integer(row, 1), string(row, 2) or _lab_chapter_name(_lab_number(row, 8), integer(row, 1)))
        for row in rows
    ]
    return string(title, 2), chapters


def _json_or_none(res: Response) -> Any:  # noqa: ANN401 (whatever JSON the site sent)
    try:
        return res.json()
    except ValueError:
        return None
