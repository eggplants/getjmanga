"""ヤンジャン+ (YanJan+, 集英社), whose web viewer draws every page through a transposed 4x4 tile grid.

The site is a Nuxt app over a JSON API at `webapi.ynjn.jp`; the HTML carries
no episode data of its own. An episode is `/viewer/<title>/<episode>`, a work
page `/title/<title>` (the same list under `/episodeList/<title>`), and the
viewer asks `/viewer?title_id=&episode_id=` for the page list. Page files come
from the public CDN as lossless WebP with their 4x4 tiles transposed, which
`descramble()` undoes; nothing is signed and no cookie is needed for a free
episode. A locked one (ticket, gold coins or a membership) is answered with
an empty `pages` list and an `action_sheet` naming the price.
"""

from __future__ import annotations

import re
from http import HTTPStatus
from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import urlencode, urlparse

from .common import Episode, Extractor, LoginError, NotAnEpisodePageError, Page, UnsupportedUrlError

if TYPE_CHECKING:
    from PIL import Image
    from requests import Session

BASE_URL = "https://ynjn.jp"
#: The Nuxt app's `baseApiUrl`; `credentials: include` on every call.
API_URL = "https://webapi.ynjn.jp"
#: The viewer cuts a page into this many tiles per side.
GRID = 4

# `/viewer/<title>/<episode>`; `/viewer/comic/<id>` is a purchased volume, which is not an episode.
_EPISODE_PATH = re.compile(r"^/viewer/(?P<title>\d+)/(?P<episode>\d+)/?$")
# The work page, or its full episode list.
_SERIES_PATH = re.compile(r"^/(?:title|episodeList)/(?P<title>\d+)/?$")

_API_HEADERS = {
    "Accept": "application/json",
    "Origin": BASE_URL,
    "Referer": f"{BASE_URL}/",
}


def descramble(image: Image.Image) -> Image.Image:
    """Put a page back together the way the viewer's canvas does.

    The viewer draws the file as served, then copies each of the 4x4 tiles
    of `floor(w/4) x floor(h/4)` pixels to the transposed cell: tile
    (row, col) of the file lands at (col, row) on the canvas. The strip left
    over by the flooring stays where it is.

    Args:
        image: The page as the CDN serves it.

    Returns:
        A new image with the tiles transposed back.
    """
    width, height = image.size
    tile_w, tile_h = width // GRID, height // GRID
    out = image.copy()
    for row in range(GRID):
        for col in range(GRID):
            tile = image.crop((col * tile_w, row * tile_h, (col + 1) * tile_w, (row + 1) * tile_h))
            out.paste(tile, (row * tile_w, col * tile_h))
    return out


def episode_url(title_id: str | int, episode_id: str | int) -> str:
    """The canonical URL of an episode.

    Args:
        title_id: The work's id, `/title/<title>`.
        episode_id: The episode's id.

    Returns:
        The viewer URL.
    """
    return f"{BASE_URL}/viewer/{title_id}/{episode_id}"


