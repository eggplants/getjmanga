"""サイコミ (Cygames), a Next.js app over a JSON API that serves RC4-encrypted pages."""

from __future__ import annotations

import re
from io import BytesIO
from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import urlparse

from PIL import Image

from getjmanga.errors import LoginError, NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractor import Episode, Extractor, Page

if TYPE_CHECKING:
    from collections.abc import Mapping

BASE_URL = "https://cycomi.com"
#: The backend the browser app talks to (`baseURL` of its axios instance).
API_URL = "https://web.cycomi.com/api"
#: The Next.js app's own route the email sign-in form posts to.
LOGIN_URL = f"{BASE_URL}/api/auth/login/email"

#: `resultCode` of every successful answer.
_RESULT_OK = 1
#: `/api/chapter/paginatedList` sorts: first chapter first.
_SORT_ASC = 1
#: The most chapters `/api/chapter/paginatedList` hands over per page; more is a `RequestError`.
_LIST_LIMIT = 100

_EPISODE_PATH = re.compile(r"^/viewer/chapter/(?P<id>\d+)/?$")
_SERIES_PATH = re.compile(r"^/title/(?P<id>\d+)/?$")
#: A page file lives under a 32-hex directory, which is also its RC4 key.
_PAGE_KEY = re.compile(r"/([0-9a-zA-Z]{32})/")

_API_HEADERS = {
    "Accept": "application/json",
    "Origin": BASE_URL,
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-site",
}


def decrypt(data: bytes, key: str) -> bytes:
    """Undo the RC4 the CDN serves page files under.

    The viewer XORs every page file with a plain RC4 key stream (the usual
    KSA and PRGA, byte for byte) keyed by the ASCII of `key`.

    Args:
        data: The file exactly as the CDN serves it.
        key: The page's key, `page_key()` of its URL.

    Returns:
        The JPEG file.
    """
    seed = key.encode()
    box = list(range(256))
    j = 0
    for i in range(256):
        j = (j + box[i] + seed[i % len(seed)]) % 256
        box[i], box[j] = box[j], box[i]
    out = bytearray(len(data))
    i = j = 0
    for n, byte in enumerate(data):
        i = (i + 1) % 256
        j = (j + box[i]) % 256
        box[i], box[j] = box[j], box[i]
        out[n] = byte ^ box[(box[i] + box[j]) % 256]
    return bytes(out)


def page_key(url: str) -> str | None:
    """The RC4 key of a page file, read off its URL the way the viewer does.

    Args:
        url: The page image URL.

    Returns:
        The 32-character directory name, or None when the file is served in
        the clear (the end-of-chapter card, for one).
    """
    match = _PAGE_KEY.search(url)
    return match[1] if match else None


def episode_url(chapter_id: str | int) -> str:
    """The canonical URL of a chapter.

    Args:
        chapter_id: The chapter id, `/viewer/chapter/<id>`.

    Returns:
        The chapter URL.
    """
    return f"{BASE_URL}/viewer/chapter/{chapter_id}"


def episode_title(chapter: Mapping[str, Any]) -> str:
    """Name a chapter the way the viewer's header does.

    Args:
        chapter: A chapter as `/api/chapter/detail` describes it.

    Returns:
        `name`, followed by `subName` when the site set one.
    """
    name = str(chapter.get("name") or chapter.get("id") or "")
    sub_name = chapter.get("subName")
    return f"{name} {sub_name}" if sub_name else name


