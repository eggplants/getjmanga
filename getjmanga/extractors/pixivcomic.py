"""pixivコミック (pixiv comic), whose pages are grid-shuffled by the image server.

The site is a Next.js app rendered in the browser; the HTML carries nothing
but a `__NEXT_DATA__` blob, and every listing comes from the JSON API under
`https://comic.pixiv.net/api/app/`, which only asks for an
`X-Requested-With: pixivcomic` header:

- `GET /works/v5/<work>` describes a work (`official_work.name`, the author,
  `first_episode`).
- `GET /works/<work>/episodes/v2?order=asc&page=<n>` lists the episodes, 300
  per page, with `next_page_number` while there are more. An entry that is
  `not_publishing` carries no episode at all, only a message.
- `GET /episodes/<id>/read_v4` is what the viewer at `/viewer/stories/<id>`
  reads: the titles, `pages` and `next_episode`. It wants two more headers
  the viewer builds from a `salt` in the viewer page's `__NEXT_DATA__`:
  `X-Client-Time`, the current time as `yyyy-MM-ddTHH:mm:ssXXXXX`, and
  `X-Client-Hash`, `sha256(time + salt)` in hex. Without them it is a 400.
  A `purchasable` or `login_required` episode answers with `pages: []` and
  still names the next one; an unknown episode, or one whose run has ended,
  is a 404.

Every page is a JPEG on `img-comic.pximg.net` under a
`/c/q90_gridshuffle32:32/` prefix. The image server shuffles it on the way
out when the request carries a `X-Cobalt-Thumber-Parameter-GridShuffle-Key`
header with the page's `key` (and refuses it without a `comic.pixiv.net`
Referer). `descramble()` undoes that the way the viewer's `shuffle()` does:
the page is cut into rows of `gridsize` pixels, each row into
`width // gridsize` blocks, and every row's blocks are permuted with a
Fisher-Yates shuffle driven by a xoshiro128** generator seeded with the first
16 bytes of `sha256(SHUFFLE_SALT + key)`, after 100 discarded draws. The
strip narrower than a block on the right stays where it is.

Signing in is pixiv's own OAuth flow behind a reCAPTCHA, so the extractor
does not log in; only free episodes are covered.
"""

from __future__ import annotations

import hashlib
import json
import re
import struct
from datetime import UTC, datetime
from http import HTTPStatus
from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import urlparse

from getjmanga.errors import NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractor import Episode, Extractor, Page

if TYPE_CHECKING:
    from collections.abc import Iterator

    from PIL import Image
    from requests import Response, Session

BASE_URL = "https://comic.pixiv.net"
API_URL = f"{BASE_URL}/api/app"

#: The constant the viewer's bundle prepends to a page's `key` before hashing.
SHUFFLE_SALT = "4wXCKprMMoxnyJ3PocJFs4CYbfnbazNe"
#: The header that makes the image server shuffle a page with the given key.
SHUFFLE_KEY_HEADER = "X-Cobalt-Thumber-Parameter-GridShuffle-Key"
#: The block size the viewer falls back to when a page names none.
DEFAULT_GRIDSIZE = 32
#: How many generator outputs the viewer throws away before shuffling.
_WARMUP_DRAWS = 100
_MASK = 0xFFFFFFFF

_EPISODE_PATH = re.compile(r"^/viewer/stories/(?P<id>\d+)/?$")
_WORK_PATH = re.compile(r"^/works/(?P<id>\d+)/?$")
_NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__" type="application/json">(?P<json>.*?)</script>', re.DOTALL)

_API_HEADERS = {
    "Accept": "application/json",
    "X-Requested-With": "pixivcomic",
}


def read_headers(salt: str, now: datetime | None = None) -> dict[str, str]:
    """Sign a `read_v4` request the way the viewer's `getReadHeaders()` does.

    Args:
        salt: The `salt` of the viewer page's `__NEXT_DATA__`.
        now: The time to stamp the request with; the current UTC time when omitted.

    Returns:
        The `X-Client-Time` and `X-Client-Hash` headers.
    """
    stamp = (now if now is not None else datetime.now(UTC)).astimezone(UTC)
    time = stamp.strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"X-Client-Time": time, "X-Client-Hash": hashlib.sha256(f"{time}{salt}".encode()).hexdigest()}


def episode_url(episode_id: str | int) -> str:
    """The canonical URL of an episode.

    Args:
        episode_id: The episode id, `/viewer/stories/<id>`.

    Returns:
        The viewer URL.
    """
    return f"{BASE_URL}/viewer/stories/{episode_id}"


def _rotl(value: int, bits: int) -> int:
    bits %= 32
    return ((value << bits) | (value >> (32 - bits))) & _MASK


