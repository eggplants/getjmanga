"""comico (NHN comico), whose Nuxt app reads everything off `api.comico.jp` and hides page URLs under AES."""

from __future__ import annotations

import base64
import hashlib
import posixpath
import re
import time
from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from getjmanga.errors import GetjmangaError, NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractor import Episode, Extractor, Page, neighbours

if TYPE_CHECKING:
    from collections.abc import Iterator

    from httpx import Client

BASE_URL = "https://www.comico.jp"
API_URL = "https://api.comico.jp"

# `/comic/<content>/chapter/<chapter>/product` (or `/trial` for a preview of a paid
# chapter); `magazine_comic` is the imprint of page-flip books served as EPUB.
_EPISODE_PATH = re.compile(
    r"^/(?P<type>comic|magazine_comic)/(?P<content>\d+)/chapter/(?P<chapter>\d+)/(?P<sales>product|trial)/?$"
)
_SERIES_PATH = re.compile(r"^/(?P<type>comic|magazine_comic)/(?P<content>\d+)/?$")

#: Every API request is signed with `sha256(salt + client ip + unix time)`; the salt
#: is a constant of the site's bundle. The app's server side signs with `0.0.0.0`
#: as the ip, and so does this.
_CHECKSUM_SALT = "9241d2f090d01716feac20ae08ba791a"
_CLIENT_IP = "0.0.0.0"  # noqa: S104 (what the app's SSR sends as its ip, not a bind address)
#: Page and EPUB URLs come AES-256-CBC encrypted (zero iv, PKCS#7) under this key.
_URL_KEY = b"a7fc9dc89f2c873d79397f8a0028a4cd"

_OPF = "{http://www.idpf.org/2007/opf}"


def api_headers(timestamp: int | None = None) -> dict[str, str]:
    """The headers `api.comico.jp` insists on, signed for now.

    Args:
        timestamp: Unix seconds to sign with; the current time when omitted.

    Returns:
        The `X-comico-*` headers the app sends with every request.
    """
    now = str(int(time.time()) if timestamp is None else timestamp)
    return {
        "X-comico-client-os": "other",
        "X-comico-client-store": "other",
        "X-comico-client-platform": "web",
        "X-comico-client-immutable-uid": _CLIENT_IP,
        "X-comico-client-uid": _CLIENT_IP,
        "X-comico-client-accept-mature": "",
        "X-comico-request-time": now,
        "X-comico-check-sum": hashlib.sha256((_CHECKSUM_SALT + _CLIENT_IP + now).encode()).hexdigest(),
        "X-comico-timezone-id": "Asia/Tokyo",
        "Accept-Language": "ja-JP",
        "Origin": BASE_URL,
    }


def decrypt_url(token: str) -> str:
    """Undo the AES the API wraps page and EPUB URLs in.

    Args:
        token: The `url` field of an image or an EPUB, base64.

    Returns:
        The plain URL.

    Raises:
        GetjmangaError: The token is no whole number of blocks, or the padding is off.
    """
    data = base64.b64decode(token)
    if not data or len(data) % 16:
        msg = "the encrypted url is not a whole number of AES blocks."
        raise GetjmangaError(msg)
    decryptor = Cipher(algorithms.AES(_URL_KEY), modes.CBC(bytes(16))).decryptor()
    plain = decryptor.update(data) + decryptor.finalize()
    padding = plain[-1]
    if not 1 <= padding <= 16 or plain[-padding:] != bytes([padding]) * padding:  # noqa: PLR2004 (PKCS#7 block)
        msg = "the decrypted url carries no PKCS#7 padding; wrong key?"
        raise GetjmangaError(msg)
    return plain[:-padding].decode()


def episode_url(content_type: str, content_id: str | int, chapter_id: str | int, sales_type: str = "product") -> str:
    """The canonical URL of a chapter.

    Args:
        content_type: `comic` or `magazine_comic`.
        content_id: The work's id.
        chapter_id: The chapter's id, which counts from 1 within the work.
        sales_type: `product` for the chapter itself, `trial` for its preview.

    Returns:
        The viewer URL.
    """
    return f"{BASE_URL}/{content_type}/{content_id}/chapter/{chapter_id}/{sales_type}"


