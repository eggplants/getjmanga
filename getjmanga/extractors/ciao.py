"""ちゃおプラス (Shogakukan's Ciao), and the "bambi" viewer it runs.

The site is a Nuxt app under `/comics/` whose data comes from
`https://api.ciao.shogakukan.co.jp`, the same white-label viewer family as
K MANGA and Comic NORA (`viewers/kmanga.py`): the bundle names its platform `bambi`
(`x-bambi-hash`, `bambi_img_viewer_recommend.png`) and speaks the same
`/web/episode/viewer` API. Every request is signed with the sorted
`sha256(key)_sha512(value)` pairs of its parameters, SHA-256'd, then
SHA-512'd; unlike Nora's, this API checks the header and answers `invalid
hash.` without it, and its signature carries no birthday suffix.

- `/comics/title/<title>/episode/<episode>` is an episode page; the title id
  is zero-padded to five digits in URLs and plain in the API.
- `/comics/title/<title>/` is the work page; the site redirects it to the
  first episode, whose page lists the rest.
- `GET /web/episode?episode_id=` names the episode and the work and says
  whether the reader may open it (`is_page_visible`); a point-priced or
  unreleased episode has it at 0 and the site shows a purchase panel
  instead of the viewer. An unknown id is a `400` with `response_code` 3100.
- `GET /title/list?title_id_list=` describes the work, with
  `episode_id_list` in reading order; an unknown id is `response_code` 3000.
- `GET /web/episode/viewer?episode_id=` hands over the signed CloudFront
  page URLs, `scramble_seed` and `scramble_ver`.
- `POST /web/user/login` (form: `email`, `password`) signs in; the API sets
  the session cookie itself and refuses bad credentials with a `400`.

Pages are JPEGs on `cdn.ciao.shogakukan.co.jp`, served without a Referer
or a cookie, cut into a 4 x 4 grid and shuffled with the xorshift32
permutation `viewers/kmanga.py` undoes. `scramble_ver` picks how the tile
side is worked out: version 2 rounds `width / 8 / 4` down and multiplies
by 8 (what `kmanga.descramble()` does), version 1 rounds the image down to
a multiple of 8 first and then divides by 4, so its tiles need only be
even.
"""

from __future__ import annotations

import hashlib
import re
from http import HTTPStatus
from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import urlparse

from getjmanga.errors import LoginError, NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractor import Episode, Extractor, Page, neighbours
from getjmanga.viewers.kmanga import GRID, SEED_MAX, SEED_MIN, UNIT, tile_order
from getjmanga.viewers.kmanga import descramble as descramble_v2

if TYPE_CHECKING:
    from collections.abc import Mapping

    from httpx import Client, Response
    from PIL import Image

BASE_URL = "https://ciao.shogakukan.co.jp"
API_URL = "https://api.ciao.shogakukan.co.jp"

#: What the bundle calls itself to the API, and the web platform id.
_APP_VERSION = "6.0.0"
_PLATFORM = "3"
#: The request-signing header the API insists on, and the one that flags a crawler.
_HASH_HEADER = "x-bambi-hash"
_CRAWLER_HEADER = "x-bambi-is-crawler"

#: `response_code` of `/web/episode` and `/title/list` for an id the site does not know.
_EPISODE_NOT_FOUND = 3100
_TITLE_NOT_FOUND = 3000

#: The tile-size formula the viewer used before `scramble_ver` 2.
_SCRAMBLE_V1 = 1

# `/comics/title/00813/episode/32965`
_EPISODE_PATH = re.compile(r"^/comics/title/(?P<title>\d+)/episode/(?P<episode>\d+)/?$")
# `/comics/title/00813/`, which the site redirects to the first episode.
_SERIES_PATH = re.compile(r"^/comics/title/(?P<title>\d+)/?$")


def service_hash(params: Mapping[str, str | int]) -> str:
    """Sign a set of parameters the way the bundle does.

    Args:
        params: The query or form parameters of the request.

    Returns:
        The value of the `x-bambi-hash` header.
    """
    pairs = ",".join(f"{_sha256(key)}_{_sha512(str(params[key]))}" for key in sorted(params))
    return _sha512(_sha256(pairs))


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _sha512(text: str) -> str:
    return hashlib.sha512(text.encode()).hexdigest()


