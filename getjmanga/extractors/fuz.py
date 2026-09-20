"""COMIC FUZ (芳文社), whose viewer talks protobuf and serves AES-encrypted pages."""

from __future__ import annotations

import re
from dataclasses import dataclass
from http import HTTPStatus
from io import BytesIO
from typing import TYPE_CHECKING, ClassVar
from urllib.parse import urlparse

from PIL import Image

from getjmanga.cipher import aes_cbc_decrypt
from getjmanga.errors import LoginError, NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractor import Episode, Extractor, Page, neighbours
from getjmanga.protobuf import encode_bytes_field, encode_varint_field, integer, message, messages, raw, string

if TYPE_CHECKING:
    from collections.abc import Iterator

    from httpx import Client

BASE_URL = "https://comic-fuz.com"
API_URL = "https://api.comic-fuz.com"
IMAGE_URL = "https://img.comic-fuz.com"

_CHAPTER_PATH = re.compile(r"^/manga/viewer/(?P<id>\d+)/?$")
_MANGA_PATH = re.compile(r"^/manga/(?P<id>\d+)/?$")

# ---------------------------------------------------------------------------
# The API speaks protobuf; see `getjmanga.protobuf`. Field numbers come from
# the site's generated `WebMangaViewer2Request` / `WebMangaViewer2Response`.
# ---------------------------------------------------------------------------

#: `DeviceInfo.DeviceType.BROWSER`; every request carries `deviceInfo` with it.
_DEVICE_TYPE_BROWSER = 2
#: `ChapterArgument.Position`: which chapter of a manga to open.
_POSITION_DETAIL = 2


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Chapter:
    """One row of a manga's chapter list."""

    id: int
    title: str
    #: Points the chapter costs; 0 means free to read.
    points: int = 0

    @property
    def url(self) -> str:
        """The viewer URL of the chapter."""
        return f"{BASE_URL}/manga/viewer/{self.id}"


