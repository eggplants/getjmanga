"""Comic NORA (コミックノーラ, Nora), and the K MANGA-style viewer it embeds.

The site is a WordPress theme around a viewer that is a white-label of the
one Kodansha's K MANGA runs (the bundle still tags its share text `#KMANGA`
and signs requests with an `x-com-sega-md-hash` header). A work page
`/comic/page-<slug>/` lists the episodes and embeds the viewer for one of
them in an iframe, `?episode_id=<id>` picking which; without the query it
shows the latest one. Episodes that are `非公開` are listed without a link
and, asked for by id, come back as a work page with an empty viewer.

The viewer is a Nuxt app whose data comes from `https://api.<host>`:

- `GET /web/episode/viewer?version=6.0.0&platform=3&episode_id=<id>` answers
  the page list, the previous and the next episode, and `scramble_seed`.
  The site does not check the hash header at the time of writing, but the
  viewer sends it, so `service_hash()` builds it the way the bundle does:
  sorted `sha256(key)_sha512(value)` pairs, SHA-256'd, then SHA-512'd with a
  birthday cookie's `sha256(birthday)_sha512(expires)` (both empty for an
  anonymous reader) appended.
- An unreleased or missing episode is a `400` with `response_code` 3100 or
  3104, never a page list.

Pages are plain JPEGs on `cdn.<host>` (signed CloudFront URLs when the
episode is scrambled), served without a Referer or a cookie. With a seed,
`descramble()` undoes what the viewer's canvas does: the page is cut into a
4 x 4 grid of tiles whose side is a multiple of 8, the tiles are shuffled by
sorting them on the values of an xorshift32 generator seeded with
`scramble_seed`, and the strip left over on the right and at the bottom stays
where it is.

Nora's other comic site, ガッコミ (`gakcomic.gakken.jp`), shares the
`/comic/page-<id>/` URL shape but not the viewer: it serves DRM-encrypted
EPUBs through Keyring's BookEnd, so it is not covered here.
"""

from __future__ import annotations

import hashlib
import re
from http import HTTPStatus
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlencode, urlparse

from bs4 import BeautifulSoup
from bs4.element import Tag

from getjmanga.errors import NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractor import Episode, Extractor, Page

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    from PIL import Image

_WORK_PATH = re.compile(r"^/comic/page-(?P<slug>[^/]+)/?$")

#: What the viewer's bundle calls itself to the API, and the web platform id.
_APP_VERSION = "6.0.0"
_PLATFORM = "3"
#: The request-signing header the API expects, and the one that flags a crawler.
_HASH_HEADER = "x-com-sega-md-hash"
_CRAWLER_HEADER = "x-com-sega-md-is-crawler"

#: The scramble grid: `GRID x GRID` tiles whose sides are multiples of `UNIT`.
GRID = 4
UNIT = 8
_SEED_MIN = 1
_SEED_MAX = 2**32 - 1
_MASK = 0xFFFFFFFF


def service_hash(params: Mapping[str, str | int], birthday: str = "", expires: str = "") -> str:
    """Sign a set of query parameters the way the viewer does.

    Args:
        params: The query parameters of the request.
        birthday: The reader's birthday cookie, empty when signed out.
        expires: When that cookie expires, empty when signed out.

    Returns:
        The value of the `x-com-sega-md-hash` header.
    """
    pairs = ",".join(f"{_sha256(key)}_{_sha512(str(params[key]))}" for key in sorted(params))
    return _sha512(_sha256(pairs) + f"{_sha256(birthday)}_{_sha512(expires)}")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _sha512(text: str) -> str:
    return hashlib.sha512(text.encode()).hexdigest()


def xorshift32(seed: int) -> Iterator[int]:
    """Yield the xorshift32 sequence the viewer shuffles tiles with.

    Args:
        seed: The `scramble_seed` of the episode.

    Yields:
        Unsigned 32-bit values, one per call.
    """
    state = seed & _MASK
    while True:
        state ^= (state << 13) & _MASK
        state ^= state >> 17
        state ^= (state << 5) & _MASK
        yield state


def tile_order(seed: int, grid: int = GRID) -> list[int]:
    """The tile permutation a seed produces.

    Args:
        seed: The `scramble_seed` of the episode.
        grid: Tiles per side.

    Returns:
        For every destination tile (row-major), the index of the source tile
        in the served image that belongs there.
    """
    values = xorshift32(seed)
    keyed = [(next(values), index) for index in range(grid * grid)]
    keyed.sort(key=lambda pair: pair[0])
    return [index for _, index in keyed]