def episode_title(name: str, sales_type: str) -> str:
    """Name a chapter the way the site labels it.

    Args:
        name: The chapter's name.
        sales_type: `product` or `trial`.

    Returns:
        The name, with 試し読み after it for a trial so it does not shadow the chapter itself.
    """
    return f"{name} 試し読み" if sales_type == "trial" else name


def epub_spine(opf: str) -> list[tuple[str, bool]]:
    """Read the pages out of an EPUB package document, in reading order.

    Args:
        opf: The `.opf` file.

    Returns:
        One `(href, is_image)` per linear spine item. A page is an image when
        the item, or the fallback it names, is one; otherwise `href` is the
        XHTML page that embeds it.
    """
    root = ET.fromstring(opf)  # noqa: S314 (the site's own package document)
    items: dict[str, tuple[str, str, str]] = {}
    for item in root.iter(f"{_OPF}item"):
        items[item.get("id", "")] = (item.get("href", ""), item.get("media-type", ""), item.get("fallback", ""))
    pages: list[tuple[str, bool]] = []
    for ref in root.iter(f"{_OPF}itemref"):
        if ref.get("linear") == "no":
            continue
        href, media_type, fallback = items.get(ref.get("idref", ""), ("", "", ""))
        if not href:
            continue
        if _is_image(media_type):
            pages.append((href, True))
            continue
        fallback_href, fallback_type, _ = items.get(fallback, ("", "", ""))
        if fallback_href and _is_image(fallback_type):
            pages.append((fallback_href, True))
        else:
            pages.append((href, False))
    return pages


def image_in_xhtml(xhtml: str, page_href: str) -> str | None:
    """Find the one image a fixed-layout EPUB page embeds.

    Args:
        xhtml: The page document.
        page_href: The page's path inside the EPUB, to resolve the image's against.

    Returns:
        The image's path inside the EPUB, or None when the page embeds none.
    """
    match = re.search(r"<(?:svg:)?image\b[^>]*?(?:xlink:)?href=\"([^\"]+)\"|<img\b[^>]*?src=\"([^\"]+)\"", xhtml)
    if match is None:
        return None
    ref = match.group(1) or match.group(2)
    return posixpath.normpath(posixpath.join(posixpath.dirname(page_href), ref))


def _is_image(media_type: str) -> bool:
    return "image" in media_type and "svg" not in media_type