def descramble(image: Image.Image, seed: int, version: int = 2) -> Image.Image:
    """Put a scrambled page back together.

    Args:
        image: The page as served.
        seed: The `scramble_seed` of the episode.
        version: The `scramble_ver` of the episode, which picks the tile size.

    Returns:
        A new image with the tiles in place. The image is returned as is when
        the seed is out of range or the image is too small to be tiled.
    """
    if version != _SCRAMBLE_V1:
        return descramble_v2(image, seed)
    width, height = image.size
    if not (SEED_MIN <= seed <= SEED_MAX) or width < GRID or height < GRID:
        return image
    if width > UNIT and height > UNIT:
        width, height = width // UNIT * UNIT, height // UNIT * UNIT
    tile_width, tile_height = width // GRID, height // GRID
    if not tile_width or not tile_height:
        return image
    out = image.copy()
    for destination, source in enumerate(tile_order(seed)):
        sx, sy = source % GRID * tile_width, source // GRID * tile_height
        dx, dy = destination % GRID * tile_width, destination // GRID * tile_height
        out.paste(image.crop((sx, sy, sx + tile_width, sy + tile_height)), (dx, dy))
    return out


def episode_url(title_id: int, episode_id: int) -> str:
    """The canonical URL of an episode.

    Args:
        title_id: The work's id.
        episode_id: The episode's id.

    Returns:
        The episode URL, with the title id zero-padded the way the site links it.
    """
    return f"{BASE_URL}/comics/title/{title_id:05d}/episode/{episode_id}"


