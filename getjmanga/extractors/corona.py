"""コロナEX (TOブックス), whose viewer shuffles every page into a grid the API describes per page."""

from __future__ import annotations

import base64
import binascii
import re
from http import HTTPStatus
from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import urlparse

from httpx import HTTPError

from getjmanga.errors import GetjmangaError, LoginError, NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractor import Episode, Extractor, Page

if TYPE_CHECKING:
    from httpx import Client, Response
    from PIL import Image

BASE_URL = "https://to-corona-ex.com"
#: The site's own backend; the work page and the viewer read everything through it.
API_URL = "https://api.to-corona-ex.com"
#: What the bundle sends as `X-API-Environment-Key` with every API call; the
#: API answers 403 without it. Baked into `_app-*.js`, so it may rotate with
#: a deploy -- `_api()` re-reads it from the bundle when the site says 403.
API_ENVIRONMENT_KEY = "K4FWy7Iqott9mrw37hDKfZ2gcLOwO-kiLHTwXT8ad1E="
#: The Firebase project the site signs in against, and Firebase's own sign-in endpoint.
FIREBASE_API_KEY = "AIzaSyCeiy1JMHVkFuI8zbiAxMjNO3zoXECENhE"
FIREBASE_SIGN_IN_URL = "https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword"
#: The statuses of the episodes the work page lists: free, or for subscribers.
EPISODE_STATUSES = "free_viewing,only_for_subscription"
#: How many episodes one listing page asks for; the site pages with `after_than`.
LISTING_LIMIT = 100

# `/episodes/<id>`; the site itself redirects a trailing slash away.
_EPISODE_PATH = re.compile(r"^/episodes/(?P<id>\d+)/?$")
# `/comics/<id>`, the work page with the episode list.
_SERIES_PATH = re.compile(r"^/comics/(?P<id>\d+)/?$")
# `n.defaults.headers.common["X-API-Environment-Key"]="..."` in `_app-*.js`.
_APP_BUNDLE = re.compile(r'src="(?P<src>/_next/static/chunks/pages/_app-[^"]+\.js)"')
_ENVIRONMENT_KEY = re.compile(r'"X-API-Environment-Key"\]\s*=\s*"(?P<key>[^"]+)"')

_API_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Origin": BASE_URL,
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-site",
}


def parse_drm_hash(drm_hash: str) -> tuple[int, int, list[int]]:
    """Read the grid and the tile order out of a page's `drm_hash`.

    The hash is base64 of `[columns, rows, *order]`, one byte each, where
    `order[n]` is the source tile that goes into the n-th destination slot,
    counted row by row.

    Args:
        drm_hash: The page's `drm_hash` as the API hands it over.

    Returns:
        Columns, rows, and one source tile index per destination tile.

    Raises:
        GetjmangaError: The hash is not base64, or does not describe a grid.
    """
    try:
        raw = base64.b64decode(drm_hash, validate=True)
    except (binascii.Error, ValueError) as error:
        msg = f"{drm_hash!r} is not a base64 drm_hash."
        raise GetjmangaError(msg) from error
    if len(raw) < 2:  # noqa: PLR2004 (the two grid bytes)
        msg = f"{drm_hash!r} names no grid."
        raise GetjmangaError(msg)
    columns, rows, *order = raw
    if columns * rows != len(order) or sorted(order) != list(range(columns * rows)):
        msg = f"{drm_hash!r} is not a permutation of a {columns}x{rows} grid."
        raise GetjmangaError(msg)
    return columns, rows, order


def descramble(image: Image.Image, drm_hash: str) -> Image.Image:
    """Put a scrambled page back together.

    The viewer draws the page as served, then copies `order[n]`-th source
    tile over the n-th destination slot, row by row. A tile is the page
    trimmed to a multiple of 8 and divided by the grid, floored, so a strip
    of up to `7 + columns` pixels on the right and bottom edge is never
    shuffled and is kept as-is.

    Args:
        image: The page exactly as the CDN serves it.
        drm_hash: The page's `drm_hash`.

    Returns:
        A new image with the tiles back in reading order.
    """
    columns, rows, order = parse_drm_hash(drm_hash)
    width, height = image.size
    tile_width = (width - width % 8) // columns
    tile_height = (height - height % 8) // rows
    out = image.copy()
    for dest, src in enumerate(order):
        src_row, src_col = divmod(src, columns)
        dest_row, dest_col = divmod(dest, columns)
        tile = image.crop(
            (
                tile_width * src_col,
                tile_height * src_row,
                tile_width * (src_col + 1),
                tile_height * (src_row + 1),
            ),
        )
        out.paste(tile, (tile_width * dest_col, tile_height * dest_row))
    return out


