"""めちゃコミック クリエイターズ (Amutus): a Next.js site whose chapter page carries every page URL.

The site is めちゃコミック's user-submission platform: authors post manga (and
novels and illustrations, which this extractor leaves alone) that anyone may
read for free, without an account. A work is `/title/<title-id>`, a chapter
`/title/<title-id>/chapter/<chapter-id>` (the server keys on the chapter id
alone; the title id in the path is cosmetic).

Both pages are server-rendered by Next.js and put what they show into the
`__NEXT_DATA__` blob, so nothing needs the protobuf API the browser bundle
also talks to (`https://api.creators.mechacomic.jp/manga/viewer`). A chapter
page's `props.pageProps.data` holds `chapter` (id, name, `vertical` for a
webtoon), `pages` (one `imgUrl` each, in reading order), `title.index` (id,
name, author) and `nextChapter` when there is one -- which is the chapter
listed just above it on the work page. A work page's `data.chapters` lists
the chapters newest first, so reading it backwards gives the order the
site's own "next" button walks.

Every page is a plain WebP on `api.creators.mechacomic.jp/assets/`, signed
with `?h=<hash>&e=<expiry>` (a 403 without the query) and served without any
Referer, cookie or User-Agent check. Nothing is scrambled.

Nothing is paywalled: an R18 work only asks the browser for an `age_check`
cookie client-side, the server data is complete either way. A chapter that
is not there is the Next.js 404 page with status 404; a chapter listing no
page comes back as not readable. Signing in goes through the main
mechacomic.jp account (`/creators_session/new`) and only matters for posting,
so the extractor does not log in.
"""

from __future__ import annotations

import json
import re
from http import HTTPStatus
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from .common import Episode, Extractor, NotAnEpisodePageError, Page, UnsupportedUrlError

if TYPE_CHECKING:
    from requests import Response

BASE_URL = "https://creators.mechacomic.jp"

_EPISODE_PATH = re.compile(r"^/title/(?P<title_id>\d+)/chapter/(?P<chapter_id>\d+)/?$")
_SERIES_PATH = re.compile(r"^/title/(?P<title_id>\d+)/?$")
_NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__" type="application/json">(?P<json>.*?)</script>', re.DOTALL)

#: What `__NEXT_DATA__.page` names on a chapter page and on a work page.
_EPISODE_ROUTE = "/title/[titleId]/chapter/[chapterId]"
_SERIES_ROUTE = "/title/[titleId]"


def episode_url(title_id: str | int, chapter_id: str | int) -> str:
    """The canonical URL of a chapter.

    Args:
        title_id: The work's id.
        chapter_id: The chapter's id.

    Returns:
        `https://creators.mechacomic.jp/title/<title-id>/chapter/<chapter-id>`.
    """
    return f"{BASE_URL}/title/{title_id}/chapter/{chapter_id}"


def series_url(title_id: str | int) -> str:
    """The canonical URL of a work.

    Args:
        title_id: The work's id.

    Returns:
        `https://creators.mechacomic.jp/title/<title-id>`.
    """
    return f"{BASE_URL}/title/{title_id}"


def next_data(html: str | bytes) -> dict[str, Any]:
    """Read the `__NEXT_DATA__` blob off a page.

    Args:
        html: The page.

    Returns:
        The parsed blob: `page`, `query`, `props.pageProps`, ...

    Raises:
        NotAnEpisodePageError: The page carries no blob.
    """
    text = html.decode(errors="replace") if isinstance(html, bytes) else html
    match = _NEXT_DATA.search(text)
    if match is None:
        msg = "no __NEXT_DATA__ on the page."
        raise NotAnEpisodePageError(msg)
    try:
        data = json.loads(match["json"])
    except json.JSONDecodeError as error:
        msg = "the __NEXT_DATA__ on the page is not JSON."
        raise NotAnEpisodePageError(msg) from error
    if not isinstance(data, dict):
        msg = "the __NEXT_DATA__ on the page is not an object."
        raise NotAnEpisodePageError(msg)
    return data


def page_data(html: str | bytes, route: str) -> dict[str, Any]:
    """The `data` a chapter or work page was rendered from.

    Args:
        html: The page.
        route: The Next.js route the page must be, `/title/[titleId]/chapter/[chapterId]` or `/title/[titleId]`.

    Returns:
        `props.pageProps.data` of the page's `__NEXT_DATA__`.

    Raises:
        NotAnEpisodePageError: The page is another route (the 404 page, say), or carries no data.
    """
    blob = next_data(html)
    if blob.get("page") != route:
        msg = f"the page is {blob.get('page')!r}, not {route!r}."
        raise NotAnEpisodePageError(msg)
    props = blob.get("props", {}).get("pageProps", {})
    data = props.get("data") if isinstance(props, dict) else None
    if not isinstance(data, dict):
        error = props.get("error") if isinstance(props, dict) else None
        msg = f"the page carries no data ({error!r})."
        raise NotAnEpisodePageError(msg)
    return data