def descramble(image: Image.Image, seed: int, grid: int = GRID) -> Image.Image:
    """Put a scrambled page back together.

    Args:
        image: The page as served.
        seed: The `scramble_seed` of the episode.
        grid: Tiles per side.

    Returns:
        A new image with the tiles in place. The image is returned as is when
        the seed is out of range or the image is too small to be tiled.
    """
    width, height = image.size
    if not (_SEED_MIN <= seed <= _SEED_MAX) or width < grid * UNIT or height < grid * UNIT:
        return image
    tile_width = width // UNIT // grid * UNIT
    tile_height = height // UNIT // grid * UNIT
    out = image.copy()
    for destination, source in enumerate(tile_order(seed, grid)):
        sx, sy = source % grid * tile_width, source // grid * tile_height
        dx, dy = destination % grid * tile_width, destination // grid * tile_height
        out.paste(image.crop((sx, sy, sx + tile_width, sy + tile_height)), (dx, dy))
    return out


class Nora(Extractor):
    """Fetch episodes from Comic NORA."""

    NAME = "nora"
    HOSTS = ("nora.gakken.jp",)
    URL_FORMS = (
        "https://nora.gakken.jp/comic/page-<slug>/?episode_id=<id>",
        "https://nora.gakken.jp/comic/page-<slug>/",
    )

    @classmethod
    def suitable(cls, url: str) -> bool:
        """Report whether the extractor takes `url`.

        Args:
            url: The URL to check.

        Returns:
            True for an https work page on a known host, with or without `?episode_id=`.
        """
        return super().suitable(url) and _WORK_PATH.match(urlparse(url).path) is not None

    @classmethod
    def is_series(cls, url: str) -> bool:
        """Report whether `url` is a work page without an episode picked.

        Args:
            url: The URL to check.

        Returns:
            True for `/comic/page-<slug>/` with no `episode_id` query.
        """
        parsed = urlparse(url)
        return _WORK_PATH.match(parsed.path) is not None and _episode_id(parsed.query) is None

    def series_urls(self, url: str) -> list[str]:
        """List every readable episode a work page links to, oldest first.

        Args:
            url: A work page URL.

        Returns:
            One `?episode_id=` URL per linked episode, oldest first, deduplicated.

        Raises:
            UnsupportedUrlError: The URL is not a work page.
            NotAnEpisodePageError: The page is not a work page, or links no episode.
        """
        if not self.is_series(url):
            msg = f"{url} is not a work page."
            raise UnsupportedUrlError(msg)
        work_url = _work_url(url)
        soup = BeautifulSoup(self._get(work_url, params={"orderby": "asc"}).content, "html.parser")
        if soup.find("article", class_="page-comic-detail") is None:
            msg = f"no work page at {url}."
            raise NotAnEpisodePageError(msg)
        urls: list[str] = []
        for anchor in soup.select("ul.article-list a[href]"):
            episode_id = _episode_id(urlparse(str(anchor["href"])).query)
            episode_url = _episode_url(work_url, episode_id)
            if episode_id is not None and episode_url not in urls:
                urls.append(episode_url)
        if not urls:
            msg = f"the work at {url} lists no readable episode."
            raise NotAnEpisodePageError(msg)
        return urls

    def episode(self, url: str) -> Episode:
        """Read one episode and list its pages.

        Args:
            url: A work page URL with `?episode_id=`; without it, the episode
                the work page embeds (its latest).

        Returns:
            The episode. `pages` is empty when the episode is not published
            (the work page embeds no viewer, or the API says it is
            unreleased); `next_url` is what the API names as the next one.

        Raises:
            NotAnEpisodePageError: The page is not a work page at all.
        """
        work_url = _work_url(url)
        wanted = _episode_id(urlparse(url).query)
        params = {"episode_id": wanted} if wanted is not None else None
        soup = BeautifulSoup(self._get(work_url, params=params).content, "html.parser")
        article = soup.find("article", class_="page-comic-detail")
        if not isinstance(article, Tag):
            msg = f"no work page at {url}."
            raise NotAnEpisodePageError(msg)

        embedded = _embedded_episode_id(article)
        episode_id = wanted if wanted is not None else embedded
        if episode_id is None:
            msg = f"the work page at {url} embeds no episode."
            raise NotAnEpisodePageError(msg)
        episode_url = _episode_url(work_url, episode_id)
        series_title = _text(article.find(class_="page-comic-detail-content-header-title")) or _site_title(soup)
        episode_title = " ".join(
            filter(
                None,
                (
                    _text(article.find(class_="page-comic-detail-episode-title-text__sub")),
                    _text(article.find(class_="page-comic-detail-episode-title-text__main")),
                ),
            ),
        )
        metadata: dict[str, Any] = {
            "episode_id": episode_id,
            "published": embedded == episode_id,
            "date": _text(article.find(class_="page-comic-detail-episode-title__date")),
            "work_url": work_url,
        }
        if embedded != episode_id:
            # `非公開`, or no such episode: the work page comes back with an empty viewer.
            return Episode(
                url=episode_url,
                series_title=series_title,
                episode_title=episode_title or str(episode_id),
                metadata={**metadata, "viewer": None},
            )

        viewer = self._viewer(work_url, episode_id)
        metadata["viewer"] = viewer
        following = viewer.get("next_episode") or {}
        next_id = following.get("episode_id") if isinstance(following, dict) else None
        seed = viewer.get("scramble_seed")
        extra = {"seed": seed} if isinstance(seed, int) and _SEED_MIN <= seed <= _SEED_MAX else {}
        return Episode(
            url=episode_url,
            series_title=series_title,
            episode_title=episode_title or str(viewer.get("episode_name") or episode_id),
            pages=tuple(Page(url=str(src), extra=extra) for src in viewer.get("page_list") or []),
            next_url=_episode_url(work_url, int(next_id)) if next_id is not None else None,
            metadata=metadata,
        )

    def image(self, page: Page, episode: Episode) -> Image.Image:
        """Fetch one page and unscramble it when the episode has a seed.

        Args:
            page: The page to fetch.
            episode: The episode the page belongs to.

        Returns:
            The page image.
        """
        image = self._fetch_image(page.url, headers={**self.HEADERS, "Referer": episode.url})
        seed = page.extra.get("seed")
        return descramble(image, int(seed)) if seed is not None else image

    def _viewer(self, work_url: str, episode_id: int) -> dict[str, Any]:
        """Ask the API for the viewer data of an episode.

        Returns an empty dict when the API refuses the episode (unreleased,
        missing), so the caller reports it as locked instead of failing.
        """
        origin = urlparse(work_url)
        params = {"version": _APP_VERSION, "platform": _PLATFORM, "episode_id": str(episode_id)}
        headers = {
            **self.HEADERS,
            _HASH_HEADER: service_hash(params),
            _CRAWLER_HEADER: "false",
            "Origin": f"{origin.scheme}://{origin.netloc}",
            "Referer": f"{origin.scheme}://{origin.netloc}/",
        }
        res = self._session.get(
            f"{origin.scheme}://api.{origin.netloc}/web/episode/viewer",
            params=params,
            headers=headers,
            timeout=self.TIMEOUT,
        )
        if res.status_code != HTTPStatus.BAD_REQUEST:
            res.raise_for_status()
        data = res.json()
        if not isinstance(data, dict) or data.get("status") != "success":
            return {}
        return data


def _episode_id(query: str) -> int | None:
    """The `episode_id` of a query string, or None without one."""
    values = parse_qs(query).get("episode_id", [])
    return int(values[0]) if values and values[0].isdigit() else None


def _work_url(url: str) -> str:
    """`https://<host>/comic/page-<slug>/`, whatever the query or the trailing slash of `url`."""
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}/"


def _episode_url(work_url: str, episode_id: int | None) -> str:
    return f"{work_url}?{urlencode({'episode_id': episode_id})}"


def _embedded_episode_id(article: Tag) -> int | None:
    """The episode the work page's viewer iframe shows, or None when there is none."""
    iframe = article.find("iframe", id="viewer-iframe")
    if not isinstance(iframe, Tag):
        return None
    match = re.search(r"/episode/(\d+)/", str(iframe.get("src", "")))
    return int(match.group(1)) if match else None


def _site_title(soup: BeautifulSoup) -> str:
    """The `<title>` up to the ` | <site name>` suffix."""
    return _text(soup.title).split(" | ", 1)[0]


def _text(tag: Tag | None) -> str:
    return " ".join(tag.get_text(" ", strip=True).split()) if isinstance(tag, Tag) else ""