def episode_url(episode_id: str | int) -> str:
    """The canonical URL of an episode.

    Args:
        episode_id: The episode id, `/episodes/<id>`.

    Returns:
        The episode URL.
    """
    return f"{BASE_URL}/episodes/{episode_id}"


class Corona(Extractor):
    """Fetch episodes from コロナEX."""

    NAME = "corona"
    HOSTS = ("to-corona-ex.com",)
    PUBLISHER = "TOブックス"
    URL_FORMS = (
        "https://to-corona-ex.com/episodes/<episode>",
        "https://to-corona-ex.com/comics/<comic>",
    )
    CONFIG_KEY = "corona"
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
        self._environment_key = API_ENVIRONMENT_KEY
        self._key_refreshed = False
        #: The Firebase id token `login()` obtained, sent as a bearer token afterwards.
        self._token: str | None = None
        #: The credits of each work asked about, by comic id.
        self._credits: dict[str, str] = {}

    @classmethod
    def suitable(cls, url: str) -> bool:
        """Report whether the extractor takes `url`.

        Args:
            url: The URL to check.

        Returns:
            True for an episode or a work page on the site.
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
            True for `/comics/<id>`.
        """
        return _SERIES_PATH.match(urlparse(url).path) is not None

    def series_urls(self, url: str) -> list[str]:
        """List every episode of a work, first episode first.

        The work page asks `/episodes` for the free and the subscriber-only
        episodes in `episode_order`, a hundred at a time, and follows
        `next_cursor` as `after_than`.

        Args:
            url: A work URL.

        Returns:
            One episode URL per listed episode, in reading order.

        Raises:
            UnsupportedUrlError: The URL is not a work page.
            NotAnEpisodePageError: The site knows no such work, or it lists no episode.
        """
        match = _SERIES_PATH.match(urlparse(url).path)
        if match is None:
            msg = f"{url} is not a series page."
            raise UnsupportedUrlError(msg)
        comic_id = match["id"]
        urls: list[str] = []
        cursor: str | None = None
        while True:
            params: dict[str, str | int] = {
                "comic_id": comic_id,
                "limit": LISTING_LIMIT,
                "sort": "episode_order",
                "order": "asc",
                "episode_status": EPISODE_STATUSES,
            }
            if cursor is not None:
                params["after_than"] = cursor
            res = self._api(f"{API_URL}/episodes", url, params=params)
            if res.status_code == HTTPStatus.NOT_FOUND:
                msg = f"no work {comic_id} on {BASE_URL}."
                raise NotAnEpisodePageError(msg)
            res.raise_for_status()
            listing = res.json()
            for entry in listing.get("resources") or []:
                candidate = episode_url(entry["id"])
                if candidate not in urls:
                    urls.append(candidate)
            cursor = listing.get("next_cursor")
            if not cursor:
                break
        if not urls:
            msg = f"the series at {url} lists no episode."
            raise NotAnEpisodePageError(msg)
        return urls

    def episode(self, url: str) -> Episode:
        """Read one episode and list its pages.

        `begin_reading` names the work and the episode and hands the signed
        page URLs over, each with its `drm_hash`; it answers 402 with the same
        description and no pages when the episode is for subscribers only.
        `end_reading` names the neighbouring episodes.

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
        episode_id = match["id"]
        canonical = episode_url(episode_id)

        res = self._api(f"{API_URL}/episodes/{episode_id}/begin_reading", canonical)
        if res.status_code == HTTPStatus.NOT_FOUND:
            msg = f"no episode {episode_id} on {BASE_URL}."
            raise NotAnEpisodePageError(msg)
        if res.status_code != HTTPStatus.PAYMENT_REQUIRED:
            res.raise_for_status()
        described = res.json()
        if not isinstance(described, dict) or "episode_id" not in described:
            msg = f"{canonical} describes no episode."
            raise NotAnEpisodePageError(msg)

        neighbours = self._neighbours(episode_id, canonical)
        prev_entry, next_entry = neighbours.get("previous_episode"), neighbours.get("next_episode")
        prev_url = episode_url(prev_entry["id"]) if isinstance(prev_entry, dict) and prev_entry.get("id") else None
        next_url = episode_url(next_entry["id"]) if isinstance(next_entry, dict) and next_entry.get("id") else None

        pages: tuple[Page, ...] = ()
        if res.status_code == HTTPStatus.OK:
            pages = tuple(
                Page(url=str(entry["page_image_url"]), extra={"drm_hash": str(entry.get("drm_hash") or "")})
                for entry in described.get("pages") or []
                if entry.get("page_image_url")
            )
        return Episode(
            url=canonical,
            series_title=str(described.get("comic_title") or described.get("comic_id") or ""),
            episode_title=str(described.get("episode_title") or episode_id),
            pages=pages,
            prev_url=prev_url,
            next_url=next_url,
            metadata={"episode": described, "neighbours": neighbours},
            writer=self._writer(str(described.get("comic_id") or ""), canonical),
            publisher=self.PUBLISHER,
        )

    def _writer(self, comic_id: str, referer: str) -> str:
        """The work's `authors` as `/comics/<id>` credits them, `名前 (役割)` each, asked for once per work."""
        if not comic_id:
            return ""
        if comic_id not in self._credits:
            res = self._api(f"{API_URL}/comics/{comic_id}", referer)
            body = res.json() if res.status_code == HTTPStatus.OK else {}
            authors = body.get("authors") if isinstance(body, dict) else None
            credited = []
            for author in authors or []:
                name, role = str(author.get("name") or "").strip(), str(author.get("role") or "").strip()
                if name:
                    credited.append(f"{name} ({role})" if role else name)
            self._credits[comic_id] = ", ".join(credited)
        return self._credits[comic_id]

    def image(self, page: Page, episode: Episode) -> Image.Image:
        """Fetch one page and put its tiles back in order.

        The page URLs are signed, so the CDN serves them without a cookie;
        the episode URL is still sent as the Referer, as the viewer does.

        Args:
            page: The page to fetch.
            episode: The episode the page belongs to.

        Returns:
            The page image.
        """
        image = self._fetch_image(page.url, headers={**self.HEADERS, "Referer": episode.url})
        drm_hash = str(page.extra.get("drm_hash") or "")
        return descramble(image, drm_hash) if drm_hash else image

    def login(self, url: str, username: str, password: str) -> None:  # noqa: ARG002 (one sign-in endpoint)
        """Sign in with an email address, so subscriber-only episodes become readable.

        The site signs in through Firebase Authentication: the id token that
        `signInWithPassword` hands back is what the viewer sends as a bearer
        token to `begin_reading`. Accounts registered with a social login
        cannot sign in this way.

        Args:
            url: Any URL on the site.
            username: The account's email address.
            password: The account's password.

        Raises:
            LoginError: Firebase refused the credentials.
        """
        res = self._session.post(
            FIREBASE_SIGN_IN_URL,
            params={"key": FIREBASE_API_KEY},
            json={"email": username, "password": password, "returnSecureToken": True},
            headers={**self.HEADERS, "Content-Type": "application/json", "Origin": BASE_URL, "Referer": f"{BASE_URL}/"},
            timeout=self.TIMEOUT,
        )
        try:
            answer = res.json()
        except ValueError:
            answer = {}
        token = answer.get("idToken") if isinstance(answer, dict) else None
        if not res.is_success or not token:
            error = answer.get("error") if isinstance(answer, dict) else None
            reason = error.get("message") if isinstance(error, dict) else None
            msg = f"{BASE_URL} refused the credentials for {username!r}: {reason or f'HTTP {res.status_code}'}."
            raise LoginError(msg)
        self._token = str(token)

    def _api(self, url: str, referer: str, *, params: dict[str, str | int] | None = None) -> Response:
        """GET an API endpoint the way the site's bundle does.

        A 403 means the environment key went stale, in which case it is read
        off the site's `_app` bundle once and the call is repeated.

        Returns:
            The response, whatever its status.
        """
        res = self._session.get(url, params=params, headers=self._api_headers(referer), timeout=self.TIMEOUT)
        if res.status_code == HTTPStatus.FORBIDDEN and not self._key_refreshed:
            self._key_refreshed = True
            refreshed = self._read_environment_key()
            if refreshed and refreshed != self._environment_key:
                self._environment_key = refreshed
                res = self._session.get(url, params=params, headers=self._api_headers(referer), timeout=self.TIMEOUT)
        return res

    def _api_headers(self, referer: str) -> dict[str, str]:
        headers = {**self.HEADERS, **_API_HEADERS, "Referer": referer, "X-API-Environment-Key": self._environment_key}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _read_environment_key(self) -> str | None:
        """The `X-API-Environment-Key` the site's current bundle carries, or None."""
        try:
            home = self._get(f"{BASE_URL}/").text
            bundle = _APP_BUNDLE.search(home)
            if bundle is None:
                return None
            match = _ENVIRONMENT_KEY.search(self._get(f"{BASE_URL}{bundle['src']}").text)
        except HTTPError:  # the baked key is then kept.
            return None
        return match["key"] if match is not None else None

    def _neighbours(self, episode_id: str, referer: str) -> dict[str, Any]:
        """What `end_reading` says about the episodes around this one, or `{}`."""
        res = self._api(
            f"{API_URL}/episodes/{episode_id}/end_reading",
            referer,
            params={"previous_and_next_episode_status": EPISODE_STATUSES},
        )
        if res.status_code != HTTPStatus.OK:
            return {}
        body = res.json()
        return body if isinstance(body, dict) else {}
