"""ニコニコ漫画 (niconico manga), read through the app API, whose DRM pages are XORed with a key off their URL."""

from __future__ import annotations

import re
from http import HTTPStatus
from io import BytesIO
from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import urlparse

from PIL import Image

from getjmanga.cipher import xor_unmask
from getjmanga.errors import NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractor import Episode, Extractor, Page

if TYPE_CHECKING:
    from requests import Session

BASE_URL = "https://manga.nicovideo.jp"
#: The backend the niconico manga app and `sp.manga.nicovideo.jp` read from.
#: Unlike the web viewer, which shows a sign-in wall to every visitor, it
#: hands over every free episode to anyone.
API_URL = "https://api.nicomanga.jp/api/v1/app/manga"

# `/watch/mg<episode>`, the episode id the API takes without its `mg`.
_EPISODE_PATH = re.compile(r"^/watch/mg(?P<id>\d+)/?$")
# `/comic/<content>`, the work page listing the episodes.
_SERIES_PATH = re.compile(r"^/comic/(?P<id>\d+)/?$")

#: How many hex digits of a `drm_hash` make the XOR key.
_KEY_HEX_DIGITS = 16


def drm_key(drm_hash: str) -> str:
    """The XOR key a frame's `drm_hash` names.

    A `drm_hash` looks like `4dd530fbba62f25d12d39afeac63cb7d4a30f0fe_10357`:
    a 40-digit hex hash and a numeric suffix, which also make the directory
    of the frame on `drm.cdn.nicomanga.jp`. Only the first eight bytes of the
    hash are the key.

    Args:
        drm_hash: The frame's `drm_hash`.

    Returns:
        The key, 16 hex digits.
    """
    return drm_hash[:_KEY_HEX_DIGITS]


def unmask(data: bytes, drm_hash: str) -> bytes:
    """Undo the DRM masking of a page file.

    `drm.cdn.nicomanga.jp` serves a frame as `application/octet-stream`
    whose bytes are all XORed with `drm_key()` repeated from the first byte
    on -- the same scheme ComicWalker's viewer uses with its `drmHash`, so
    that port does the work. A frame served off `deliver.cdn.nicomanga.jp`
    (every user-posted work) carries a null `drm_hash` and never comes here.

    Args:
        data: The file exactly as the CDN serves it.
        drm_hash: The frame's `drm_hash`.

    Returns:
        The WebP (or JPEG) file.
    """
    return xor_unmask(data, drm_key(drm_hash))


def episode_url(episode_id: str | int) -> str:
    """The canonical URL of an episode.

    Args:
        episode_id: The episode id, `/watch/mg<id>`.

    Returns:
        The episode URL on `manga.nicovideo.jp`.
    """
    return f"{BASE_URL}/watch/mg{episode_id}"