class MechaCreators(Extractor):
    """Fetch chapters from めちゃコミック クリエイターズ."""

    NAME = "mechacreators"
    HOSTS = ("creators.mechacomic.jp",)
    URL_FORMS = (
        "https://creators.mechacomic.jp/title/<title-id>/chapter/<chapter-id>",
        "https://creators.mechacomic.jp/title/<title-id>",
    )

    @classmethod
    def suitable(cls, url: str) -> bool:
        """Report whether `url` is a manga chapter or work page of the site.

        Args:
            url: The URL to check.

        Returns:
            True for `/title/<id>/chapter/<id>` and `/title/<id>` on the known host.
        """
        if not super().suitable(url):
            return False
        path = urlparse(url).path
        return bool(_EPISODE_PATH.match(path) or _SERIES_PATH.match(path))

    @classmethod
    def is_series(cls, url: str) -> bool:
        """Report whether `url` is a work page, `/title/<id>`.

        Args:
            url: The URL to check.

        Returns:
            True for a work page.
        """
        return _SERIES_PATH.match(urlparse(url).path) is not None

    def series_urls(self, url: str) -> list[str]:
        """List every chapter of a work, first to latest.

        Args:
            url: A `/title/<id>` URL.

        Returns:
            One chapter URL per listed chapter, in the order the site's "next" button walks them.

        Raises:
            UnsupportedUrlError: The URL is not a work page.
            NotAnEpisodePageError: The work is not there, or lists no chapter.
        """
        match = _SERIES_PATH.match(urlparse(url).path)
        if match is None:
            msg = f"{url} is not a work page."
            raise UnsupportedUrlError(msg)
        data = page_data(self._fetch(url).content, _SERIES_ROUTE)
        title_id = str(data.get("title", {}).get("index", {}).get("id") or match["title_id"])
        chapters = data.get("chapters") or []
        urls = [episode_url(title_id, chapter["id"]) for chapter in reversed(chapters) if chapter.get("id") is not None]
        if not urls:
            msg = f"the work at {url} lists no chapter."
            raise NotAnEpisodePageError(msg)
        return list(dict.fromkeys(urls))

    def episode(self, url: str) -> Episode:
        """Read one chapter: its titles, its pages and the chapter after it.

        Args:
            url: A `/title/<id>/chapter/<id>` URL.

        Returns:
            The chapter, with no pages when it lists none.

        Raises:
            UnsupportedUrlError: The URL is a work page rather than a chapter.
            NotAnEpisodePageError: The chapter is not there (404), or the page is not a chapter page.
        """
        match = _EPISODE_PATH.match(urlparse(url).path)
        if match is None:
            msg = f"{url} is not a chapter page (a work page is listed with series_urls())."
            raise UnsupportedUrlError(msg)
        data = page_data(self._fetch(url).content, _EPISODE_ROUTE)

        chapter = data.get("chapter") or {}
        index = data.get("title", {}).get("index", {})
        title_id = str(index.get("id") or match["title_id"])
        following = data.get("nextChapter") or {}
        pages = tuple(
            Page(url=str(page["imgUrl"]), width=int(page.get("width") or 0), height=int(page.get("height") or 0))
            for page in data.get("pages") or []
            if isinstance(page, dict) and page.get("imgUrl")
        )
        return Episode(
            url=url,
            series_title=str(index.get("name") or ""),
            episode_title=str(chapter.get("name") or ""),
            pages=pages,
            next_url=episode_url(title_id, following["id"]) if following.get("id") is not None else None,
            metadata={
                "chapter": chapter,
                "nextChapter": data.get("nextChapter"),
                "title": data.get("title"),
                "user": data.get("user"),
                "titleId": title_id,
            },
        )

    def _fetch(self, url: str) -> Response:
        """GET a page of the site; a 404 means there is no such work or chapter."""
        res = self._session.get(url, headers=self.HEADERS, timeout=self.TIMEOUT)
        if res.status_code == HTTPStatus.NOT_FOUND:
            msg = f"{url} is not there (404)."
            raise NotAnEpisodePageError(msg)
        res.raise_for_status()
        return res
