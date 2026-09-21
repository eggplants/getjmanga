"""レジンコミックス (Lezhin Comics Japan), whose viewer XORs every page file with one site-wide key.

The site is a Next.js app in front of a JSON API at `https://lezhin.jp/api`.
A chapter's pages come from `/comic/<title>/chapter/<hash>/viewer` as
presigned S3 URLs that expire after an hour, and the files they serve are
WebPs XORed with a 32-byte key that sits in the viewer bundle -- there is no
per-page key. BeLToon, the other Lezhin Entertainment site, runs the Balcony
platform instead and shares nothing with this one.
"""

from __future__ import annotations

import re
from http import HTTPStatus
from io import BytesIO
from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import urljoin, urlparse

from PIL import Image

from getjmanga.cipher import xor_unmask
from getjmanga.errors import LoginError, NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractor import Episode, Extractor, Page

if TYPE_CHECKING:
    from httpx import Client, Response

BASE_URL = "https://lezhin.jp"
#: The API the Next.js app talks to; same origin, no token needed for a free chapter.
API_URL = f"{BASE_URL}/api"
LOGIN_URL = f"{API_URL}/auth/login"
#: The key the viewer bundle hard-codes as `crypto:{method:"xor",key:...}` for every page.
#: `_discover_key()` reads the bundle again should the site rotate it.
XOR_KEY = "57e87c8a4d50b7c3456dbab4ab144b200826e62459039c9915d1e5f5e0bf3a51"
#: How many chapters one `all-chapters` page asks for.
PAGE_SIZE = 100

# `/comic/<title>/chapter/<hash>/viewer`, the reader; without `/viewer` the
# site serves its catch-all page, but the chapter is the same one.
_EPISODE_PATH = re.compile(r"^/comic/(?P<title>[A-Za-z0-9._~+-]+)/chapter/(?P<chapter>[A-Za-z0-9]+)(?:/viewer)?/?$")
# `/comic/<title>`, the work page with the chapter list.
_SERIES_PATH = re.compile(r"^/comic/(?P<title>[A-Za-z0-9._~+-]+)/?$")
# `crypto:{method:"xor",key:"..."}` in the viewer bundle.
_BUNDLE_KEY = re.compile(r'method:"xor",key:"(?P<key>[0-9a-fA-F]{2,})"')
_BUNDLE_SRC = re.compile(r'<script[^>]+src="(?P<src>/_next/static/chunks/[^"]+\.js)"')

_API_HEADERS = {
    "Accept": "application/json",
    "X-Platform": "D",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}
_IMAGE_MAGICS = (b"RIFF", b"\xff\xd8\xff", b"\x89PNG")


def episode_url(title_id: str, chapter_id: str) -> str:
    """The canonical URL of a chapter.

    Args:
        title_id: The work's slug, `/comic/<title>`.
        chapter_id: The chapter's hash id, `/chapter/<hash>`.

    Returns:
        The reader URL.
    """
    return f"{BASE_URL}/comic/{title_id}/chapter/{chapter_id}/viewer"


#: Where a work page inlines its authors: the flight payload escapes its quotes.
_AUTHORS = re.compile(r'\\"author\\":\{\\"data\\":\[(?P<data>.*?)\]\}')
_AUTHOR_NAME = re.compile(r'\\"name\\":\\"(?P<name>[^"\\]*)\\"')


def authors_of(html: str) -> list[str]:
    """The author names a work page's inlined `comicDetailData` lists, in order.

    Args:
        html: The work page.

    Returns:
        The names; empty when the page inlines none.
    """
    match = _AUTHORS.search(html)
    if match is None:
        return []
    return [name for name in _AUTHOR_NAME.findall(match["data"]) if name]


def _results(status: int, body: dict[str, Any]) -> dict[str, Any]:
    """The `results` of a successful API answer, `{}` for any other."""
    results = body.get("results") if status == HTTPStatus.OK else None
    return results if isinstance(results, dict) else {}


def looks_like_image(data: bytes) -> bool:
    """Report whether `data` starts like a WebP, a JPEG or a PNG.

    Args:
        data: A page file after unmasking.

    Returns:
        True when the magic bytes are those of an image the viewer serves.
    """
    return data.startswith(_IMAGE_MAGICS)


