"""週刊コロコロコミック (小学館), a Next.js app with a protobuf API and AES-CBC page files.

The site left GigaViewer for a viewer of its own. Every page of the app
goes through `/api/csr?rq=<endpoint>&<params>`, a Next.js route that
proxies to the site's backend and answers in protobuf; the message shapes
below come from the protobufjs classes in the site's bundle. A chapter's
pages are `.webp.enc` files on a signed CDN URL, AES-CBC encrypted with a
key and iv the viewer answer carries -- the same scheme COMIC FUZ uses, so
its `decrypt()` is reused.
"""

from __future__ import annotations

import re
from http import HTTPStatus
from io import BytesIO
from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import urlparse

from PIL import Image

from getjmanga.protobuf import integer, message, messages, raw, string

from .common import Episode, Extractor, NotAnEpisodePageError, Page, UnsupportedUrlError
from .fuz import decrypt

if TYPE_CHECKING:
    from requests import Response

BASE_URL = "https://www.corocoro.jp"
#: The app's own API route: `?rq=<endpoint>` plus the endpoint's parameters.
API_URL = f"{BASE_URL}/api/csr"

# `/chapter/<id>/viewer`, the viewer page of one chapter.
_EPISODE_PATH = re.compile(r"^/chapter/(?P<id>\d+)/viewer/?$")
# `/title/<id>`, the work page with the chapter list.
_SERIES_PATH = re.compile(r"^/title/(?P<id>\d+)/?$")

# `Proto.ViewerView`, the answer to `chapter/viewer`.
_VIEWER_PAGES = 2
_VIEWER_CURRENT = 5
_VIEWER_PREV = 6
_VIEWER_NEXT = 7
_VIEWER_SCROLL = 9
_VIEWER_CHAPTERS = 10
_VIEWER_TITLE = 15
_VIEWER_AUTHORS = 17
_VIEWER_AES_KEY = 19
_VIEWER_AES_IV = 20
_VIEWER_RESULT = 21
#: `ViewerView.Result`: SUCCESS, AUTHENTICATION_ERROR (a login would do), NOT_PAYMENT_ERROR.
RESULT_SUCCESS = 0

# `Proto.TitleDetailView`, the answer to `title/detail`.
_DETAIL_TITLE = 2
_DETAIL_CHAPTERS = 8

# `Proto.Image`, a page or a thumbnail.
_IMAGE_SRC = 1
_IMAGE_HEIGHT = 2
_IMAGE_WIDTH = 3

# `Proto.Chapter`.
_CHAPTER_ID = 1
_CHAPTER_MAIN_NAME = 2
_CHAPTER_SUB_NAME = 3
_CHAPTER_POINT_CONSUMPTION = 5
_CHAPTER_BADGE = 11
#: `Chapter.Badge`: NONE, UPDATE, FREE, ADVANCE (a ticket or points), PREMIUM (paid only).
BADGES = ("none", "update", "free", "advance", "premium")

# `Proto.PointConsumption`.
_POINT_TYPE = 1
_POINT_AMOUNT = 2

# `Proto.Title`.
_TITLE_ID = 1
_TITLE_NAME = 3
_TITLE_CHAPTER_REVERSED = 14

# `Proto.Author`.
_AUTHOR_NAME = 2
_AUTHOR_ROLE = 4


def endpoint(path: str) -> str:
    """The API route of one backend endpoint.

    Args:
        path: The endpoint, `chapter/viewer` or `title/detail`.

    Returns:
        The URL its parameters are appended to.
    """
    return f"{API_URL}?rq={path}"


def episode_url(chapter_id: int) -> str:
    """The canonical URL of a chapter.

    Args:
        chapter_id: The chapter id.

    Returns:
        The viewer URL.
    """
    return f"{BASE_URL}/chapter/{chapter_id}/viewer"


def chapter(buf: bytes) -> dict[str, Any]:
    """Read a `Proto.Chapter` message.

    Args:
        buf: The encoded message.

    Returns:
        Its id, names, badge and what it costs (None when it is free).
    """
    fields = message(buf)
    cost = None
    if raw(fields, _CHAPTER_POINT_CONSUMPTION):
        point = message(raw(fields, _CHAPTER_POINT_CONSUMPTION))
        cost = {"type": integer(point, _POINT_TYPE), "amount": integer(point, _POINT_AMOUNT)}
    badge = integer(fields, _CHAPTER_BADGE)
    return {
        "id": integer(fields, _CHAPTER_ID),
        "main_name": string(fields, _CHAPTER_MAIN_NAME),
        "sub_name": string(fields, _CHAPTER_SUB_NAME),
        "badge": BADGES[badge] if 0 <= badge < len(BADGES) else str(badge),
        "point_consumption": cost,
    }