class NicoManga(Extractor):
    """Fetch episodes from ニコニコ漫画."""

    NAME = "nicomanga"
    # `seiga.nicovideo.jp` is the old home of the same pages and redirects to
    # `manga.nicovideo.jp`; `sp.` is the mobile front end of the same API.
    HOSTS = ("manga.nicovideo.jp", "seiga.nicovideo.jp", "sp.manga.nicovideo.jp")
    URL_FORMS = (
        "https://manga.nicovideo.jp/watch/mg<episode>",
        "https://manga.nicovideo.jp/comic/<content>",
    )
    HEADERS: ClassVar[dict[str, str]] = {
        **Extractor.HEADERS,
        "Accept": "application/json",
        "Origin": BASE_URL,
    }

    def __init__(self, session: Session | None = None) -> None:
        """Build an extractor.

        Args:
            session: A session to reuse. A retrying one is made when omitted.
        """
        super().__init__(session)
        # The work and its episode list answer per series; a bulk run asks for
        # the same series once per episode, so both are kept.
        self._contents: dict[str, dict[str, Any]] = {}
        self._listings: dict[str, list[dict[str, Any]]] = {}

    @classmethod
    def suitable(cls, url: str) -> bool:
        """Report whether the extractor takes `url`.

        Args:
            url: The URL to check.

        Returns:
            True for an episode or a work page on a known host.
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
            True for `/comic/<content>`.
        """
        return _SERIES_PATH.match(urlparse(url).path) is not None

    def series_urls(self, url: str) -> list[str]:
        """List every episode of a work, first episode first.

        Args:
            url: A work URL.

        Returns:
            One episode URL per listed episode, in the order the API lists them.

        Raises:
            UnsupportedUrlError: The URL is not a work page.
            NotAnEpisodePageError: The work lists no episode.
        """
        match = _SERIES_PATH.match(urlparse(url).path)
        if match is None:
            msg = f"{url} is not a series page."
            raise UnsupportedUrlError(msg)
        urls: list[str] = []
        for entry in self._listing(match["id"], url):
            candidate = episode_url(entry["id"])
            if candidate not in urls:
                urls.append(candidate)
        if not urls:
            msg = f"the series at {url} lists no episode."
            raise NotAnEpisodePageError(msg)
        return urls

    def episode(self, url: str) -> Episode:
        """Read one episode and list its pages.

        The API describes the episode, its work names the series and the
        work's episode list names the one after it; `frames` hands the page
        files over, or answers 403 (`NOT_PURCHASED`, `PUBLICATION_FINISHED`)
        when the episode wants a coin or is no longer served. An episode the
        API refuses to describe at all (a finished one) is read off the
        work's listing when a bulk run cached it, and is locked either way.

        Args:
            url: The episode URL.

        Returns:
            The episode. `pages` is empty when it is not readable.

        Raises:
            UnsupportedUrlError: The URL is not an episode URL.
            NotAnEpisodePageError: The API knows no such episode.
        """
        match = _EPISODE_PATH.match(urlparse(url).path)
        if match is None:
            msg = f"{url} is not an episode page."
            raise UnsupportedUrlError(msg)
        episode_id = match["id"]

        info, refused = self._api(f"episodes/{episode_id}", url)
        meta: dict[str, Any] | None = info.get("meta") if isinstance(info, dict) else None
        if meta is None:
            if refused is None or refused == "NOT_FOUND" or refused.startswith("HTTP "):
                msg = f"no episode mg{episode_id} on {BASE_URL} ({refused or 'no description'})."
                raise NotAnEpisodePageError(msg)
            # Refused outright (`PUBLICATION_FINISHED`): locked, named by the
            # work's listing when a bulk run walked in from an earlier episode.
            meta = self._cached_entry(episode_id) or {}
        content_id = str(meta.get("content_id") or "")

        content = self._content(content_id, url) if content_id else {}
        entries = self._listing(content_id, url) if content_id else []
        index = next((i for i, entry in enumerate(entries) if str(entry.get("id")) == episode_id), None)
        next_url = None
        if index is not None and index + 1 < len(entries):
            next_url = episode_url(entries[index + 1]["id"])

        frames: list[Any] | None = None
        if refused is None:
            result, refused = self._api(f"episodes/{episode_id}/frames", url, params={"enable_webp": "true"})
            frames = result if isinstance(result, list) else None
        pages = tuple(
            Page(
                url=str(frame["meta"]["source_url"]),
                width=int(frame["meta"].get("width") or 0),
                height=int(frame["meta"].get("height") or 0),
                extra={"drm_hash": frame["meta"].get("drm_hash")},
            )
            for frame in frames or []
            if isinstance(frame, dict) and isinstance(frame.get("meta"), dict) and frame["meta"].get("source_url")
        )
        return Episode(
            url=episode_url(episode_id),
            series_title=str(content.get("title") or content_id or f"mg{episode_id}"),
            episode_title=str(meta.get("title") or f"mg{episode_id}"),
            pages=pages,
            next_url=next_url,
            metadata={
                "episode": info if isinstance(info, dict) else {"meta": meta},
                "content": content,
                "frames": frames,
                "error_code": refused,
            },
        )

    def image(self, page: Page, episode: Episode) -> Image.Image:
        """Fetch one page and unmask it when the CDN served it masked.

        Args:
            page: The page to fetch.
            episode: The episode the page belongs to.

        Returns:
            The page image.
        """
        res = self._get(
            page.url,
            headers={**Extractor.HEADERS, "Accept": "image/webp,image/*,*/*;q=0.8", "Referer": episode.url},
            timeout=self.IMAGE_TIMEOUT,
        )
        data = res.content
        drm_hash = page.extra.get("drm_hash")
        if drm_hash:
            data = unmask(data, str(drm_hash))
        return Image.open(BytesIO(data))

    def _api(
        self, path: str, referer: str, *, params: dict[str, str] | None = None
    ) -> tuple[dict[str, Any] | list[Any] | None, str | None]:
        """GET one API path and unwrap its `data.result`.

        The API wraps everything as `{"meta": {"status": ...}, "data": {"result": ...}}`
        and answers a refusal (403 for a coin or a finished run, 404 for an
        unknown id) in the same shape, with `data` null and an `error_code` in
        `meta`.

        Returns:
            `data.result` and None, or None and the `error_code` (or the HTTP
            status when the answer names none).

        Raises:
            HTTPError: The API failed on its side (5xx).
        """
        res = self._session.get(
            f"{API_URL}/{path}",
            params=params,
            headers={**self.HEADERS, "Referer": referer},
            timeout=self.TIMEOUT,
        )
        if res.status_code >= HTTPStatus.INTERNAL_SERVER_ERROR:
            res.raise_for_status()
        try:
            body = res.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            return None, f"HTTP {res.status_code}"
        data = body.get("data")
        if res.status_code == HTTPStatus.OK and isinstance(data, dict) and "result" in data:
            return data["result"], None
        meta = body.get("meta")
        code = meta.get("error_code") if isinstance(meta, dict) else None
        return None, str(code) if code else f"HTTP {res.status_code}"

    def _cached_entry(self, episode_id: str) -> dict[str, Any] | None:
        """The episode's `meta` from a work listing already fetched, if any."""
        for content_id, entries in self._listings.items():
            for entry in entries:
                if str(entry.get("id")) == episode_id:
                    return {**(entry.get("meta") or {}), "content_id": content_id}
        return None

    def _content(self, content_id: str, referer: str) -> dict[str, Any]:
        """The work as `contents/<id>` describes it, fetched once; `{}` when unknown."""
        if content_id not in self._contents:
            result, _ = self._api(f"contents/{content_id}", referer)
            meta = result.get("meta") if isinstance(result, dict) else None
            self._contents[content_id] = meta if isinstance(meta, dict) else {}
        return self._contents[content_id]

    def _listing(self, content_id: str, referer: str) -> list[dict[str, Any]]:
        """The work's episodes as `contents/<id>/episodes` lists them, fetched once."""
        if content_id not in self._listings:
            result, _ = self._api(f"contents/{content_id}/episodes", referer)
            self._listings[content_id] = [
                entry
                for entry in (result if isinstance(result, list) else [])
                if isinstance(entry, dict) and entry.get("id") is not None
            ]
        return self._listings[content_id]