class Lezhin(Extractor):
    """Fetch chapters from レジンコミックス."""

    NAME = "lezhin"
    HOSTS = ("lezhin.jp",)
    PUBLISHER = "レジンエンターテインメント"
    URL_FORMS = (
        "https://lezhin.jp/comic/<title>/chapter/<hash>/viewer",
        "https://lezhin.jp/comic/<title>/chapter/<hash>",
        "https://lezhin.jp/comic/<title>",
    )
    # One host, one account; `www.` only redirects here.
    CONFIG_KEY = "lezhin"
    HEADERS: ClassVar[dict[str, str]] = {
        **Extractor.HEADERS,
        "sec-ch-ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Linux"',
    }

    def __init__(self, session: Client | None = None) -> None:
        """Build an extractor.

        Args:
            session: A session to reuse. A retrying one is made when omitted.
        """
        super().__init__(session)
        #: The bearer token `login()` obtained, sent with every API call afterwards.
        self._token: str | None = None
        #: The XOR key pages are unmasked with, replaced once `_discover_key()` found a newer one.
        self._key = XOR_KEY
        #: The credits of each work page read, by title id.
        self._credits: dict[str, str] = {}

    @classmethod
    def suitable(cls, url: str) -> bool:
        """Report whether the extractor takes `url`.

        Args:
            url: The URL to check.

        Returns:
            True for a chapter or a work page on the known host.
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
            True for `/comic/<title>`.
        """
        return _SERIES_PATH.match(urlparse(url).path) is not None

    def series_urls(self, url: str) -> list[str]:
        """List every chapter of a work, first chapter first.

        `all-chapters` pages through the list in display order, volume
        groups flattened; free and paid chapters alike are listed.

        Args:
            url: A work URL.

        Returns:
            One chapter URL per listed chapter.

        Raises:
            UnsupportedUrlError: The URL is not a work page.
            NotAnEpisodePageError: The site knows no such work, or lists no chapter.
        """
        match = _SERIES_PATH.match(urlparse(url).path)
        if match is None:
            msg = f"{url} is not a work page."
            raise UnsupportedUrlError(msg)
        title_id = match["title"]
        urls: list[str] = []
        page, last_page = 1, 1
        while page <= last_page:
            status, body = self._api(
                f"comic/{title_id}/all-chapters", referer=url, params={"page": page, "page_size": PAGE_SIZE}
            )
            results = body.get("results") if status == HTTPStatus.OK else None
            if not isinstance(results, dict):
                msg = f"no readable work {title_id} on {BASE_URL}: {body.get('message', status)}"
                raise NotAnEpisodePageError(msg)
            for group in results.get("data") or []:
                for entry in group.get("chapters") or []:
                    candidate = episode_url(title_id, str(entry["hash_id"]))
                    if candidate not in urls:
                        urls.append(candidate)
            last_page = int((results.get("pagination") or {}).get("last_page") or 1)
            page += 1
        if not urls:
            msg = f"the work at {url} lists no chapter."
            raise NotAnEpisodePageError(msg)
        return urls

    def episode(self, url: str) -> Episode:
        """Read one chapter and list its pages.

        `general-info` names the chapter, the work and the chapter after it;
        `viewer` hands the page URLs over, or answers 400 `not_purchased`
        when the chapter wants points, and 400 `title_is_safe_mode` for an
        adult work read without an adult-mode account.

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
        title_id, chapter_id = match["title"], match["chapter"]
        canonical = episode_url(title_id, chapter_id)

        status, body = self._api(f"comic/{title_id}/chapter/{chapter_id}/general-info", referer=canonical)
        if status == HTTPStatus.NOT_FOUND:
            msg = f"no chapter {chapter_id} of {title_id} on {BASE_URL}: {body.get('message', '')}"
            raise NotAnEpisodePageError(msg)
        info = _results(status, body)
        item = info.get("item") or {}
        prev_item, next_item = info.get("previous_item") or {}, info.get("next_item") or {}
        prev_url = episode_url(title_id, str(prev_item["hash_id"])) if prev_item.get("hash_id") else None
        next_url = episode_url(title_id, str(next_item["hash_id"])) if next_item.get("hash_id") else None

        status, body = self._api(f"comic/{title_id}/chapter/{chapter_id}/viewer", referer=canonical)
        if status == HTTPStatus.NOT_FOUND:
            msg = f"no chapter {chapter_id} of {title_id} on {BASE_URL}: {body.get('message', '')}"
            raise NotAnEpisodePageError(msg)
        viewer = _results(status, body)
        images = sorted(viewer.get("image_paths") or [], key=lambda entry: int(entry.get("display_order") or 0))
        pages = tuple(
            Page(
                url=str(entry["image_path"]),
                width=int(entry.get("width") or 0),
                height=int(entry.get("height") or 0),
                extra={"key": self._key},
            )
            for entry in images
        )
        return self._dated_by_upload(
            Episode(
                url=canonical,
                series_title=str((item.get("title") or {}).get("name") or title_id),
                episode_title=str(item.get("name") or chapter_id),
                pages=pages,
                prev_url=prev_url,
                next_url=next_url,
                metadata={"info": info, "viewer": viewer, "error": None if pages else body.get("message")},
                writer=self._writer(title_id),
                publisher=self.PUBLISHER,
            )
        )

    def _writer(self, title_id: str) -> str:
        """The authors the work page renders, read once per work.

        No API answers with them: the Next.js page inlines its
        `comicDetailData`, whose `author.data` lists the names, in a flight
        payload, so they are picked out of that.
        """
        if title_id not in self._credits:
            html = self._get(f"{BASE_URL}/comic/{title_id}").text
            self._credits[title_id] = ", ".join(authors_of(html))
        return self._credits[title_id]

    def image(self, page: Page, episode: Episode) -> Image.Image:
        """Fetch one page from the presigned URL and unmask it.

        The URL carries its own signature, so no Referer or cookie is
        needed. Should the key in `page.extra` not yield an image, the
        viewer bundle is read for the current one and the page tried again.

        Args:
            page: The page to fetch.
            episode: The episode the page belongs to.

        Returns:
            The page image.
        """
        res = self._get(page.url, headers={**self.HEADERS, "Referer": episode.url}, timeout=self.IMAGE_TIMEOUT)
        data = xor_unmask(res.content, str(page.extra.get("key") or self._key))
        if not looks_like_image(data):
            self._key = self._discover_key(episode.url)
            data = xor_unmask(res.content, self._key)
        return Image.open(BytesIO(data))

    def login(self, url: str, username: str, password: str) -> None:  # noqa: ARG002 (one site, one login endpoint)
        """Sign in with an email address, so bought chapters become readable.

        `/api/auth/login` is the app's own route in front of the API's
        `/auth/login/email`; it answers with a bearer token that every API
        call carries afterwards. Accounts made through LINE, Google, X,
        Facebook, Apple or Yahoo! JAPAN cannot sign in this way.

        Args:
            url: Any URL on the site.
            username: The account's email address.
            password: The account's password.

        Raises:
            LoginError: The site refused the credentials.
        """
        res = self._session.post(
            LOGIN_URL,
            json={"email": username, "password": password},
            headers={**self.HEADERS, **_API_HEADERS, "Origin": BASE_URL, "Referer": f"{BASE_URL}/login"},
            timeout=self.TIMEOUT,
        )
        body = self._json(res)
        results = body.get("results") if res.status_code == HTTPStatus.OK else None
        token = results.get("access_token") if isinstance(results, dict) else None
        if not token:
            reason = body.get("message") or f"HTTP {res.status_code}"
            msg = f"{BASE_URL} refused the credentials for {username!r}: {reason}"
            raise LoginError(msg)
        self._token = str(token)

    def _api(
        self, path: str, *, referer: str, params: dict[str, str | int] | None = None
    ) -> tuple[int, dict[str, Any]]:
        """GET one API endpoint and hand back its status and its JSON body.

        A 400 or a 404 is the API's answer for a locked chapter and an
        unknown one, so the caller sees the status rather than an exception.
        """
        headers = {**self.HEADERS, **_API_HEADERS, "Referer": referer}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        res = self._session.get(f"{API_URL}/{path}", headers=headers, params=params, timeout=self.TIMEOUT)
        if res.status_code not in (HTTPStatus.OK, HTTPStatus.BAD_REQUEST, HTTPStatus.NOT_FOUND):
            res.raise_for_status()
        return res.status_code, self._json(res)

    @staticmethod
    def _json(res: Response) -> dict[str, Any]:
        """The response body as a dict, `{}` when it is not JSON."""
        try:
            body = res.json()
        except ValueError:
            return {}
        return body if isinstance(body, dict) else {}

    def _discover_key(self, viewer_url: str) -> str:
        """Read the XOR key out of the viewer page's bundles.

        Args:
            viewer_url: A reader URL, whose page lists the bundles.

        Returns:
            The key, hex.

        Raises:
            NotAnEpisodePageError: No bundle of the page carries one.
        """
        html = self._get(viewer_url).text
        for match in _BUNDLE_SRC.finditer(html):
            script = self._get(urljoin(BASE_URL, match["src"])).text
            found = _BUNDLE_KEY.search(script)
            if found is not None:
                return found["key"].lower()
        msg = f"no XOR key in the bundles of {viewer_url}."
        raise NotAnEpisodePageError(msg)