class Comico(Extractor):
    """Fetch chapters from comico."""

    NAME = "comico"
    HOSTS = ("www.comico.jp",)
    PUBLISHER = "NHN comico"
    URL_FORMS = (
        "https://www.comico.jp/comic/<content>/chapter/<chapter>/product",
        "https://www.comico.jp/comic/<content>/chapter/<chapter>/trial",
        "https://www.comico.jp/comic/<content>",
        "https://www.comico.jp/magazine_comic/<content>/chapter/<chapter>/product",
        "https://www.comico.jp/magazine_comic/<content>",
    )
    HEADERS: ClassVar[dict[str, str]] = {**Extractor.HEADERS, "Referer": f"{BASE_URL}/"}

    def __init__(self, session: Client | None = None) -> None:
        """Build an extractor.

        Args:
            session: A session to reuse. A retrying one is made when omitted.
        """
        super().__init__(session)
        # The work page names every chapter, which is what a chapter the API
        # will not open at all is named and given its next chapter from.
        self._listings: dict[tuple[str, str], dict[str, Any]] = {}

    @classmethod
    def suitable(cls, url: str) -> bool:
        """Report whether `url` is a chapter viewer or a work page.

        Args:
            url: The URL to check.

        Returns:
            True for `/<type>/<content>/chapter/<chapter>/<product|trial>` and `/<type>/<content>`.
        """
        if not super().suitable(url):
            return False
        path = urlparse(url).path
        return bool(_EPISODE_PATH.match(path) or _SERIES_PATH.match(path))

    @classmethod
    def is_series(cls, url: str) -> bool:
        """Report whether `url` is a work page.

        Args:
            url: The URL to check.

        Returns:
            True for `/comic/<content>` and `/magazine_comic/<content>`.
        """
        return cls.suitable(url) and _SERIES_PATH.match(urlparse(url).path) is not None

    def series_urls(self, url: str) -> list[str]:
        """List every chapter of a work, first chapter first.

        A work sold by the episode lists its episodes; one sold only by the
        volume lists its volumes.

        Args:
            url: A work page URL.

        Returns:
            The viewer URL of every chapter, locked ones included.

        Raises:
            UnsupportedUrlError: The URL is no work page.
            NotAnEpisodePageError: The work lists no chapter.
        """
        match = _SERIES_PATH.match(urlparse(url).path)
        if match is None:
            msg = f"{url} is not a work page."
            raise UnsupportedUrlError(msg)
        content_type, content_id = match["type"], match["content"]
        urls: list[str] = []
        for chapter in self._chapters(content_type, content_id, url):
            candidate = episode_url(content_type, content_id, chapter["id"])
            if candidate not in urls:
                urls.append(candidate)
        if not urls:
            msg = f"the work at {url} lists no chapter."
            raise NotAnEpisodePageError(msg)
        return urls

    def episode(self, url: str) -> Episode:
        """Read one chapter: its titles, its pages and the chapter after it.

        The chapter endpoint answers 200 for a locked chapter too, only
        without the `images` (or `epub`) the pages come from.

        Args:
            url: A chapter viewer URL.

        Returns:
            The chapter. `pages` is empty when it wants coins, a ticket or a login.

        Raises:
            UnsupportedUrlError: The URL is no chapter viewer.
            NotAnEpisodePageError: The site knows no such chapter.
        """
        match = _EPISODE_PATH.match(urlparse(url).path)
        if match is None:
            msg = f"{url} is not a chapter viewer url."
            raise UnsupportedUrlError(msg)
        content_type, content_id, chapter_id, sales_type = match.group("type", "content", "chapter", "sales")
        canonical = episode_url(content_type, content_id, chapter_id, sales_type)

        body = self._api(f"/{content_type}/{content_id}/chapter/{chapter_id}/{sales_type}", canonical)
        result, data = body.get("result") or {}, body.get("data") or {}
        if result.get("code") != 200 or not isinstance(data.get("chapter"), dict):  # noqa: PLR2004 (the site's OK)
            return self._locked(content_type, content_id, chapter_id, sales_type, str(result.get("message") or ""))

        content: dict[str, Any] = data.get("content") or {}
        chapter: dict[str, Any] = data["chapter"]
        prev_chapter = chapter.get("previousChapter") or {}
        next_chapter = chapter.get("nextChapter") or {}
        return Episode(
            url=canonical,
            series_title=str(content.get("name") or content_id),
            episode_title=episode_title(str(chapter.get("name") or chapter_id), sales_type),
            pages=tuple(self._pages(chapter, canonical)),
            prev_url=(
                episode_url(content_type, content_id, prev_chapter["id"], sales_type)
                if prev_chapter.get("id")
                else None
            ),
            next_url=(
                episode_url(content_type, content_id, next_chapter["id"], sales_type)
                if next_chapter.get("id")
                else None
            ),
            metadata={"content": content, "chapter": chapter},
            writer=_authors(content),
            publisher=str(content.get("publisherName") or "") or self.PUBLISHER,
        )

    def _pages(self, chapter: dict[str, Any], referer: str) -> Iterator[Page]:
        """The pages of a chapter the API opened: crops of a scroll, or the files of an EPUB."""
        images = chapter.get("images")
        if isinstance(images, list):
            for image in sorted(images, key=lambda image: int(image.get("sort") or 0)):
                if not image.get("url"):
                    continue
                parameter = str(image.get("parameter") or "")
                yield Page(
                    url=decrypt_url(str(image["url"])) + (f"?{parameter}" if parameter else ""),
                    width=int(image.get("width") or 0),
                    height=int(image.get("height") or 0),
                )
            return
        epub = chapter.get("epub")
        if isinstance(epub, dict):
            yield from self._epub_pages(epub, referer)

    def _epub_pages(self, epub: dict[str, Any], referer: str) -> Iterator[Page]:
        """The pages of an EPUB chapter, read off the package document the CDN unzips for the viewer."""
        included = epub.get("chapterEpubIncludedFile") or {}
        if not included.get("url"):
            return
        root = decrypt_url(str(included["url"])) + str(included.get("rootPath") or "")
        parameter = str(included.get("parameter") or "")
        query = f"?{parameter}" if parameter else ""
        optimize = str((included.get("m2Parameter") or epub.get("m2Parameter") or {}).get("optimize") or "")
        headers = {**self.HEADERS, "Referer": referer}
        opf = self._get(root + str(included.get("rootFileName") or "standard.opf") + query, headers=headers)
        for href, is_image in epub_spine(opf.text):
            image_href = href
            if not is_image:
                page = self._get(root + href + query, headers=headers)
                found = image_in_xhtml(page.text, href)
                if found is None:
                    continue
                image_href = found
            yield Page(url=root + image_href + optimize + query)

    def _locked(self, content_type: str, content_id: str, chapter_id: str, sales_type: str, reason: str) -> Episode:
        """Describe a chapter the API would not open, from the work's chapter list.

        Raises:
            NotAnEpisodePageError: The work does not list the chapter either.
        """
        canonical = episode_url(content_type, content_id, chapter_id, sales_type)
        chapters = self._chapters(content_type, content_id, canonical)
        position = next((i for i, chapter in enumerate(chapters) if str(chapter.get("id")) == chapter_id), None)
        if position is None:
            msg = f"no chapter {chapter_id} in the work at {canonical}: {reason or 'not listed'}."
            raise NotAnEpisodePageError(msg)
        listing = self._listings[content_type, content_id]
        before, after = neighbours(chapters, chapters[position])
        return Episode(
            url=canonical,
            series_title=str(listing.get("name") or content_id),
            episode_title=episode_title(str(chapters[position].get("name") or chapter_id), sales_type),
            prev_url=episode_url(content_type, content_id, before["id"], sales_type) if before else None,
            next_url=episode_url(content_type, content_id, after["id"], sales_type) if after else None,
            metadata={"chapter": chapters[position], "reason": reason},
            writer=_authors(listing),
            publisher=str(listing.get("publisherName") or "") or self.PUBLISHER,
        )

    def _chapters(self, content_type: str, content_id: str, referer: str) -> list[dict[str, Any]]:
        """The work's chapters in reading order, fetched once per work.

        Raises:
            NotAnEpisodePageError: The site knows no such work, or has stopped selling it.
        """
        key = (content_type, content_id)
        if key not in self._listings:
            body = self._api(f"/{content_type}/{content_id}", referer)
            result, data = body.get("result") or {}, body.get("data") or {}
            # A work sold by the episode fills `episode`, one sold by the volume `volume`; both may be there.
            content = next(
                (
                    tab.get("content")
                    for tab in (data.get("episode"), data.get("volume"))
                    if isinstance(tab, dict) and isinstance(tab.get("content"), dict)
                ),
                None,
            )
            if result.get("code") != 200 or content is None:  # noqa: PLR2004 (the site's OK)
                msg = f"no work {content_type}/{content_id} on {BASE_URL}: {result.get('message') or 'not listed'}."
                raise NotAnEpisodePageError(msg)
            self._listings[key] = content
        chapters = self._listings[key].get("chapters") or []
        return sorted((c for c in chapters if isinstance(c, dict) and c.get("id") is not None), key=_sort_key)

    def _api(self, path: str, referer: str) -> dict[str, Any]:
        """GET one API path, signed the way the app signs it."""
        res = self._get(f"{API_URL}{path}", headers={**self.HEADERS, **api_headers(), "Referer": referer})
        body = res.json()
        return body if isinstance(body, dict) else {}


#: What the API's author `role` codes mean; a plain `creator` needs no saying.
_ROLES = {"creator": "", "original_creator": "原作"}


def _authors(content: dict[str, Any]) -> str:
    """The work's `authors` in `sort` order, each with its role when it has one worth naming."""
    credited = []
    for author in sorted((a for a in content.get("authors") or [] if isinstance(a, dict)), key=_sort_key):
        name, role = str(author.get("name") or "").strip(), str(author.get("role") or "")
        role = _ROLES.get(role, role)
        if name:
            credited.append(f"{name} ({role})" if role else name)
    return ", ".join(credited)


def _sort_key(chapter: dict[str, Any]) -> int:
    return int(chapter.get("sort") or chapter.get("id") or 0)