class Fuz(Extractor):
    """Fetch chapters from COMIC FUZ."""

    NAME = "fuz"
    HOSTS = ("comic-fuz.com",)
    URL_FORMS = (
        "https://comic-fuz.com/manga/viewer/<chapter-id>",
        "https://comic-fuz.com/manga/<manga-id>",
    )
    CONFIG_KEY = "comic-fuz"
    HEADERS: ClassVar[dict[str, str]] = {**Extractor.HEADERS, "Origin": BASE_URL, "Referer": f"{BASE_URL}/"}

    def __init__(self, session: Client | None = None) -> None:
        """Build an extractor.

        Args:
            session: A session to reuse. A retrying one is made when omitted.
        """
        super().__init__(session)
        # Every viewer response carries the manga's whole chapter list, which
        # is what a locked chapter (the API answers 401 or 402, nothing else)
        # is named and given its next chapter from.
        self._chapters: dict[int, tuple[str, list[Chapter]]] = {}

    @classmethod
    def suitable(cls, url: str) -> bool:
        """Report whether `url` is a chapter viewer or a manga page.

        Args:
            url: The URL to check.

        Returns:
            True for `/manga/viewer/<id>` and `/manga/<id>` on comic-fuz.com.
        """
        if not super().suitable(url):
            return False
        path = urlparse(url).path
        return bool(_CHAPTER_PATH.match(path) or _MANGA_PATH.match(path))

    @classmethod
    def is_series(cls, url: str) -> bool:
        """Report whether `url` is a manga page.

        Args:
            url: The URL to check.

        Returns:
            True for `/manga/<id>`.
        """
        return cls.suitable(url) and _MANGA_PATH.match(urlparse(url).path) is not None

    def series_urls(self, url: str) -> list[str]:
        """List every chapter of a manga, oldest first.

        Args:
            url: A `/manga/<id>` URL.

        Returns:
            The viewer URL of every chapter, locked ones included.

        Raises:
            UnsupportedUrlError: The URL is no manga page.
            NotAnEpisodePageError: The manga lists no chapter.
        """
        match = _MANGA_PATH.match(urlparse(url).path)
        if match is None:
            msg = f"{url} is not a manga page."
            raise UnsupportedUrlError(msg)
        manga_id = int(match["id"])
        # `DETAIL` opens whatever chapter the manga page would, which does not
        # matter here: the chapter list comes along with any of them.
        argument = encode_varint_field(1, manga_id) + encode_varint_field(2, _POSITION_DETAIL)
        res = self._viewer(encode_bytes_field(5, argument))
        if res is None:
            msg = f"the manga at {url} opens no chapter."
            raise NotAnEpisodePageError(msg)
        _, _, chapters = self._remember(res)
        if not chapters:
            msg = f"the manga at {url} lists no chapter."
            raise NotAnEpisodePageError(msg)
        return [chapter.url for chapter in chapters]

    def episode(self, url: str) -> Episode:
        """Read one chapter: its pages, with the key each one is encrypted under.

        Args:
            url: A `/manga/viewer/<id>` URL.

        Returns:
            The chapter. `pages` is empty when it wants points or a login.

        Raises:
            UnsupportedUrlError: The URL is no chapter viewer.
        """
        match = _CHAPTER_PATH.match(urlparse(url).path)
        if match is None:
            msg = f"{url} is not a chapter viewer url."
            raise UnsupportedUrlError(msg)
        chapter_id = int(match["id"])

        res = self._viewer(encode_varint_field(4, chapter_id))
        if res is None:
            return self._locked(url, chapter_id)

        manga_id, series_title, chapters = self._remember(res)
        data = message(raw(res, 2))
        return Episode(
            url=url,
            series_title=series_title or str(manga_id),
            episode_title=string(data, 1) or str(chapter_id),
            pages=tuple(self._pages(data)),
            prev_url=self._neighbours(chapters, chapter_id)[0],
            next_url=self._neighbours(chapters, chapter_id)[1],
            metadata={
                "chapter_id": chapter_id,
                "manga_id": manga_id,
                "chapters": [{"id": c.id, "title": c.title, "points": c.points} for c in chapters],
            },
        )

    def image(self, page: Page, episode: Episode) -> Image.Image:
        """Fetch one page and decrypt it.

        The CDN only serves a page to a session that holds the `fuz_image_token`
        cookie the viewer API set, which the shared session does by now.

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

    def login(self, url: str, username: str, password: str) -> None:  # noqa: ARG002 (one sign-in endpoint)
        """Sign in, so chapters the account has unlocked become readable.

        Args:
            url: Ignored; COMIC FUZ has one sign-in endpoint.
            username: The email address the account uses.
            password: The account's password.

        Raises:
            LoginError: COMIC FUZ refused the credentials.
        """
        body = self._device_info() + encode_bytes_field(2, username) + encode_bytes_field(3, password)
        res = self._session.post(f"{API_URL}/v1/sign_in", content=body, headers=self.HEADERS, timeout=self.TIMEOUT)
        res.raise_for_status()
        answer = message(res.content)
        if not integer(answer, 1):
            msg = f"comic-fuz.com refused the credentials for {username!r}: {string(answer, 2) or 'no reason given'}."
            raise LoginError(msg)

    @staticmethod
    def _device_info() -> bytes:
        return encode_bytes_field(1, encode_varint_field(3, _DEVICE_TYPE_BROWSER))

    def _viewer(self, selector: bytes) -> dict[int, list[int | bytes]] | None:
        """Call `web_manga_viewer_2`; None when the chapter is locked."""
        res = self._session.post(
            f"{API_URL}/v1/web_manga_viewer_2",
            content=self._device_info() + selector,
            headers=self.HEADERS,
            timeout=self.TIMEOUT,
        )
        # 401 wants a login, 402 wants points: locked either way.
        if res.status_code in (HTTPStatus.UNAUTHORIZED, HTTPStatus.PAYMENT_REQUIRED):
            return None
        res.raise_for_status()
        return message(res.content)

    def _remember(self, res: dict[int, list[int | bytes]]) -> tuple[int, str, list[Chapter]]:
        """Read the manga's id, name and chapter list off a viewer response, and keep them."""
        manga = message(raw(res, 11))
        manga_id, series_title = integer(manga, 1), string(manga, 2)
        chapters: list[Chapter] = []
        # Groups and the chapters inside them are listed newest first.
        for group in messages(res, 5):
            for chapter in messages(message(group), 2):
                fields = message(chapter)
                title = " ".join(part for part in (string(fields, 2), string(fields, 3)) if part)
                chapters.append(
                    Chapter(integer(fields, 1), title or str(integer(fields, 1)), integer(message(raw(fields, 5)), 2))
                )
        chapters.reverse()
        if manga_id:
            self._chapters[manga_id] = (series_title, chapters)
        return manga_id, series_title, chapters

    def _locked(self, url: str, chapter_id: int) -> Episode:
        """Describe a chapter the API would not open, from a chapter list seen earlier."""
        for manga_id, (series_title, chapters) in self._chapters.items():
            if any(chapter.id == chapter_id for chapter in chapters):
                title = next(chapter.title for chapter in chapters if chapter.id == chapter_id)
                return Episode(
                    url=url,
                    series_title=series_title,
                    episode_title=title,
                    prev_url=self._neighbours(chapters, chapter_id)[0],
                    next_url=self._neighbours(chapters, chapter_id)[1],
                    metadata={"chapter_id": chapter_id, "manga_id": manga_id},
                )
        return Episode(url=url, series_title=str(chapter_id), episode_title=str(chapter_id))

    @staticmethod
    def _neighbours(chapters: list[Chapter], chapter_id: int) -> tuple[str | None, str | None]:
        """The URLs of the chapters either side of `chapter_id`, None at either end."""
        chapter = next((chapter for chapter in chapters if chapter.id == chapter_id), None)
        if chapter is None:
            return None, None
        before, after = neighbours(chapters, chapter)
        return before.url if before else None, after.url if after else None

    @staticmethod
    def _pages(data: dict[int, list[int | bytes]]) -> Iterator[Page]:
        """The image pages of a `ViewerData`, in reading order.

        Pages other than images -- ads, the last-page card -- are skipped, and
        so is an `isExtraPage` image, which is a promo slotted in by the site.
        """
        for page in messages(data, 2):
            image = message(raw(message(page), 1))
            if not image or integer(image, 7):
                continue
            yield Page(
                url=IMAGE_URL + string(image, 1),
                width=integer(image, 5),
                height=integer(image, 6),
                extra={"key": string(image, 4), "iv": string(image, 3)},
            )
