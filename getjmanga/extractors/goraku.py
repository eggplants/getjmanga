"""ゴラクうぇぶ! (日本文芸社), a Next.js site whose `da-viewer` decrypts AES-CBC pages fetched with an Akamai token."""

from __future__ import annotations

import json
import re
from http import HTTPStatus
from io import BytesIO
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin, urlparse

from PIL import Image

from getjmanga.cipher import aes_cbc_decrypt
from getjmanga.errors import NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractor import Episode, Extractor, Page

if TYPE_CHECKING:
    from collections.abc import Iterator

BASE_URL = "https://gorakuweb.com"

# `/episode/<title id>` is what the site links a work by: it renders the
# latest episode together with the whole episode list. `/episode/<title
# id>/<episode id>` is one episode.
_TITLE_PATH = re.compile(r"^/episode/(?P<title>\d+)/?$")
_EPISODE_PATH = re.compile(r"^/episode/(?P<title>\d+)/(?P<episode>\d+)/?$")

# The Next.js app streams its React Server Components payload into
# `self.__next_f.push([1, "<rows>"])` calls; joined, the rows are one per
# line as `<hex id>:<json>`. The episode component's props are one of them.
_FLIGHT_PUSH = re.compile(r"self\.__next_f\.push\((\[.*?\])\)\s*</script>", re.DOTALL)
_FLIGHT_ROW = re.compile(r"^[0-9a-f]+:(?P<json>[\[{].*)$", re.MULTILINE)
_UNDEFINED = "$undefined"

#: The props that make a flight row the episode component's.
_EPISODE_KEYS = frozenset({"titleId", "episodeId", "episodeList"})


def flight_rows(html: str) -> Iterator[Any]:
    """Yield every JSON row of the page's React flight payload.

    Args:
        html: The episode page.

    Yields:
        Each row that parses as JSON, in page order.
    """
    parts: list[str] = []
    for push in _FLIGHT_PUSH.findall(html):
        try:
            call = json.loads(push)
        except ValueError:
            continue
        if isinstance(call, list) and len(call) > 1 and isinstance(call[1], str):
            parts.append(call[1])
    for match in _FLIGHT_ROW.finditer("".join(parts)):
        try:
            yield json.loads(match.group("json"))
        except ValueError:
            continue


def _find_props(node: Any) -> dict[str, Any] | None:  # noqa: ANN401 (a parsed JSON tree)
    if isinstance(node, dict):
        if node.keys() >= _EPISODE_KEYS:
            return node
        node = list(node.values())
    if isinstance(node, list):
        for child in node:
            found = _find_props(child)
            if found is not None:
                return found
    return None