class YanJan(Extractor):
    """Fetch episodes from ヤンジャン+ (Shueisha)."""

    NAME = "ynjn"
    HOSTS = ("ynjn.jp",)
    URL_FORMS = (
        "https://ynjn.jp/viewer/<title>/<episode>",
        "https://ynjn.jp/title/<title>",
        "https://ynjn.jp/episodeList/<title>",
    )
    # A single host; the `[site.ynjn]` section reads more naturally than `[site."ynjn.jp"]`.
    CONFIG_KEY = "ynjn"
    HEADERS: ClassVar[dict[str, str]] = {**Extractor.HEADERS, **_API_HEADERS}

    def __init__(self, session: Session | None = None) -> None:
        """Build an extractor.

        Args:
            session: A session to reuse. A retrying one is made when omitted.
        """
        super().__init__(session)
        # The episode list of a work, kept per title: a bulk run through a
        # series asks for it once per locked episode.
        self._listings: dict[str, list[dict[str, Any]]] = {}

    @classmethod
    def suitable(cls, url: str) -> bool:
        """Report whether the extractor takes `url`.

        Args:
            url: The URL to check.

        Returns:
            True for an episode viewer, a work page or an episode list on `ynjn.jp`.
        """
        if not super().suitable(url):
            return False
        path = urlparse(url).path
        return _EPISODE_PATH.match(path) is not None or _SERIES_PATH.match(path) is not None

    @classmethod
    def is_series(cls, url: str) -> bool:
        """Report whether `url` names a whole work rather than one episode.

        Args:
            url: The URL to check.

        Returns:
            True for `/title/<title>` and `/episodeList/<title>`.
        """
        return _SERIES_PATH.match(urlparse(url).path) is not None

    def series_urls(self, url: str) -> list[str]:
        """List every episode of a work, first episode first.

        Args:
            url: A work page or episode list URL.

        Returns:
            One viewer URL per listed episode, in the order the site lists them.

        Raises:
            UnsupportedUrlError: The URL is not a work page.
            NotAnEpisodePageError: The site knows no such work, or it lists no episode.
        """
        match = _SERIES_PATH.match(urlparse(url).path)
        if match is None:
            msg = f"{url} is not a series page."
            raise UnsupportedUrlError(msg)
        title_id = match["title"]
        urls: list[str] = []
        for entry in self._listing(title_id):
            candidate = episode_url(title_id, entry["id"])
            if candidate not in urls:
                urls.append(candidate)
        if not urls:
            msg = f"the series at {url} lists no episode."
            raise NotAnEpisodePageError(msg)
        return urls

    def episode(self, url: str) -> Episode:
        """Read one episode and list its pages.

        `/viewer` answers a readable episode with its pages and the id of the
        next one; a locked episode gets an empty page list and an
        `action_sheet` with the price, and then the work's episode list says
        what follows it.

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
        title_id, episode_id = match["title"], match["episode"]

        query = urlencode({"title_id": title_id, "episode_id": episode_id})
        data = self._api(f"{API_URL}/viewer?{query}")
        if data is None:
            msg = f"no episode {episode_id} on {BASE_URL}."
            raise NotAnEpisodePageError(msg)
        navigation: dict[str, Any] = data.get("viewer_navigation") or {}
        sheet: dict[str, Any] = data.get("action_sheet") or {}

        pages: list[Page] = []
        for entry in data.get("pages") or []:
            manga_page = entry.get("manga_page") if isinstance(entry, dict) else None
            if not isinstance(manga_page, dict) or not manga_page.get("page_image_url"):
                continue  # a `topic` advert or the `end_page`
            pages.append(
                Page(
                    url=str(manga_page["page_image_url"]),
                    width=int(manga_page.get("image_horizontal_size") or 0),
                    height=int(manga_page.get("image_vertical_size") or 0),
                    extra={"page_number": manga_page.get("page_number")},
                ),
            )

        next_id = int(navigation.get("next_episode_id") or 0)
        if not next_id and not pages:
            next_id = self._following(title_id, episode_id)
        return Episode(
            url=episode_url(title_id, episode_id),
            series_title=str(navigation.get("title_name") or sheet.get("title_name") or title_id),
            episode_title=str(navigation.get("name") or sheet.get("episode_name") or episode_id),
            pages=tuple(pages),
            next_url=episode_url(title_id, next_id) if next_id else None,
            metadata=data,
        )

    def image(self, page: Page, episode: Episode) -> Image.Image:
        """Fetch one page from the CDN and transpose its tiles back.

        Args:
            page: The page to fetch.
            episode: The episode the page belongs to.

        Returns:
            The page image.
        """
        return descramble(self._fetch_image(page.url, headers={**self.HEADERS, "Referer": episode.url}))

    def login(self, url: str, username: str, password: str) -> None:  # noqa: ARG002 (one site, one login API)
        """Sign in with an email address, so rented and membership episodes become readable.

        `/auth/login` takes the credentials as JSON and sets the session cookies
        on `.ynjn.jp`, which every later API call carries. A refusal answers
        401 with `is_success: false` and a message.

        Args:
            url: Any URL on the site.
            username: The account's email address.
            password: The account's password.

        Raises:
            LoginError: The site refused the credentials.
        """
        res = self._session.post(
            f"{API_URL}/auth/login",
            json={"email": username, "password": password},
            headers={**self.HEADERS, "Referer": f"{BASE_URL}/login"},
            timeout=self.TIMEOUT,
        )
        body = res.json() if res.status_code in (HTTPStatus.OK, HTTPStatus.UNAUTHORIZED) else None
        if not isinstance(body, dict) or not body.get("is_success"):
            reason = (body or {}).get("data") if isinstance(body, dict) else None
            detail = f": {reason['message']}" if isinstance(reason, dict) and reason.get("message") else ""
            msg = f"{BASE_URL} refused the credentials for {username!r}{detail}"
            raise LoginError(msg)

    def _api(self, url: str) -> dict[str, Any] | None:
        """One API answer's `data`, or None when the site says `is_success: false`.

        The API answers an unknown id with HTTP 500 and an error envelope, so
        the status is not what tells success apart.
        """
        res = self._session.get(url, headers=self.HEADERS, timeout=self.TIMEOUT)
        try:
            body = res.json()
        except ValueError:
            body = None
        if not isinstance(body, dict) or not body.get("is_success") or not isinstance(body.get("data"), dict):
            if res.status_code not in (HTTPStatus.OK, HTTPStatus.INTERNAL_SERVER_ERROR):
                res.raise_for_status()
            return None
        return body["data"]

    def _listing(self, title_id: str) -> list[dict[str, Any]]:
        """The episodes of a work as `/title/<title>/episode?is_get_all=true` lists them, fetched once.

        Raises:
            NotAnEpisodePageError: The site knows no such work.
        """
        if title_id not in self._listings:
            data = self._api(f"{API_URL}/title/{title_id}/episode?is_get_all=true")
            if data is None:
                msg = f"no series {title_id} on {BASE_URL}."
                raise NotAnEpisodePageError(msg)
            self._listings[title_id] = [entry for entry in data.get("episodes") or [] if isinstance(entry, dict)]
        return self._listings[title_id]

    def _following(self, title_id: str, episode_id: str) -> int:
        """The id of the episode listed after `episode_id`, or 0 at the end or when unknown."""
        try:
            entries = self._listing(title_id)
        except NotAnEpisodePageError:
            return 0
        ids = [str(entry.get("id")) for entry in entries]
        if episode_id not in ids:
            return 0
        index = ids.index(episode_id)
        return int(ids[index + 1]) if index + 1 < len(ids) else 0