def xoshiro128(seed: tuple[int, int, int, int]) -> Iterator[int]:
    """Yield the xoshiro128** sequence the viewer shuffles blocks with.

    Args:
        seed: Four unsigned 32-bit words. An all-zero seed is read as `(1, 0, 0, 0)`.

    Yields:
        Unsigned 32-bit values, one per call.
    """
    s = [word & _MASK for word in seed]
    if not any(s):
        s[0] = 1
    while True:
        result = (9 * _rotl((5 * s[1]) & _MASK, 7)) & _MASK
        t = (s[1] << 9) & _MASK
        s[2] ^= s[0]
        s[3] ^= s[1]
        s[1] ^= s[2]
        s[0] ^= s[3]
        s[2] ^= t
        s[3] = _rotl(s[3], 11)
        yield result


def block_order(key: str, rows: int, columns: int, salt: str = SHUFFLE_SALT) -> list[list[int]]:
    """The block permutation of every row a page key produces.

    Args:
        key: The page's `key`.
        rows: How many block rows the page has.
        columns: How many whole blocks fit in a row.
        salt: The constant hashed in front of the key.

    Returns:
        One list per row: for every destination block, the index of the block
        in the served image that belongs there.
    """
    digest = hashlib.sha256(f"{salt}{key}".encode()).digest()
    seed = struct.unpack("<4I", digest[:16])
    values = xoshiro128((seed[0], seed[1], seed[2], seed[3]))
    for _ in range(_WARMUP_DRAWS):
        next(values)
    order: list[list[int]] = []
    for _ in range(rows):
        shuffled = list(range(columns))
        for end in range(columns - 1, 0, -1):
            pick = next(values) % (end + 1)
            shuffled[end], shuffled[pick] = shuffled[pick], shuffled[end]
        # `shuffled[source] = destination` as the server scrambled; invert it to read back.
        inverse = [0] * columns
        for source, destination in enumerate(shuffled):
            inverse[destination] = source
        order.append(inverse)
    return order


