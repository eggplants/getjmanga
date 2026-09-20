"""ドリコミプラス (Drecom), whose viewer opens a session and serves AES-CBC pages with an HMAC over each."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import struct
from http import HTTPStatus
from io import BytesIO
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from PIL import Image

from getjmanga.errors import GetjmangaError, LoginError, NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractor import Episode, Extractor, Page

if TYPE_CHECKING:
    from collections.abc import Mapping

    from httpx import Client, Response

BASE_URL = "https://drecomi-plus.jp"
#: The Next.js app's backend; the work page, the viewer and sign-in all go through it.
API_URL = "https://api.drecomi-plus.jp/api/v1/app"
#: How many episodes the site asks its listing for per page.
LIST_LIMIT = 200

# `/series/<series>/episodes/<episode>`, `CD20013` and `CD20013-001-001`; the site rewrites the address to
# this shape when an episode opens.
_EPISODE_PATH = re.compile(r"^/series/(?P<series>[A-Z]+\d+)/episodes/(?P<episode>[A-Z]+\d+(?:-\d+)+)/?$")
# `/series/<series>`, the work page with the episode list (`/series/genre`, `/series/author` and
# `/series/label` are listings, hence the digits).
_SERIES_PATH = re.compile(r"^/series/(?P<series>[A-Z]+\d+)/?$")

_API_HEADERS = {
    "Accept": "application/json",
    "Origin": BASE_URL,
    "Referer": f"{BASE_URL}/",
}


def _b64(value: str) -> bytes:
    return base64.b64decode(value)


def _framed(*parts: bytes) -> bytes:
    """Concatenate `parts`, each behind a zero 32-bit word and its 32-bit big-endian length."""
    return b"".join(struct.pack(">II", 0, len(part)) + part for part in parts)


def verify(data: bytes, hmac_key: str, iv: str, auth_tag: str, identity: Mapping[str, str | int]) -> bool:
    """Check a page file against the `auth_tag` the viewer session handed over.

    The tag is an HMAC-SHA256 with the session's `hmac_key` over the
    ciphertext, the IV and the page's identity (`content_id`,
    `content_type` and `page_number`, as a compact JSON list of `{k, v}`
    pairs with string values), each framed by an 8-byte length.

    Args:
        data: The `.webp.enc` file exactly as the CDN serves it.
        hmac_key: The session's `hmac_key`, base64.
        iv: The page's `iv`, base64.
        auth_tag: The page's `auth_tag`, base64.
        identity: The session's `content_id` and `content_type` and the page's `page_number`.

    Returns:
        True when the tag matches.
    """
    pairs = [{"k": key, "v": str(identity.get(key, ""))} for key in ("content_id", "content_type", "page_number")]
    message = _framed(data, _b64(iv), json.dumps(pairs, separators=(",", ":")).encode())
    digest = hmac.new(_b64(hmac_key), message, hashlib.sha256).digest()
    return hmac.compare_digest(digest, _b64(auth_tag))


def decrypt(data: bytes, key: str, iv: str) -> bytes:
    """Undo the AES-256-CBC the CDN serves page files under.

    Args:
        data: The `.webp.enc` file exactly as the CDN serves it.
        key: The session's `session_key`, base64.
        iv: The page's `iv`, base64.

    Returns:
        The WebP file.

    Raises:
        GetjmangaError: The data is not a whole number of blocks, or the padding is off.
    """
    if len(data) % 16 or not data:
        msg = "the encrypted page is not a whole number of AES blocks."
        raise GetjmangaError(msg)
    decryptor = Cipher(algorithms.AES(_b64(key)), modes.CBC(_b64(iv))).decryptor()
    plain = decryptor.update(data) + decryptor.finalize()
    padding = plain[-1]
    if not 1 <= padding <= 16 or plain[-padding:] != bytes([padding]) * padding:  # noqa: PLR2004 (PKCS#7 block)
        msg = "the decrypted page carries no PKCS#7 padding; wrong key or iv?"
        raise GetjmangaError(msg)
    return plain[:-padding]


def episode_url(series_code: str, episode_code: str) -> str:
    """The canonical URL of an episode.

    Args:
        series_code: The series code, `CD20013`.
        episode_code: The episode code, `CD20013-001-001`.

    Returns:
        The episode URL.
    """
    return f"{BASE_URL}/series/{series_code}/episodes/{episode_code}"


class Drecomi(Extractor):
    """Fetch episodes from ドリコミプラス."""

    NAME = "drecomi"
    HOSTS = ("drecomi-plus.jp",)
    URL_FORMS = (
        "https://drecomi-plus.jp/series/<series>/episodes/<episode>",
        "https://drecomi-plus.jp/series/<series>",
    )
    CONFIG_KEY = "drecomi"

    def __init__(self, session: Client | None = None) -> None:
        """Build an extractor.

        Args:
            session: A session to reuse. A retrying one is made when omitted.
        """
        super().__init__(session)
        #: The bearer token `login()` obtained; the API takes it on every request.
        self._token: str | None = None

    @classmethod
    def suitable(cls, url: str) -> bool:
        """Report whether the extractor takes `url`.

        Args:
            url: The URL to check.

        Returns:
            True for an episode or a series page on the known host.
        """
        if not super().suitable(url):
            return False
        path = urlparse(url).path
        return _EPISODE_PATH.match(path) is not None or _SERIES_PATH.match(path) is not None

    @classmethod
    def is_series(cls, url: str) -> bool:
        """Report whether `url` names a whole series rather than one episode.

        Args:
            url: The URL to check.

        Returns:
            True for `/series/<series>`.
        """
        return _SERIES_PATH.match(urlparse(url).path) is not None

    def series_urls(self, url: str) -> list[str]:
        """List every episode of a series, first episode first.

        Args:
            url: A series URL.

        Returns:
            One episode URL per listed episode, by episode number.

        Raises:
            UnsupportedUrlError: The URL is not a series page.
            NotAnEpisodePageError: The site knows no such series, or it lists no episode.
        """
        match = _SERIES_PATH.match(urlparse(url).path)
        if match is None:
            msg = f"{url} is not a series page."
            raise UnsupportedUrlError(msg)
        series_code = match["series"]
        urls: list[str] = []
        page, total_pages = 1, 1
        while page <= total_pages:
            res = self._api(
                "/episodes",
                params={
                    "series_code": series_code,
                    "page": page,
                    "limit": LIST_LIMIT,
                    "sort": "episode_number",
                    "order": "asc",
                },
            )
            if res.status_code == HTTPStatus.NOT_FOUND:
                msg = f"no series {series_code} on {BASE_URL}."
                raise NotAnEpisodePageError(msg)
            res.raise_for_status()
            listing = _json_or_none(res)
            if not isinstance(listing, dict):
                break
            for entry in listing.get("items") or []:
                candidate = episode_url(series_code, str(entry.get("code") or ""))
                if isinstance(entry, dict) and entry.get("code") and candidate not in urls:
                    urls.append(candidate)
            total_pages = int((listing.get("pagination") or {}).get("totalPages") or 0)
            page += 1
        if not urls:
            msg = f"the series at {url} lists no episode."
            raise NotAnEpisodePageError(msg)
        return urls

    def episode(self, url: str) -> Episode:
        """Read one episode and list its pages.

        `/episodes/<code>` names the episode and its series; a viewer session
        (`POST /viewer/episodes/<code>/session`) hands over the page files
        with the keys to open them, or refuses (401 without an account, 403
        without a purchase) when the episode costs coins; `/episodes/<code>/next`
        names the episode after it, 404 at the end of the series.

        Args:
            url: The episode URL.

        Returns:
            The episode. `pages` is empty when it is not readable.

        Raises:
            UnsupportedUrlError: The URL is not an episode URL.
            NotAnEpisodePageError: The site knows no such episode.
        """
        match = _EPISODE_PATH.match(urlparse(url).path)
        if match is None:
            msg = f"{url} is not an episode page."
            raise UnsupportedUrlError(msg)
        episode_code = match["episode"]

        res = self._api(f"/episodes/{episode_code}")
        detail = _json_or_none(res) if res.status_code == HTTPStatus.OK else None
        if not isinstance(detail, dict) or not detail.get("code"):
            msg = f"no episode {episode_code} on {BASE_URL}."
            raise NotAnEpisodePageError(msg)
        series_code = str(detail.get("series_code") or match["series"])

        viewer = self._viewer(episode_code)
        pages = self._pages(viewer) if viewer is not None else ()

        res = self._api(f"/episodes/{episode_code}/next")
        following = _json_or_none(res) if res.status_code == HTTPStatus.OK else None
        next_url = None
        if isinstance(following, dict) and following.get("code"):
            next_url = episode_url(str(following.get("series_code") or series_code), str(following["code"]))
        # The API has a `/next` but no `/previous`; the series listing has both.
        canonical = episode_url(series_code, episode_code)
        prev_url = self._listed_neighbours(f"{BASE_URL}/series/{series_code}", canonical)[0]

        return Episode(
            url=canonical,
            series_title=str(detail.get("series_title") or series_code),
            episode_title=str((viewer or {}).get("episode_name") or detail.get("name") or episode_code),
            pages=pages,
            prev_url=prev_url,
            next_url=next_url,
            metadata={"episode": detail, "viewer": viewer, "next": following},
        )

    def image(self, page: Page, episode: Episode) -> Image.Image:
        """Fetch one page file from the CDN, check it and decrypt it.

        The CDN serves the `.webp.enc` files to anyone; the keys in
        `page.extra` are what the viewer session handed over.

        Args:
            page: The page to fetch.
            episode: The episode the page belongs to.

        Returns:
            The page image.

        Raises:
            GetjmangaError: The file does not match its `auth_tag`.
        """
        res = self._get(
            page.url,
            headers={**self.HEADERS, "Accept": "*/*", "Origin": BASE_URL, "Referer": episode.url},
            timeout=self.IMAGE_TIMEOUT,
        )
        extra = page.extra
        identity = {key: extra.get(key, "") for key in ("content_id", "content_type", "page_number")}
        if not verify(res.content, str(extra["hmac_key"]), str(extra["iv"]), str(extra["auth_tag"]), identity):
            msg = f"the page file at {page.url} does not match its auth tag."
            raise GetjmangaError(msg)
        return Image.open(BytesIO(decrypt(res.content, str(extra["key"]), str(extra["iv"]))))

    def login(self, url: str, username: str, password: str) -> None:  # noqa: ARG002 (one sign-in endpoint)
        """Sign in, so episodes the account has bought become readable.

        Args:
            url: Ignored; ドリコミプラス has one sign-in endpoint.
            username: The email address the account uses.
            password: The account's password.

        Raises:
            LoginError: The site refused the credentials.
        """
        res = self._session.post(
            f"{API_URL}/auth/login",
            json={"email": username, "password": password},
            headers={**self.HEADERS, **_API_HEADERS, "Content-Type": "application/json"},
            timeout=self.TIMEOUT,
        )
        answer = _json_or_none(res)
        token = answer.get("access_token") if isinstance(answer, dict) else None
        if not res.is_success or not token:
            reason = answer.get("error") if isinstance(answer, dict) else None
            msg = f"drecomi-plus.jp refused the credentials for {username!r}: {reason or f'HTTP {res.status_code}'}."
            raise LoginError(msg)
        self._token = str(token)

    def _headers(self) -> dict[str, str]:
        headers = {**self.HEADERS, **_API_HEADERS}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _api(self, path: str, *, params: Mapping[str, str | int] | None = None) -> Response:
        """GET an API path; the caller reads the status (404 is meaningful)."""
        return self._session.get(
            f"{API_URL}{path}",
            params=dict(params) if params is not None else None,
            headers=self._headers(),
            timeout=self.TIMEOUT,
        )

    def _viewer(self, episode_code: str) -> dict[str, Any] | None:
        """Open a viewer session for an episode, or None when the site withholds it.

        A readable episode answers 201 with its pages and keys; one that
        costs coins answers 401 (`AUTHENTICATION_REQUIRED`) without an
        account and 403 without a purchase.
        """
        res = self._session.post(
            f"{API_URL}/viewer/episodes/{episode_code}/session",
            headers=self._headers(),
            timeout=self.TIMEOUT,
        )
        if res.status_code in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN, HTTPStatus.NOT_FOUND):
            return None
        res.raise_for_status()
        body = _json_or_none(res)
        return body if isinstance(body, dict) else None

    @staticmethod
    def _pages(viewer: dict[str, Any]) -> tuple[Page, ...]:
        """The pages of a viewer session, in `page_number` order, each carrying its keys."""
        key, hmac_key = str(viewer.get("session_key") or ""), str(viewer.get("hmac_key") or "")
        content_id, content_type = str(viewer.get("content_id") or ""), str(viewer.get("content_type") or "episode")
        entries = sorted(
            (p for p in viewer.get("pages") or [] if isinstance(p, dict) and p.get("image_url")),
            key=lambda p: int(p.get("page_number") or 0),
        )
        return tuple(
            Page(
                url=str(p["image_url"]),
                extra={
                    "key": key,
                    "hmac_key": hmac_key,
                    "iv": str(p.get("iv") or ""),
                    "auth_tag": str(p.get("auth_tag") or ""),
                    "content_id": content_id,
                    "content_type": content_type,
                    "page_number": int(p.get("page_number") or 0),
                },
            )
            for p in entries
        )


def _json_or_none(res: Response) -> Any:  # noqa: ANN401 (whatever JSON the site sent)
    """`res.json()`, or None when the body is not JSON."""
    try:
        return res.json()
    except ValueError:
        return None