class Ciao(Extractor):
    """Fetch episodes from ちゃおプラス."""

    NAME = "ciao"
    HOSTS = ("ciao.shogakukan.co.jp",)
    URL_FORMS = (
        "https://ciao.shogakukan.co.jp/comics/title/<title>/episode/<episode>",
        "https://ciao.shogakukan.co.jp/comics/title/<title>/",
    )
    CONFIG_KEY = "ciao"
    HEADERS: ClassVar[dict[str, str]] = {
        **Extractor.HEADERS,
        "Origin": BASE_URL,
        "Referer": f"{BASE_URL}/",
    }

    def __init__(self, session: Client | None = None) -> None:
        """Build an extractor.

        Args:
            session: A session to reuse. A retrying one is made when omitted.
        """
        super().__init__(session)
        # `/title/list` describes a work with its whole episode list; a bulk
        # run asks about the same work once per episode, so it is kept.
        self._titles: dict[int, dict[str, Any]] = {}

    @classmethod
    def suitable(cls, url: str) -> bool:
        """Report whether the extractor takes `url`.

        Args:
            url: The URL to check.

        Returns:
            True for an https episode or work page on the known host.
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
            True for `/comics/title/<title>/`.
        """
        return _SERIES_PATH.match(urlparse(url).path) is not None

    def series_urls(self, url: str) -> list[str]:
        """List every episode of a work, first episode first.

        Args:
            url: A work page URL.

        Returns:
            One episode URL per listed episode, in the order the API lists them.

        Raises:
            UnsupportedUrlError: The URL is not a work page.
            NotAnEpisodePageError: The site knows no such work, or it lists no episode.
        """
        match = _SERIES_PATH.match(urlparse(url).path)
        if match is None:
            msg = f"{url} is not a work page."
            raise UnsupportedUrlError(msg)
        title_id = int(match["title"])
        title = self._title(title_id)
        if title is None:
            msg = f"no work {title_id} on {BASE_URL}."
            raise NotAnEpisodePageError(msg)
        urls: list[str] = []
        for entry in title.get("episode_id_list") or []:
            candidate = episode_url(title_id, int(entry))
            if candidate not in urls:
                urls.append(candidate)
        if not urls:
            msg = f"the work at {url} lists no episode."
            raise NotAnEpisodePageError(msg)
        return urls

    def episode(self, url: str) -> Episode:
        """Read one episode and list its pages.

        `/web/episode` names the episode and says whether it may be opened,
        the work's listing names the one after it, and the viewer API hands
        the pages over only when the site would show the viewer.

        Args:
            url: The episode URL.

        Returns:
            The episode. `pages` is empty when it is priced in points, still
            unreleased, or otherwise withheld from the reader.

        Raises:
            UnsupportedUrlError: The URL is not an episode URL.
            NotAnEpisodePageError: The site knows no such episode.
        """
        match = _EPISODE_PATH.match(urlparse(url).path)
        if match is None:
            msg = f"{url} is not an episode page."
            raise UnsupportedUrlError(msg)
        episode_id = int(match["episode"])

        detail = self._call("/web/episode", {"episode_id": str(episode_id)}, not_found=_EPISODE_NOT_FOUND)
        entry = detail.get("episode") if isinstance(detail, dict) else None
        if not isinstance(entry, dict):
            msg = f"no episode {episode_id} on {BASE_URL}."
            raise NotAnEpisodePageError(msg)
        title_id = int(entry.get("title_id") or match["title"])
        title = self._title(title_id) or {}
        prev_id, next_id = neighbours([int(i) for i in title.get("episode_id_list") or []], episode_id)

        viewer: dict[str, Any] | None = None
        pages: tuple[Page, ...] = ()
        if entry.get("is_page_visible"):
            viewer = self._call("/web/episode/viewer", {"episode_id": str(episode_id)})
            seed = viewer.get("scramble_seed")
            extra = {"seed": seed, "version": int(viewer.get("scramble_ver") or 2)} if isinstance(seed, int) else {}
            pages = tuple(Page(url=str(src), extra=extra) for src in viewer.get("page_list") or [])
            preceding, following = viewer.get("previous_episode"), viewer.get("next_episode")
            if prev_id is None and isinstance(preceding, dict) and preceding.get("episode_id") is not None:
                prev_id = int(preceding["episode_id"])
            if next_id is None and isinstance(following, dict) and following.get("episode_id") is not None:
                next_id = int(following["episode_id"])

        share = detail.get("share")
        shared_name = share.get("title_name") if isinstance(share, dict) else None
        return Episode(
            url=episode_url(title_id, episode_id),
            series_title=str(title.get("title_name") or shared_name or title_id),
            episode_title=str(entry.get("episode_name") or episode_id),
            pages=pages,
            prev_url=episode_url(title_id, prev_id) if prev_id is not None else None,
            next_url=episode_url(title_id, next_id) if next_id is not None else None,
            metadata={"episode": entry, "title": title, "viewer": viewer},
        )

    def image(self, page: Page, episode: Episode) -> Image.Image:
        """Fetch one page from the CDN and unscramble it.

        Args:
            page: The page to fetch.
            episode: The episode the page belongs to.

        Returns:
            The page image.
        """
        image = self._fetch_image(page.url, headers={**self.HEADERS, "Referer": episode.url})
        seed = page.extra.get("seed")
        if seed is None:
            return image
        return descramble(image, int(seed), int(page.extra.get("version") or 2))

    def login(self, url: str, username: str, password: str) -> None:  # noqa: ARG002 (one sign-in endpoint)
        """Sign in, so episodes the account has unlocked become readable.

        Args:
            url: Ignored; the site has one sign-in endpoint.
            username: The account's email address.
            password: The account's password.

        Raises:
            LoginError: The site refused the credentials.
        """
        form = {"version": _APP_VERSION, "platform": _PLATFORM, "email": username, "password": password}
        res = self._session.post(
            f"{API_URL}/web/user/login",
            data=form,
            headers={**self._api_headers(form), "Content-Type": "application/x-www-form-urlencoded"},
            timeout=self.TIMEOUT,
        )
        body = _json_or_none(res)
        if res.status_code == HTTPStatus.BAD_REQUEST or not isinstance(body, dict) or body.get("status") != "success":
            reason = str((body.get("error_message") if isinstance(body, dict) else None) or "no reason given")
            msg = f"ciao.shogakukan.co.jp refused the credentials for {username!r}: {reason.rstrip('.')}."
            raise LoginError(msg)

    def _title(self, title_id: int) -> dict[str, Any] | None:
        """The work as `/title/list` describes it, fetched once; None when the site knows no such work."""
        if title_id not in self._titles:
            body = self._call("/title/list", {"title_id_list": str(title_id)}, not_found=_TITLE_NOT_FOUND)
            listing = body.get("title_list") or []
            self._titles[title_id] = listing[0] if listing and isinstance(listing[0], dict) else {}
        return self._titles[title_id] or None

    def _call(self, path: str, params: Mapping[str, str], *, not_found: int | None = None) -> dict[str, Any]:
        """GET one API path, signed, and hand its JSON back.

        Returns an empty dict when the API answers `400` -- with `not_found`,
        only for that `response_code` (an id the site does not know); without
        it, for any `400` (the viewer refusing an unreleased episode).
        """
        query = {"version": _APP_VERSION, "platform": _PLATFORM, **params}
        res = self._session.get(
            f"{API_URL}{path}", params=query, headers=self._api_headers(query), timeout=self.TIMEOUT
        )
        if res.status_code == HTTPStatus.BAD_REQUEST:
            body = _json_or_none(res)
            code = body.get("response_code") if isinstance(body, dict) else None
            if not_found is None or code == not_found:
                return {}
        res.raise_for_status()
        body = _json_or_none(res)
        return body if isinstance(body, dict) and body.get("status") == "success" else {}

    def _api_headers(self, params: Mapping[str, str]) -> dict[str, str]:
        return {**self.HEADERS, _HASH_HEADER: service_hash(params), _CRAWLER_HEADER: "false"}


def _json_or_none(res: Response) -> Any:  # noqa: ANN401 (whatever JSON the site sent)
    """`res.json()`, or None when the body is not JSON."""
    try:
        return res.json()
    except ValueError:
        return None