def _clean(value: Any) -> Any:  # noqa: ANN401 (a parsed JSON tree)
    """Turn the flight payload's `$undefined` markers into None, recursively."""
    if value == _UNDEFINED:
        return None
    if isinstance(value, dict):
        return {key: _clean(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_clean(item) for item in value]
    return value


def episode_props(html: str) -> dict[str, Any] | None:
    """Pick the episode component's props out of an episode page.

    Args:
        html: The episode page.

    Returns:
        The props (`titleId`, `episodeId`, `title`, `seriesTitle`, `base`,
        `accessKey`, `keyBytes`, `ivBytes`, `metadata`, `episodeList`, ...)
        with `$undefined` read as None, or None when the page holds none.
    """
    for row in flight_rows(html):
        props = _find_props(row)
        if props is not None:
            return _clean(props)
    return None


def page_url(base: str, filename: str, access_key: str) -> str:
    """The URL the viewer fetches one page file from.

    The Akamai token is appended verbatim: its `~`, `*` and `=` must not be
    percent-encoded, or the edge refuses the request.

    Args:
        base: The episode's `base`, a directory on the content CDN.
        filename: The page's `filename`.
        access_key: The episode's `accessKey`.

    Returns:
        The page file URL.
    """
    return f"{base}/{filename}?__token__={access_key}"


class Goraku(Extractor):
    """Fetch episodes from ゴラクうぇぶ!."""

    NAME = "goraku"
    HOSTS = ("gorakuweb.com",)
    URL_FORMS = (
        "https://gorakuweb.com/episode/<title id>/<episode id>",
        "https://gorakuweb.com/episode/<title id>",
    )

    @classmethod
    def suitable(cls, url: str) -> bool:
        """Report whether `url` is an episode or a work URL of the site.

        Args:
            url: The URL to check.

        Returns:
            True for `/episode/<title id>` and `/episode/<title id>/<episode id>` on `HOSTS`.
        """
        if not super().suitable(url):
            return False
        path = urlparse(url).path
        return _EPISODE_PATH.match(path) is not None or _TITLE_PATH.match(path) is not None

    @classmethod
    def is_series(cls, url: str) -> bool:
        """Report whether `url` is a work URL, `/episode/<title id>`.

        Args:
            url: The URL to check.

        Returns:
            True for a work URL.
        """
        return _TITLE_PATH.match(urlparse(url).path) is not None

    def series_urls(self, url: str) -> list[str]:
        """List the episodes of a work, oldest first.

        The work page carries the whole episode list, newest first; closed
        episodes are listed too, and come back locked from `episode()`.

        Args:
            url: The `/episode/<title id>` URL.

        Returns:
            One episode URL per listed episode, first episode first.

        Raises:
            UnsupportedUrlError: The URL is not a work URL.
            NotAnEpisodePageError: The page lists no episode.
        """
        if not self.is_series(url):
            msg = f"{url} is not a work page."
            raise UnsupportedUrlError(msg)
        props = self._props(url)
        urls: list[str] = []
        for entry in reversed(props.get("episodeList") or []):
            href = entry.get("href") if isinstance(entry, dict) else None
            if not href:
                continue
            episode_url = urljoin(BASE_URL, str(href))
            if episode_url not in urls:
                urls.append(episode_url)
        if not urls:
            msg = f"the work at {url} lists no episode."
            raise NotAnEpisodePageError(msg)
        return urls

    def episode(self, url: str) -> Episode:
        """Read one episode off its page.

        The page's flight payload carries the titles, the page list with
        its CDN `base` and Akamai `accessKey`, and the AES key and iv the
        page files are encrypted under. A closed episode has none of the
        latter (`metadata` is null), and a work URL renders its latest
        episode.

        Args:
            url: The episode or work URL.

        Returns:
            The episode. `pages` is empty when it is not readable.

        Raises:
            UnsupportedUrlError: The URL is not an episode URL.
            NotAnEpisodePageError: The page describes no episode.
        """
        path = urlparse(url).path
        if _EPISODE_PATH.match(path) is None and _TITLE_PATH.match(path) is None:
            msg = f"{url} is not an episode URL of {self.NAME}."
            raise UnsupportedUrlError(msg)
        props = self._props(url)
        canonical = f"{BASE_URL}/episode/{props['titleId']}/{props['episodeId']}"
        next_href = props.get("nextEpisodeUrl")
        metadata = props.get("metadata")
        base, access_key = props.get("base"), props.get("accessKey")
        key, iv = props.get("keyBytes"), props.get("ivBytes")
        pages: list[Page] = []
        if isinstance(metadata, dict) and base and access_key:
            for entry in metadata.get("pages") or []:
                if not isinstance(entry, dict) or "filename" not in entry:
                    continue
                pages.append(
                    Page(
                        url=page_url(str(base), str(entry["filename"]), str(access_key)),
                        width=int(entry.get("width") or 0),
                        height=int(entry.get("height") or 0),
                        extra={"key": key, "iv": iv},
                    ),
                )
        return Episode(
            url=canonical,
            series_title=str(props.get("seriesTitle") or ""),
            episode_title=str(props.get("title") or ""),
            pages=tuple(pages),
            next_url=urljoin(BASE_URL, str(next_href)) if next_href else None,
            metadata=props,
        )

    def image(self, page: Page, episode: Episode) -> Image.Image:
        """Fetch one page file and decrypt it when the episode is keyed.

        Args:
            page: The page to fetch.
            episode: The episode the page belongs to.

        Returns:
            The page image.
        """
        res = self._get(page.url, headers={**self.HEADERS, "Referer": episode.url}, timeout=self.IMAGE_TIMEOUT)
        data = res.content
        key, iv = page.extra.get("key"), page.extra.get("iv")
        if key and iv:
            data = aes_cbc_decrypt(data, str(key), str(iv))
        return Image.open(BytesIO(data))

    def _props(self, url: str) -> dict[str, Any]:
        """GET an episode or work page and pick out the episode props.

        Args:
            url: The page URL.

        Returns:
            The episode component's props.

        Raises:
            NotAnEpisodePageError: The site knows no such page, or it holds no episode.
        """
        res = self._session.get(url, headers=self.HEADERS, timeout=self.TIMEOUT)
        # An unknown id is a 500 from the app, not a 404.
        if res.status_code in (HTTPStatus.NOT_FOUND, HTTPStatus.INTERNAL_SERVER_ERROR):
            msg = f"no episode at {url} (HTTP {res.status_code})."
            raise NotAnEpisodePageError(msg)
        res.raise_for_status()
        props = episode_props(res.text)
        if props is None:
            msg = f"no episode on {url}."
            raise NotAnEpisodePageError(msg)
        return props