def chapter_title(entry: dict[str, Any]) -> str:
    """Name a chapter the way the site's list does.

    Args:
        entry: A chapter as `chapter()` read it.

    Returns:
        `mainName`, followed by `subName` when the site set one.
    """
    main, sub = str(entry.get("main_name") or ""), str(entry.get("sub_name") or "")
    return f"{main} {sub}" if sub else main


def author(buf: bytes) -> dict[str, str]:
    """Read a `Proto.Author` message.

    Args:
        buf: The encoded message.

    Returns:
        The author's name and role.
    """
    fields = message(buf)
    return {"name": string(fields, _AUTHOR_NAME), "role": string(fields, _AUTHOR_ROLE)}


def title(buf: bytes) -> dict[str, Any]:
    """Read a `Proto.Title` message.

    Args:
        buf: The encoded message.

    Returns:
        Its id, name and whether its chapter list runs oldest first.
    """
    fields = message(buf)
    return {
        "id": integer(fields, _TITLE_ID),
        "name": string(fields, _TITLE_NAME),
        "chapter_reversed": bool(integer(fields, _TITLE_CHAPTER_REVERSED)),
    }


class Corocoro(Extractor):
    """Fetch chapters from 週刊コロコロコミック."""

    NAME = "corocoro"
    HOSTS = ("www.corocoro.jp",)
    URL_FORMS = (
        "https://www.corocoro.jp/chapter/<chapter-id>/viewer",
        "https://www.corocoro.jp/title/<title-id>",
    )
    HEADERS: ClassVar[dict[str, str]] = {**Extractor.HEADERS, "Accept": "*/*"}

    @classmethod
    def suitable(cls, url: str) -> bool:
        """Report whether the extractor takes `url`.

        Args:
            url: The URL to check.

        Returns:
            True for a chapter viewer or a title page on www.corocoro.jp.
        """
        if not super().suitable(url):
            return False
        path = urlparse(url).path
        return _EPISODE_PATH.match(path) is not None or _SERIES_PATH.match(path) is not None

    @classmethod
    def is_series(cls, url: str) -> bool:
        """Report whether `url` names a whole title rather than one chapter.

        Args:
            url: The URL to check.

        Returns:
            True for `/title/<id>`.
        """
        return _SERIES_PATH.match(urlparse(url).path) is not None

    def series_urls(self, url: str) -> list[str]:
        """List every chapter of a title, first chapter first.

        `title/detail` lists the chapters the way the title page shows them;
        `Title.chapterReversed` says whether that is oldest first.

        Args:
            url: A title URL.

        Returns:
            The viewer URL of every listed chapter, locked ones included.

        Raises:
            UnsupportedUrlError: The URL is not a title page.
            NotAnEpisodePageError: The site knows no such title, or it lists no chapter.
        """
        match = _SERIES_PATH.match(urlparse(url).path)
        if match is None:
            msg = f"{url} is not a title page."
            raise UnsupportedUrlError(msg)
        res = self._session.get(
            endpoint("title/detail"),
            params={"title_id": match["id"]},
            headers={**self.HEADERS, "Referer": url},
            timeout=self.TIMEOUT,
        )
        if res.status_code == HTTPStatus.NOT_FOUND or _is_html(res):
            msg = f"no title at {url}."
            raise NotAnEpisodePageError(msg)
        res.raise_for_status()
        detail = message(res.content)
        listed = [chapter(buf) for buf in messages(detail, _DETAIL_CHAPTERS)]
        if not title(raw(detail, _DETAIL_TITLE)).get("chapter_reversed"):
            listed.reverse()
        urls: list[str] = []
        for entry in listed:
            candidate = episode_url(entry["id"])
            if entry["id"] and candidate not in urls:
                urls.append(candidate)
        if not urls:
            msg = f"the title at {url} lists no chapter."
            raise NotAnEpisodePageError(msg)
        return urls

    def episode(self, url: str) -> Episode:
        """Read one chapter and list its pages.

        `chapter/viewer` names the chapter, its neighbours and the title,
        and hands the page files over with the AES key and iv they are
        encrypted under. For a chapter the visitor may not read it says so
        in `result` and lists no page, but still names the next chapter.

        Args:
            url: The chapter URL.

        Returns:
            The chapter. `pages` is empty when it is not readable.

        Raises:
            UnsupportedUrlError: The URL is not a chapter URL.
            NotAnEpisodePageError: The site knows no such chapter.
        """
        match = _EPISODE_PATH.match(urlparse(url).path)
        if match is None:
            msg = f"{url} is not a chapter page."
            raise UnsupportedUrlError(msg)
        chapter_id = int(match["id"])

        # The viewer asks with PUT: `use_ticket` and the points are what the
        # reader agreed to spend on a locked chapter, which is never anything here.
        res = self._session.put(
            endpoint("chapter/viewer"),
            params={"chapter_id": chapter_id, "use_ticket": 0, "event_point": 0, "paid_point": 0},
            headers={**self.HEADERS, "Referer": episode_url(chapter_id)},
            timeout=self.TIMEOUT,
        )
        # An unknown chapter is answered with the front page, as HTML.
        if res.status_code == HTTPStatus.NOT_FOUND or _is_html(res):
            msg = f"no chapter at {url}."
            raise NotAnEpisodePageError(msg)
        res.raise_for_status()
        viewer = message(res.content)
        current = chapter(raw(viewer, _VIEWER_CURRENT))
        if not current["id"]:
            msg = f"no chapter at {url}."
            raise NotAnEpisodePageError(msg)
        series = title(raw(viewer, _VIEWER_TITLE))
        result = integer(viewer, _VIEWER_RESULT)
        key, iv = string(viewer, _VIEWER_AES_KEY), string(viewer, _VIEWER_AES_IV)

        pages: tuple[Page, ...] = ()
        if result == RESULT_SUCCESS:
            pages = tuple(
                Page(
                    url=string(fields, _IMAGE_SRC),
                    width=integer(fields, _IMAGE_WIDTH),
                    height=integer(fields, _IMAGE_HEIGHT),
                    extra={"key": key, "iv": iv},
                )
                for fields in (message(buf) for buf in messages(viewer, _VIEWER_PAGES))
                if string(fields, _IMAGE_SRC)
            )
        following = chapter(raw(viewer, _VIEWER_NEXT))
        return Episode(
            url=episode_url(chapter_id),
            series_title=series["name"] or str(series["id"] or chapter_id),
            episode_title=chapter_title(current),
            pages=pages,
            next_url=episode_url(following["id"]) if following["id"] else None,
            metadata={
                "result": result,
                "scroll": integer(viewer, _VIEWER_SCROLL),
                "title": series,
                "authors": [author(buf) for buf in messages(viewer, _VIEWER_AUTHORS)],
                "chapter": current,
                "prev_chapter": chapter(raw(viewer, _VIEWER_PREV)) if raw(viewer, _VIEWER_PREV) else None,
                "next_chapter": following if following["id"] else None,
                "chapters": [chapter(buf) for buf in messages(viewer, _VIEWER_CHAPTERS)],
            },
        )

    def image(self, page: Page, episode: Episode) -> Image.Image:
        """Fetch one page from the CDN and decrypt it.

        The page URL is signed (`h` and an expiry `e`), so it is fetched as
        given; no cookie or Referer is needed.

        Args:
            page: The page to fetch.
            episode: The episode the page belongs to.

        Returns:
            The page image.
        """
        res = self._get(
            page.url,
            headers={**self.HEADERS, "Accept": "image/webp,image/*,*/*", "Referer": episode.url},
            timeout=self.IMAGE_TIMEOUT,
        )
        data = res.content
        key, iv = str(page.extra.get("key") or ""), str(page.extra.get("iv") or "")
        # The viewer only decrypts when it was handed both; a page without them is served plain.
        if key and iv:
            data = decrypt(data, key, iv)
        return Image.open(BytesIO(data))


def _is_html(res: Response) -> bool:
    """Whether the API route answered with a page instead of a message."""
    return str(res.headers.get("content-type") or "").lower().startswith("text/html")
