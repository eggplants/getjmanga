"""ゼロサムオンライン (Zero-Sum Online), 一迅社's free web magazine and its protobuf API.

The site is a Next.js app whose viewer reads everything off
`api.zerosumonline.com` as protobuf messages (`Proto.TitleView`,
`Proto.MangaViewerView`; the field numbers below are read out of the site's
bundled JS). A chapter id only ever appears in a URL AES-encrypted with a
passphrase the bundle carries (crypto-js `AES.encrypt(id, passphrase)`, with a
random salt, so the same chapter has many URLs), and the server decrypts it
the same way. Pages are plain WebP files served without a cookie or a Referer.
"""

from __future__ import annotations

import base64
import hashlib
import re
from http import HTTPStatus
from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import quote, unquote, urlparse

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from getjmanga.errors import NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractor import Episode, Extractor, Page, neighbours
from getjmanga.protobuf import integer, message, messages, string

if TYPE_CHECKING:
    from httpx import Client

BASE_URL = "https://zerosumonline.com"
API_URL = "https://api.zerosumonline.com/api/v1"

#: The passphrase the site's `AES.encrypt()` is keyed with when it puts a chapter id
#: in a URL. It sits in the site's public JS bundle; it is not a secret.
CHAPTER_ID_KEY = "gMx7rLcYtPafs46"
_SALTED_MAGIC = b"Salted__"
_SALT_LENGTH = 8
_KEY_LENGTH = 32
_IV_LENGTH = 16

# `/episode/<tag>/chapter/<token>`: `tag` names the series, `token` is the encrypted chapter id.
_EPISODE_PATH = re.compile(r"^/episode/(?P<tag>[^/]+)/chapter/(?P<token>[^/]+)/?$")
# `/detail/<tag>`: the series page, listing the chapters that are currently public.
_SERIES_PATH = re.compile(r"^/detail/(?P<tag>[^/]+)/?$")

_API_HEADERS = {
    "Accept": "*/*",
    "Origin": BASE_URL,
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-site",
}

#: `Proto.MangaViewerView.Status.SUCCESS`; anything else means the chapter is not served.
_STATUS_SUCCESS = 0


def _bytes_to_key(passphrase: bytes, salt: bytes) -> tuple[bytes, bytes]:
    """OpenSSL's `EVP_BytesToKey` with MD5, which crypto-js derives a key and IV with."""
    derived = block = b""
    while len(derived) < _KEY_LENGTH + _IV_LENGTH:
        block = hashlib.md5(block + passphrase + salt).digest()  # noqa: S324 (what the site does, not a choice)
        derived += block
    return derived[:_KEY_LENGTH], derived[_KEY_LENGTH : _KEY_LENGTH + _IV_LENGTH]


def encrypt_chapter_id(chapter_id: int, key: str = CHAPTER_ID_KEY) -> str:
    """Encrypt a chapter id the way the site does for its URLs.

    The site salts every encryption at random; this one derives the salt from
    the id instead, so the same chapter always gets the same URL. The server
    accepts either, it only reads the salt out of the token.

    Args:
        chapter_id: The chapter id.
        key: The passphrase to key the cipher with.

    Returns:
        The token, URL-encoded as it appears in `/episode/<tag>/chapter/<token>`.
    """
    salt = hashlib.sha256(str(chapter_id).encode()).digest()[:_SALT_LENGTH]
    aes_key, iv = _bytes_to_key(key.encode(), salt)
    padder = padding.PKCS7(algorithms.AES.block_size).padder()
    plain = padder.update(str(chapter_id).encode()) + padder.finalize()
    encryptor = Cipher(algorithms.AES(aes_key), modes.CBC(iv)).encryptor()
    token = base64.b64encode(_SALTED_MAGIC + salt + encryptor.update(plain) + encryptor.finalize()).decode()
    return quote(token, safe="")