def descramble(image: Image.Image, key: str, gridsize: int = DEFAULT_GRIDSIZE) -> Image.Image:
    """Put a grid-shuffled page back together.

    Args:
        image: The page as the image server shuffled it.
        key: The page's `key`.
        gridsize: The block side in pixels, the page's `gridsize`.

    Returns:
        A new image with the blocks in place. The image is returned as is
        when it is narrower than one block.
    """
    width, height = image.size
    if gridsize <= 0 or width < gridsize:
        return image
    rows = -(-height // gridsize)
    columns = width // gridsize
    order = block_order(key, rows, columns)
    restored = image.copy()
    for row, sources in enumerate(order):
        top = row * gridsize
        bottom = min(top + gridsize, height)
        for destination, source in enumerate(sources):
            if source == destination:
                continue
            block = image.crop((source * gridsize, top, (source + 1) * gridsize, bottom))
            restored.paste(block, (destination * gridsize, top))
    return restored


class PixivComic(Extractor):
    """Fetch episodes from pixivコミック."""

    NAME = "pixivcomic"
    HOSTS = ("comic.pixiv.net",)
    URL_FORMS = (
        "https://comic.pixiv.net/viewer/stories/<id>",
        "https://comic.pixiv.net/works/<id>",
    )
    HEADERS: ClassVar[dict[str, str]] = {
        **Extractor.HEADERS,
        "sec-ch-ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Linux"',
    }

    def __init__(self, session: Session | None = None) -> None:
        """Build an extractor.

        Args:
            session: A session to reuse. A retrying one is made when omitted.
        """
        super().__init__(session)
        # The salt is one value for the whole site; read off the first viewer page and kept.
        self._salt: str | None = None

    @classmethod
    def suitable(cls, url: str) -> bool:
        """Report whether the extractor takes `url`.

        Args:
            url: The URL to check.

        Returns:
            True for a viewer page or a work page on `comic.pixiv.net`.
        """
        if not super().suitable(url):
            return False
        path = urlparse(url).path
        return _EPISODE_PATH.match(path) is not None or _WORK_PATH.match(path) is not None

    @classmethod
    def is_series(cls, url: str) -> bool:
        """Report whether `url` names a whole work rather than one episode.

        Args:
            url: The URL to check.

        Returns:
            True for `/works/<id>`.
        """
        return _WORK_PATH.match(urlparse(url).path) is not None

    def series_urls(self, url: str) -> list[str]:
        """List every episode of a work, first episode first.

        Args:
            url: A work URL.

        Returns:
            One viewer URL per listed episode, oldest first. Episodes whose
            run has ended are listed without an id and so left out.

        Raises:
            UnsupportedUrlError: The URL is not a work page.
            NotAnEpisodePageError: The work is unknown or lists no episode.
        """
        match = _WORK_PATH.match(urlparse(url).path)
        if match is None:
            msg = f"{url} is not a work page."
            raise UnsupportedUrlError(msg)
        work_id = match["id"]
        urls: list[str] = []
        page: int | None = 1
        while page is not None:
            res = self._session.get(
                f"{API_URL}/works/{work_id}/episodes/v2",
                params={"order": "asc", "page": page},
                headers={**self.HEADERS, **_API_HEADERS, "Referer": url},
                timeout=self.TIMEOUT,
            )
            data = self._data(res)
            if data is None:
                msg = f"no work {work_id} on {BASE_URL}."
                raise NotAnEpisodePageError(msg)
            for entry in data.get("episodes") or []:
                episode = entry.get("episode") if isinstance(entry, dict) else None
                if not isinstance(episode, dict) or episode.get("id") is None:
                    continue
                candidate = episode_url(episode["id"])
                if candidate not in urls:
                    urls.append(candidate)
            page = data.get("next_page_number") or None
        if not urls:
            msg = f"the work at {url} lists no episode."
            raise NotAnEpisodePageError(msg)
        return urls

    def episode(self, url: str) -> Episode:
        """Read one episode and list its pages.

        Args:
            url: The viewer URL.

        Returns:
            The episode. `pages` is empty when it wants a purchase or a sign-in.

        Raises:
            UnsupportedUrlError: The URL is not a viewer URL.
            NotAnEpisodePageError: The site knows no such episode, or no longer serves it.
        """
        match = _EPISODE_PATH.match(urlparse(url).path)
        if match is None:
            msg = f"{url} is not an episode page."
            raise UnsupportedUrlError(msg)
        episode_id = match["id"]
        page_url = episode_url(episode_id)

        res = self._session.get(
            f"{API_URL}/episodes/{episode_id}/read_v4",
            headers={**self.HEADERS, **_API_HEADERS, **read_headers(self._read_salt(page_url)), "Referer": page_url},
            timeout=self.TIMEOUT,
        )
        data = self._data(res)
        reading = data.get("reading_episode") if data is not None else None
        if not isinstance(reading, dict):
            msg = f"no episode {episode_id} on {BASE_URL}."
            raise NotAnEpisodePageError(msg)

        pages = tuple(
            Page(
                url=str(entry["url"]),
                width=int(entry.get("width") or 0),
                height=int(entry.get("height") or 0),
                extra={"key": entry.get("key"), "gridsize": int(entry.get("gridsize") or DEFAULT_GRIDSIZE)},
            )
            for entry in reading.get("pages") or []
            if isinstance(entry, dict) and entry.get("url")
        )
        following = reading.get("next_episode") or {}
        next_url = episode_url(following["id"]) if isinstance(following, dict) and following.get("id") else None
        title = reading.get("title") or " ".join(
            part for part in (reading.get("numbering_title"), reading.get("sub_title")) if part
        )
        return Episode(
            url=page_url,
            series_title=str(reading.get("work_title") or reading.get("work_id") or ""),
            episode_title=str(title or episode_id),
            pages=pages,
            next_url=next_url,
            metadata=reading,
        )

    def image(self, page: Page, episode: Episode) -> Image.Image:
        """Fetch one page with its shuffle key and put it back together.

        Args:
            page: The page to fetch.
            episode: The episode the page belongs to.

        Returns:
            The page image.
        """
        key = page.extra.get("key")
        headers = {**self.HEADERS, "Accept": "image/*,*/*", "Referer": episode.url}
        if key:
            headers[SHUFFLE_KEY_HEADER] = str(key)
        image = self._fetch_image(page.url, headers=headers)
        if not key:
            return image
        return descramble(image, str(key), int(page.extra.get("gridsize") or DEFAULT_GRIDSIZE))

    def _read_salt(self, page_url: str) -> str:
        """The `salt` of a viewer page's `__NEXT_DATA__`, read once per session.

        Raises:
            NotAnEpisodePageError: The page is not a viewer page, or carries no salt.
        """
        if self._salt is None:
            res = self._session.get(page_url, headers=self.HEADERS, timeout=self.TIMEOUT)
            if res.status_code == HTTPStatus.NOT_FOUND:
                msg = f"no viewer page at {page_url}."
                raise NotAnEpisodePageError(msg)
            res.raise_for_status()
            match = _NEXT_DATA.search(res.text)
            props: Any = None
            if match is not None:
                try:
                    props = json.loads(match["json"]).get("props", {}).get("pageProps")
                except (ValueError, AttributeError):
                    props = None
            salt = props.get("salt") if isinstance(props, dict) else None
            if not isinstance(salt, str) or not salt:
                msg = f"no viewer salt on {page_url}."
                raise NotAnEpisodePageError(msg)
            self._salt = salt
        return self._salt

    @staticmethod
    def _data(res: Response) -> dict[str, Any] | None:
        """The `data` object of an API answer, or None when the API knows no such thing (404)."""
        if res.status_code == HTTPStatus.NOT_FOUND:
            return None
        res.raise_for_status()
        try:
            body = res.json()
        except ValueError:
            return None
        data = body.get("data") if isinstance(body, dict) else None
        return data if isinstance(data, dict) else None