class Cycomi(Extractor):
    """Fetch chapters from サイコミ."""

    NAME = "cycomi"
    HOSTS = ("cycomi.com",)
    URL_FORMS = (
        "https://cycomi.com/viewer/chapter/<chapter>",
        "https://cycomi.com/title/<title>",
    )
    # One host, one account.
    CONFIG_KEY = "cycomi"
    HEADERS: ClassVar[dict[str, str]] = {
        **Extractor.HEADERS,
        "sec-ch-ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Linux"',
    }

    @classmethod
    def suitable(cls, url: str) -> bool:
        """Report whether the extractor takes `url`.

        Args:
            url: The URL to check.

        Returns:
            True for a chapter viewer or a work page on the known host.
        """
        if not super().suitable(url):
            return False
        path = urlparse(url).path
        return _EPISODE_PATH.match(path) is not None or _SERIES_PATH.match(path) is not None

    @classmethod
    def is_series(cls, url: str) -> bool:
        """Report whether `url` names a whole work rather than one chapter.

        Args:
            url: The URL to check.

        Returns:
            True for `/title/<id>`.
        """
        return _SERIES_PATH.match(urlparse(url).path) is not None

    def series_urls(self, url: str) -> list[str]:
        """List every chapter of a work, first chapter first.

        `/api/chapter/paginatedList` hands the chapters over a hundred at a
        time, with a `nextCursor` to ask for the rest.

        Args:
            url: A work URL.

        Returns:
            One chapter URL per listed chapter, in the order the site lists them.

        Raises:
            UnsupportedUrlError: The URL is not a work page.
            NotAnEpisodePageError: The site knows no such work, or it lists no chapter.
        """
        match = _SERIES_PATH.match(urlparse(url).path)
        if match is None:
            msg = f"{url} is not a series page."
            raise UnsupportedUrlError(msg)
        title_id = match["id"]
        urls: list[str] = []
        cursor: Any = None
        while True:
            params: dict[str, str | int] = {"titleId": title_id, "sort": _SORT_ASC, "limit": _LIST_LIMIT}
            if cursor is not None:
                params["cursor"] = cursor
            body = self._api_get("/chapter/paginatedList", params, referer=url)
            if body is None:
                msg = f"no series {title_id} on {BASE_URL}."
                raise NotAnEpisodePageError(msg)
            for entry in body.get("data") or []:
                if isinstance(entry, dict) and entry.get("id") is not None:
                    candidate = episode_url(entry["id"])
                    if candidate not in urls:
                        urls.append(candidate)
            cursor = body.get("nextCursor")
            if cursor is None:
                break
        if not urls:
            msg = f"the series at {url} lists no episode."
            raise NotAnEpisodePageError(msg)
        return urls

    def episode(self, url: str) -> Episode:
        """Read one chapter and list its pages.

        `/api/chapter/detail` names the chapter and its work;
        `/api/chapter/page/list` hands the signed page URLs over, and answers
        with an empty list when the chapter wants a coin, a rental or a wait.

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
            msg = f"{url} is not an episode page."
            raise UnsupportedUrlError(msg)
        chapter_id = match["id"]

        detail = self._api_get("/chapter/detail", {"chapterId": chapter_id}, referer=url)
        chapter = detail.get("data") if detail is not None else None
        if not isinstance(chapter, dict):
            msg = f"no chapter {chapter_id} on {BASE_URL}."
            raise NotAnEpisodePageError(msg)
        title_id = chapter.get("titleId")

        listing = self._api_post("/chapter/page/list", {"titleId": title_id, "chapterId": int(chapter_id)}, referer=url)
        pages_data = listing.get("data") if listing is not None else None
        if not isinstance(pages_data, dict):
            msg = f"no pages for chapter {chapter_id} on {BASE_URL}."
            raise NotAnEpisodePageError(msg)

        # The list ends with `endAdPageCount` cards past `lastPageNumber`,
        # which are not part of the chapter.
        last_page = int(pages_data.get("lastPageNumber") or 0)
        pages: list[Page] = []
        for entry in pages_data.get("pages") or []:
            if not isinstance(entry, dict) or entry.get("type", "image") != "image" or not entry.get("image"):
                continue
            number = int(entry.get("pageNumber") or 0)
            if last_page and number > last_page:
                continue
            src = str(entry["image"])
            pages.append(
                Page(
                    url=src,
                    width=int(entry.get("width") or 0),
                    height=int(entry.get("height") or 0),
                    extra={"key": page_key(src), "page_number": number},
                )
            )

        preceding, following = pages_data.get("prev"), pages_data.get("next")
        prev_url = None
        if isinstance(preceding, dict) and preceding.get("chapterId") is not None:
            prev_url = episode_url(preceding["chapterId"])
        next_url = None
        if isinstance(following, dict) and following.get("chapterId") is not None:
            next_url = episode_url(following["chapterId"])

        return Episode(
            url=episode_url(chapter_id),
            series_title=str(chapter.get("titleName") or title_id or ""),
            episode_title=episode_title(chapter),
            pages=tuple(pages),
            prev_url=prev_url,
            next_url=next_url,
            metadata={"chapter": chapter, "pages": pages_data},
        )

    def image(self, page: Page, episode: Episode) -> Image.Image:
        """Fetch one page and decrypt it.

        The page URLs are signed CloudFront URLs; the signature is all the
        CDN checks, and it runs out in under an hour, so a page is fetched
        soon after `episode()` handed it over.

        Args:
            page: The page to fetch.
            episode: The chapter the page belongs to.

        Returns:
            The page image.
        """
        res = self._get(page.url, headers={**self.HEADERS, "Referer": episode.url}, timeout=self.IMAGE_TIMEOUT)
        key = page.extra.get("key")
        data = decrypt(res.content, str(key)) if key else res.content
        return Image.open(BytesIO(data))

    def login(self, url: str, username: str, password: str) -> None:  # noqa: ARG002 (one site, one login route)
        """Sign in with an email address, so bought and rented chapters become readable.

        The email form posts JSON to the app's own `/api/auth/login/email`,
        which sets the session cookie the backend reads afterwards. The site
        answers a wrong pair with `resultCode` 903000 and nothing more.
        Accounts made through Cygames ID or Apple cannot sign in this way.

        Args:
            url: Any URL on the site.
            username: The account's email address.
            password: The account's password.

        Raises:
            LoginError: The site refused the credentials.
        """
        res = self._session.post(
            LOGIN_URL,
            json={"email": username, "password": password, "keepFlag": True},
            headers={**self.HEADERS, **_API_HEADERS, "Referer": f"{BASE_URL}/auth/login/email"},
            timeout=self.TIMEOUT,
        )
        res.raise_for_status()
        body = res.json()
        code = body.get("resultCode") if isinstance(body, dict) else None
        if code != _RESULT_OK:
            msg = f"{BASE_URL} refused the credentials for {username!r} (resultCode {code})."
            raise LoginError(msg)

    def _api_get(self, path: str, params: Mapping[str, str | int], *, referer: str) -> dict[str, Any] | None:
        """GET an API route; None when the site answers with an error code."""
        res = self._get(
            f"{API_URL}{path}",
            params=params,
            headers={**self.HEADERS, **_API_HEADERS, "Referer": referer},
        )
        return _answer(res.json())

    def _api_post(self, path: str, payload: Mapping[str, Any], *, referer: str) -> dict[str, Any] | None:
        """POST JSON to an API route; None when the site answers with an error code."""
        res = self._session.post(
            f"{API_URL}{path}",
            json=dict(payload),
            headers={**self.HEADERS, **_API_HEADERS, "Content-Type": "application/json", "Referer": referer},
            timeout=self.TIMEOUT,
        )
        res.raise_for_status()
        return _answer(res.json())


def _answer(body: Any) -> dict[str, Any] | None:  # noqa: ANN401 (whatever `.json()` decoded)
    """The API's answer when it succeeded, else None.

    Every route answers `{"resultCode": 1, "data": ...}` on success and a
    bare `{"resultCode": <error>}` otherwise, with 200 either way.
    """
    if isinstance(body, dict) and body.get("resultCode") == _RESULT_OK:
        return body
    return None