def decrypt_chapter_id(token: str, key: str = CHAPTER_ID_KEY) -> int:
    """Read the chapter id out of a URL token.

    Args:
        token: The last path segment of an episode URL, URL-encoded or not.
        key: The passphrase the site keyed the cipher with.

    Returns:
        The chapter id.

    Raises:
        ValueError: The token is not an encrypted chapter id.
    """
    try:
        raw = base64.b64decode(unquote(token), validate=True)
    except ValueError as error:
        msg = f"{token!r} is not a base64 token."
        raise ValueError(msg) from error
    if not raw.startswith(_SALTED_MAGIC) or len(raw) < len(_SALTED_MAGIC) + _SALT_LENGTH:
        msg = f"{token!r} carries no salt."
        raise ValueError(msg)
    aes_key, iv = _bytes_to_key(key.encode(), raw[len(_SALTED_MAGIC) : len(_SALTED_MAGIC) + _SALT_LENGTH])
    body = raw[len(_SALTED_MAGIC) + _SALT_LENGTH :]
    if not body or len(body) % (algorithms.AES.block_size // 8):
        msg = f"{token!r} is not a whole number of cipher blocks."
        raise ValueError(msg)
    decryptor = Cipher(algorithms.AES(aes_key), modes.CBC(iv)).decryptor()
    unpadder = padding.PKCS7(algorithms.AES.block_size).unpadder()
    try:
        plain = unpadder.update(decryptor.update(body) + decryptor.finalize()) + unpadder.finalize()
        return int(plain.decode())
    except ValueError as error:
        msg = f"{token!r} does not decrypt to a chapter id."
        raise ValueError(msg) from error


def episode_url(tag: str, chapter_id: int) -> str:
    """The canonical URL of a chapter.

    Args:
        tag: The series tag, `/detail/<tag>`.
        chapter_id: The chapter id.

    Returns:
        The episode URL, with the chapter id encrypted the way the site wants it.
    """
    return f"{BASE_URL}/episode/{tag}/chapter/{encrypt_chapter_id(chapter_id)}"


# --- the messages ---------------------------------------------------------------


def decode_chapter(buf: bytes) -> dict[str, Any]:
    """Decode a `Proto.Chapter`.

    Args:
        buf: The encoded message.

    Returns:
        The chapter's id, name, thumbnail and its public window as Unix times.
    """
    fields = message(buf)
    return {
        "id": integer(fields, 1),
        "name": string(fields, 2),
        "imageUrl": string(fields, 3),
        "startTime": integer(fields, 4),
        "endTime": integer(fields, 5),
        "nextUpdateTime": string(fields, 6),
    }


def decode_title(buf: bytes) -> dict[str, Any]:
    """Decode a `Proto.Title`.

    Args:
        buf: The encoded message.

    Returns:
        The series' id, tag, name, author, description and thumbnail.
    """
    fields = message(buf)
    return {
        "id": integer(fields, 1),
        "tag": string(fields, 2),
        "name": string(fields, 3),
        "nameKana": string(fields, 4),
        "author": string(fields, 5),
        "description": string(fields, 7),
        "imgUrl": string(fields, 8),
        "startTime": integer(fields, 9),
        "endTime": integer(fields, 10),
        "latestChapterId": integer(fields, 15),
    }


def decode_title_view(buf: bytes) -> dict[str, Any]:
    """Decode a `Proto.TitleView`, the `/title` answer.

    Args:
        buf: The encoded message.

    Returns:
        `title` and `chapters`, the public chapters newest first as the site lists them.
    """
    fields = message(buf)
    title = messages(fields, 2)
    return {
        "title": decode_title(title[-1]) if title else {},
        "chapters": [decode_chapter(chapter) for chapter in messages(fields, 3)],
    }


def decode_viewer(buf: bytes) -> dict[str, Any]:
    """Decode a `Proto.MangaViewerView`, the `/viewer` answer.

    Args:
        buf: The encoded message.

    Returns:
        `status`, the series id and tag, the chapter's `viewerTitle` and its
        `pages`, the image URLs in reading order. The last-page card the viewer
        appends, which carries no image, is left out.
    """
    fields = message(buf)
    pages = [string(message(page), 1) for page in messages(fields, 5)]
    return {
        "status": integer(fields, 1),
        "titleId": integer(fields, 2),
        "titleTag": string(fields, 3),
        "viewerTitle": string(fields, 4),
        "pages": [page for page in pages if page],
    }


class ZeroSum(Extractor):
    """Fetch episodes from ゼロサムオンライン."""

    NAME = "zerosum"
    HOSTS = ("zerosumonline.com",)
    URL_FORMS = (
        "https://zerosumonline.com/episode/<tag>/chapter/<token>",
        "https://zerosumonline.com/detail/<tag>",
    )
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
        # `/title` answers per series; a bulk run asks for the same series
        # once per chapter, so it is kept.
        self._listings: dict[str, dict[str, Any]] = {}

    @classmethod
    def suitable(cls, url: str) -> bool:
        """Report whether the extractor takes `url`.

        Args:
            url: The URL to check.

        Returns:
            True for an episode or a series page on the site.
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
            True for `/detail/<tag>` on the site.
        """
        return cls.suitable(url) and _SERIES_PATH.match(urlparse(url).path) is not None

    def series_urls(self, url: str) -> list[str]:
        """List every public chapter of a series, first chapter first.

        Args:
            url: A series URL.

        Returns:
            One episode URL per listed chapter, oldest first.

        Raises:
            UnsupportedUrlError: The URL is not a series page.
            NotAnEpisodePageError: The site knows no such series, or it lists no chapter.
        """
        match = _SERIES_PATH.match(urlparse(url).path)
        if match is None:
            msg = f"{url} is not a series page."
            raise UnsupportedUrlError(msg)
        tag = match["tag"]
        urls: list[str] = []
        # The site lists the newest chapter first.
        for chapter in reversed(self._listing(tag, url)["chapters"]):
            candidate = episode_url(tag, int(chapter["id"]))
            if candidate not in urls:
                urls.append(candidate)
        if not urls:
            msg = f"the series at {url} lists no episode."
            raise NotAnEpisodePageError(msg)
        return urls

    def episode(self, url: str) -> Episode:
        """Read one chapter and list its pages.

        `/viewer` hands the pages over; `/title` names the series and the
        chapter and says which chapter follows. A chapter whose public window
        has closed still answers, with no pages and no name.

        Args:
            url: The episode URL.

        Returns:
            The episode. `pages` is empty when the chapter is not readable.

        Raises:
            UnsupportedUrlError: The URL is not an episode URL.
            NotAnEpisodePageError: The token is not a chapter id, or the site knows no such chapter.
        """
        match = _EPISODE_PATH.match(urlparse(url).path)
        if match is None:
            msg = f"{url} is not an episode page."
            raise UnsupportedUrlError(msg)
        token = match["token"]
        try:
            chapter_id = int(token) if token.isdecimal() else decrypt_chapter_id(token)
        except ValueError as error:
            msg = f"{url} names no chapter: {error}"
            raise NotAnEpisodePageError(msg) from error

        viewer = self._viewer(chapter_id, url)
        if viewer is None:
            msg = f"no chapter {chapter_id} on {BASE_URL}."
            raise NotAnEpisodePageError(msg)
        tag = str(viewer.get("titleTag") or match["tag"])
        listing = self._listing(tag, url)
        chapters: list[dict[str, Any]] = listing["chapters"]
        index = next((i for i, chapter in enumerate(chapters) if chapter["id"] == chapter_id), None)

        chapter = chapters[index] if index is not None else {}
        # Newest first, so the chapter that follows is the one listed before, and the other way round.
        after, before = neighbours(chapters, chapter) if index is not None else (None, None)
        prev_url = episode_url(tag, int(before["id"])) if before else None
        next_url = episode_url(tag, int(after["id"])) if after else None
        pages: tuple[Page, ...] = ()
        if viewer["status"] == _STATUS_SUCCESS:
            pages = tuple(Page(url=str(src)) for src in viewer["pages"])
        return Episode(
            url=episode_url(tag, chapter_id),
            series_title=str(listing["title"].get("name") or tag),
            episode_title=str(viewer["viewerTitle"] or chapter.get("name") or chapter_id),
            pages=pages,
            prev_url=prev_url,
            next_url=next_url,
            metadata={"title": listing["title"], "chapter": chapter, "viewer": viewer},
        )

    def _listing(self, tag: str, referer: str) -> dict[str, Any]:
        """The series as `/title` describes it, fetched once.

        Raises:
            NotAnEpisodePageError: The site knows no such series (it answers 501).
        """
        if tag not in self._listings:
            res = self._session.get(
                f"{API_URL}/title?tag={quote(tag, safe='')}",
                headers={**self.HEADERS, **_API_HEADERS, "Referer": referer},
                timeout=self.TIMEOUT,
            )
            if res.status_code == HTTPStatus.NOT_IMPLEMENTED:
                msg = f"no series {tag!r} on {BASE_URL}."
                raise NotAnEpisodePageError(msg)
            res.raise_for_status()
            self._listings[tag] = decode_title_view(res.content)
        return self._listings[tag]

    def _viewer(self, chapter_id: int, referer: str) -> dict[str, Any] | None:
        """The `/viewer` answer for a chapter, or None when the site knows no such chapter.

        The viewer POSTs, with nothing in the body; a GET is refused. An id
        the site does not know is a 500 rather than a `CONTENT_NOT_FOUND`.
        """
        res = self._session.post(
            f"{API_URL}/viewer?chapter_id={chapter_id}",
            headers={**self.HEADERS, **_API_HEADERS, "Referer": referer},
            timeout=self.TIMEOUT,
        )
        if res.status_code == HTTPStatus.INTERNAL_SERVER_ERROR:
            return None
        res.raise_for_status()
        return decode_viewer(res.content)
