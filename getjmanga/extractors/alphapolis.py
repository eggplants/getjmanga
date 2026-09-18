"""アルファポリス (Alphapolis) web manga, and the CSS puzzle its viewer scrambles pages with.

The site hosts official serialisations under `/manga/official/<id>` and
user-submitted works under `/manga/<user>/<id>`; both open the same Vue viewer,
which POSTs to a `viewer.json` endpoint for the page list. Official pages come
cut into tiles that are shuffled, flipped and rotated; the viewer puts them back
with CSS, driven by a table it decodes from a fake PNG data URL (`placeholder`)
in a WebAssembly module. `descramble()` is a port of that module.
"""

from __future__ import annotations

import base64
import json
import math
import re
import struct
from dataclasses import dataclass
from http import HTTPStatus
from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import unquote, urljoin, urlparse

from bs4 import BeautifulSoup
from bs4.element import Tag
from PIL import Image

from .common import Episode, Extractor, LoginError, NotAnEpisodePageError, Page, UnsupportedUrlError

if TYPE_CHECKING:
    from collections.abc import Iterator

    from requests import Session

BASE_URL = "https://www.alphapolis.co.jp"
LOGIN_URL = f"{BASE_URL}/login"

_OFFICIAL_WORK = re.compile(r"^/manga/official/(?P<manga>\d+)/?$")
_OFFICIAL_EPISODE = re.compile(r"^/manga/official/(?P<manga>\d+)/(?P<episode>\d+)/?$")
_USER_WORK = re.compile(r"^/manga/(?P<user>\d+)/(?P<manga>\d+)/?$")
_USER_EPISODE = re.compile(r"^/manga/(?P<user>\d+)/(?P<manga>\d+)/episode/(?P<episode>\d+)/?$")

#: Laravel hands the CSRF token out in this cookie and wants it back in this header.
_XSRF_COOKIE = "XSRF-TOKEN"
_XSRF_HEADER = "X-XSRF-TOKEN"

#: The `placeholder` is a PNG signature and IHDR chunk with the puzzle table appended.
_PNG_HEADER_SIZE = 33
#: Each piece of the puzzle table is packed into this many bytes.
_PIECE_SIZE = 8


@dataclass(frozen=True)
class Piece:
    """One tile of a scrambled page, and where it belongs."""

    #: Top-left corner of the tile's content in the unscrambled page.
    x: int
    y: int
    #: Top-left corner of the tile in the image as served.
    source_x: int
    source_y: int
    #: Size of the tile as served, border included, before it is turned.
    source_width: int
    source_height: int
    #: Quarter turns counterclockwise to give the tile.
    rotation: int
    #: Whether to mirror the tile left-to-right before turning it.
    flipped: bool
    #: Paint order; a later tile covers the border of an earlier one.
    order: int


@dataclass(frozen=True)
class Puzzle:
    """The puzzle table of one page, as the viewer's module decodes it."""

    #: Size of the unscrambled page.
    width: int
    height: int
    #: Width of the border each tile carries around its content.
    border: int
    pieces: tuple[Piece, ...]


# ---------------------------------------------------------------------------
# Descrambling
#
# A scrambled page is a grid of square tiles, each carrying a `border`-pixel
# frame around `tile - 2 * border` pixels of content, so the served image is
# a little larger than the page. The table names, for every tile, where its
# content goes, where it sits in the served image, how it was turned and
# whether it was mirrored. The viewer keys nothing by `iv` and `salt`: they
# only obfuscate the CSS custom properties it renders the table into.
# ---------------------------------------------------------------------------


def parse_puzzles(placeholder: str) -> list[str]:
    """Split the viewer's `placeholder` into one puzzle table per page.

    Args:
        placeholder: The `page.placeholder` data URL of a viewer response.

    Returns:
        The hex-encoded table of each page, in page order; an empty string
        for a page that is not scrambled.
    """
    _, _, payload = placeholder.partition(",")
    raw = base64.b64decode(payload)[_PNG_HEADER_SIZE:]
    tables: list[str] = []
    offset = 0
    while offset + 2 <= len(raw):
        (count,) = struct.unpack_from("<H", raw, offset)
        offset += 2
        tables.append(raw[offset : offset + count * _PIECE_SIZE].hex())
        offset += count * _PIECE_SIZE
    return tables


def parse_puzzle(table: bytes, width: int, height: int) -> Puzzle:
    """Unpack a page's puzzle table against the size of the image as served.

    Args:
        table: The page's table, as `parse_puzzles` returns it (decoded).
        width: Width of the served image.
        height: Height of the served image.

    Returns:
        The puzzle. It has no pieces when the table is empty.
    """
    if len(table) < _PIECE_SIZE:
        return Puzzle(width=width, height=height, border=0, pieces=())

    (head,) = struct.unpack_from("<I", table, 0)
    tile = table[7]
    border = (head >> 27) & 7
    content = tile - 2 * border
    columns = math.ceil(width / tile)
    rows = math.ceil(height / tile)
    page_width = width - columns * 2 * border
    page_height = height - rows * 2 * border

    pieces: list[Piece] = []
    for offset in range(0, len(table) - _PIECE_SIZE + 1, _PIECE_SIZE):
        placement, source = struct.unpack_from("<II", table, offset)
        x = (placement >> 15) & 0xFFF
        y = (placement >> 3) & 0xFFF
        column, row = x // content, y // content
        piece_width = (page_width - x if column == columns - 1 else content) + 2 * border
        piece_height = (page_height - y if row == rows - 1 else content) + 2 * border
        rotation = (placement >> 1) & 3
        turned = rotation % 2 == 1
        pieces.append(
            Piece(
                x=x,
                y=y,
                source_x=((source >> 16) & 0xFF) * tile,
                source_y=((source >> 8) & 0xFF) * tile,
                source_width=piece_height if turned else piece_width,
                source_height=piece_width if turned else piece_height,
                rotation=rotation,
                flipped=bool(placement & 1),
                order=columns * row + column,
            ),
        )
    return Puzzle(width=page_width, height=page_height, border=border, pieces=tuple(pieces))


def descramble(image: Image.Image, table: str) -> Image.Image:
    """Put a scrambled page back together.

    Args:
        image: The page exactly as the CDN serves it.
        table: The page's hex-encoded puzzle table, from `parse_puzzles`.

    Returns:
        A new image with the tiles back where they belong, or the image
        itself when the table is empty.
    """
    puzzle = parse_puzzle(bytes.fromhex(table), *image.size)
    if not puzzle.pieces:
        return image
    out = Image.new(image.mode, (puzzle.width, puzzle.height))
    border = puzzle.border
    for piece in sorted(puzzle.pieces, key=lambda piece: piece.order):
        tile = image.crop(
            (piece.source_x, piece.source_y, piece.source_x + piece.source_width, piece.source_y + piece.source_height),
        )
        if piece.flipped:
            tile = tile.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
        if piece.rotation:
            tile = tile.rotate(90 * piece.rotation, expand=True)
        # The viewer clips the top and left border off and lets the piece to
        # the right and below paint over the other two edges.
        out.paste(tile.crop((border, border, tile.width, tile.height)), (piece.x, piece.y))
    return out


@dataclass(frozen=True)
class Work:
    """What a work page says about its episodes."""

    title: str
    #: Episode URLs in reading order.
    urls: tuple[str, ...]
    #: The title of each episode, by URL.
    titles: dict[str, str]


class AlphaPolis(Extractor):
    """Fetch episodes from アルファポリス's manga section."""

    NAME = "alphapolis"
    HOSTS = ("www.alphapolis.co.jp",)
    URL_FORMS = (
        "https://www.alphapolis.co.jp/manga/official/<manga-id>/<episode-no>",
        "https://www.alphapolis.co.jp/manga/official/<manga-id>",
        "https://www.alphapolis.co.jp/manga/<user-id>/<manga-id>/episode/<episode-no>",
        "https://www.alphapolis.co.jp/manga/<user-id>/<manga-id>",
    )
    CONFIG_KEY = "alphapolis"
    HEADERS: ClassVar[dict[str, str]] = {**Extractor.HEADERS, "Referer": f"{BASE_URL}/"}

    def __init__(self, session: Session | None = None) -> None:
        """Build an extractor.

        Args:
            session: A session to reuse. A retrying one is made when omitted.
        """
        super().__init__(session)
        self._works: dict[str, Work] = {}

    @classmethod
    def suitable(cls, url: str) -> bool:
        """Report whether `url` is a manga work or episode page.

        Args:
            url: The URL to check.

        Returns:
            True for an https official or user-submitted manga work or
            episode URL; novels and the section's index pages are not taken.
        """
        if not super().suitable(url):
            return False
        path = urlparse(url).path
        return any(pattern.match(path) for pattern in (_OFFICIAL_WORK, _OFFICIAL_EPISODE, _USER_WORK, _USER_EPISODE))

    @classmethod
    def is_series(cls, url: str) -> bool:
        """Report whether `url` is a work page.

        Args:
            url: The URL to check.

        Returns:
            True for `/manga/official/<id>` and `/manga/<user>/<id>`.
        """
        return cls.suitable(url) and cls.parse_uri(url)[1] is None

    @staticmethod
    def parse_uri(url: str) -> tuple[str, str | None]:
        """Split a work or episode URL into the work URL and the episode number.

        Args:
            url: A work or episode URL.

        Returns:
            The work URL, and the episode number when the URL names one.

        Raises:
            UnsupportedUrlError: The URL is not one this extractor reads.
        """
        parsed = urlparse(url)
        if parsed.hostname in AlphaPolis.HOSTS:
            path = parsed.path
            if match := _OFFICIAL_EPISODE.match(path):
                return f"{BASE_URL}/manga/official/{match['manga']}", match["episode"]
            if match := _OFFICIAL_WORK.match(path):
                return f"{BASE_URL}/manga/official/{match['manga']}", None
            if match := _USER_EPISODE.match(path):
                return f"{BASE_URL}/manga/{match['user']}/{match['manga']}", match["episode"]
            if match := _USER_WORK.match(path):
                return f"{BASE_URL}/manga/{match['user']}/{match['manga']}", None
        msg = f"'{url}' is not an alphapolis manga work or episode url."
        raise UnsupportedUrlError(msg)

    def series_urls(self, url: str) -> list[str]:
        """List a work's episodes, in reading order.

        Args:
            url: A work URL.

        Returns:
            The URL of every listed episode, locked ones included.

        Raises:
            NotAnEpisodePageError: The work lists no episode.
        """
        work_url, episode_no = self.parse_uri(url)
        if episode_no is not None:
            msg = f"{url} is an episode page, not a work page."
            raise UnsupportedUrlError(msg)
        work = self.work(work_url)
        if not work.urls:
            msg = f"the work at {url} lists no episode."
            raise NotAnEpisodePageError(msg)
        return list(work.urls)

    def episode(self, url: str) -> Episode:
        """Read the page list and the titles of one episode.

        Args:
            url: The episode URL.

        Returns:
            The episode. `pages` is empty when it wants a rental or a login.

        Raises:
            NotAnEpisodePageError: The page carries no viewer.
        """
        work_url, episode_no = self.parse_uri(url)
        if episode_no is None:
            msg = f"{url} is a work page; list it with series_urls()."
            raise UnsupportedUrlError(msg)

        res = self._session.get(url, headers=self.HEADERS, timeout=self.TIMEOUT)
        # An episode the session may not open answers with a rental page and a 403.
        if res.status_code == HTTPStatus.FORBIDDEN:
            return self._locked_episode(url, work_url)
        if res.status_code == HTTPStatus.NOT_FOUND:
            msg = f"{url} names no episode (HTTP 404)."
            raise NotAnEpisodePageError(msg)
        res.raise_for_status()

        config = _viewer_config(res.text)
        if config is None:
            msg = f"no viewer on {url}; is it an alphapolis manga episode?"
            raise NotAnEpisodePageError(msg)

        data = self._viewer(url, config)
        page = data.get("page")
        if not isinstance(page, dict):
            # `{"isAccessDenied": true}` or `{"isExpired": true}`: not rented.
            return self._locked_episode(url, work_url)

        manga = data.get("manga") or {}
        episode = data.get("episode") or {}
        tables = parse_puzzles(str(page.get("placeholder") or ""))
        images = page.get("images") or []
        return Episode(
            url=url,
            series_title=str(manga.get("title") or "").strip() or work_url.rsplit("/", 1)[-1],
            episode_title=str(episode.get("mainTitle") or episode.get("title") or "").strip() or episode_no,
            pages=tuple(
                Page(
                    url=str(image["url"]),
                    width=int(image.get("width") or 0),
                    height=int(image.get("height") or 0),
                    extra={"puzzle": tables[index] if index < len(tables) else ""},
                )
                for index, image in enumerate(images)
            ),
            next_url=_next_url(url, [urljoin(BASE_URL, str(entry["url"])) for entry in data.get("episodes") or []]),
            metadata={"manga": manga, "episode": episode, "size": page.get("size") or {}},
        )

    def image(self, page: Page, episode: Episode) -> Image.Image:
        """Fetch one page and put its tiles back where they belong.

        Args:
            page: The page to fetch.
            episode: The episode the page belongs to.

        Returns:
            The page in reading order.
        """
        image = self._fetch_image(page.url, headers={**self.HEADERS, "Referer": episode.url})
        table = str(page.extra.get("puzzle") or "")
        return descramble(image, table) if table else image

    def login(self, url: str, username: str, password: str) -> None:  # noqa: ARG002 (one sign-in page)
        """Sign in, so rented episodes become readable.

        The sign-in form is a plain Laravel one: a `_token` off the page goes
        back with the credentials, and the session cookie lands on the shared
        session. This grants nothing the account does not already own.

        Args:
            url: Ignored; the site has one sign-in page.
            username: The email address the account uses.
            password: The account's password.

        Raises:
            LoginError: The site refused the credentials.
        """
        form = self._get(LOGIN_URL)
        token = BeautifulSoup(form.content, "html.parser").find("input", attrs={"name": "_token"})
        if not isinstance(token, Tag):
            msg = f"{LOGIN_URL} carries no sign-in form."
            raise LoginError(msg)

        res = self._session.post(
            LOGIN_URL,
            data={"_token": str(token.attrs.get("value", "")), "email": username, "password": password},
            headers={**self.HEADERS, "Origin": BASE_URL, "Referer": LOGIN_URL},
            timeout=self.TIMEOUT,
        )
        res.raise_for_status()

        # A refusal bounces back to the form with a flash message; a success
        # lands on a page whose header knows the account.
        soup = BeautifulSoup(res.content, "html.parser")
        if urlparse(res.url).path.rstrip("/") == "/login" or not _signed_in(soup):
            flash = soup.find(class_="flash-message")
            reason = " ".join(flash.get_text().split()) if isinstance(flash, Tag) else "no reason given"
            msg = f"{BASE_URL} refused the credentials for {username!r}: {reason}"
            raise LoginError(msg)

    def work(self, work_url: str) -> Work:
        """Read a work page's episode list and title.

        The result is cached, so walking a whole work costs one request for
        the list however many episodes are downloaded off it.

        Args:
            work_url: The work URL, as `parse_uri` returns it.

        Returns:
            The work.

        Raises:
            NotAnEpisodePageError: The page carries no episode list.
        """
        cached = self._works.get(work_url)
        if cached is not None:
            return cached

        res = self._get(work_url)
        soup = BeautifulSoup(res.content, "html.parser")
        official = urlparse(work_url).path.startswith("/manga/official/")
        listing = _embedded_json(soup, "app-official-manga-toc" if official else "app-cover-data")
        if listing is None:
            msg = f"no episode list on {work_url}; is it an alphapolis manga work?"
            raise NotAnEpisodePageError(msg)

        if official:
            title = str(listing.get("mangaTitle") or "")
            entries = listing.get("episodes") or []
        else:
            title = str((listing.get("content") or {}).get("title") or "")
            entries = list(_chapter_entries(listing.get("chapterEpisodes") or []))

        urls: list[str] = []
        titles: dict[str, str] = {}
        for entry in entries:
            if not isinstance(entry, dict) or not entry.get("url"):
                continue
            url = urljoin(BASE_URL, str(entry["url"]))
            if url not in titles:
                urls.append(url)
            titles[url] = str(entry.get("mainTitle") or entry.get("shortTitle") or "").strip()
        work = Work(title=title.strip() or work_url.rsplit("/", 1)[-1], urls=tuple(urls), titles=titles)
        self._works[work_url] = work
        return work

    def _viewer(self, url: str, config: dict[str, Any]) -> dict[str, Any]:
        """POST the viewer's page-list request the way the Vue app does."""
        endpoint = urljoin(url, str((config.get("urls") or {}).get("getViewer") or "/manga/official/viewer.json"))
        payload: dict[str, Any] = {
            "manga_sele_id": (config.get("manga") or {}).get("mangaId"),
            "episode_no": (config.get("episode") or {}).get("episodeNo"),
            "resolution": "full_hd",
            "hide_page": bool(config.get("isPageImageHidden")),
            "preview": bool(config.get("isPreview")),
        }
        if config.get("viewerInfoData") is not None:
            payload["data"] = config["viewerInfoData"]
        res = self._session.post(
            endpoint,
            json=payload,
            headers={
                **self.HEADERS,
                "Origin": BASE_URL,
                "Referer": url,
                "X-Requested-With": "XMLHttpRequest",
                _XSRF_HEADER: self._xsrf_token(),
            },
            timeout=self.TIMEOUT,
        )
        res.raise_for_status()
        data = res.json()
        return data if isinstance(data, dict) else {}

    def _xsrf_token(self) -> str:
        """The CSRF token the episode page's response set as a cookie."""
        return unquote(self._cookie(_XSRF_COOKIE, self.HOSTS[0]) or "")

    def _locked_episode(self, url: str, work_url: str) -> Episode:
        """Describe an episode the site would not open, from its work's list."""
        work = self.work(work_url)
        return Episode(
            url=url,
            series_title=work.title,
            episode_title=work.titles.get(url) or url.rsplit("/", 1)[-1],
            next_url=_next_url(url, work.urls),
            metadata={"locked": True},
        )


def _viewer_config(html: str) -> dict[str, Any] | None:
    """Read the viewer's JSON configuration off an episode page."""
    return _embedded_json(BeautifulSoup(html, "html.parser"), "app-manga-viewer")


def _embedded_json(soup: BeautifulSoup, element_id: str) -> dict[str, Any] | None:
    """Read the `application/json` script the Vue app under `element_id` mounts with."""
    holder = soup.find(id=element_id)
    if not isinstance(holder, Tag):
        return None
    script = holder if holder.name == "script" else holder.find("script", attrs={"type": "application/json"})
    if not isinstance(script, Tag) or not script.string:
        return None
    try:
        data = json.loads(script.string)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _chapter_entries(chapters: list[Any]) -> Iterator[dict[str, Any]]:
    """Flatten a user work's chapters into its episodes, extras last in each."""
    for chapter in chapters:
        if not isinstance(chapter, dict):
            continue
        yield from (entry for entry in chapter.get("episodes") or [] if isinstance(entry, dict))
        extra = chapter.get("extraEpisode")
        if isinstance(extra, dict):
            yield extra


def _next_url(url: str, urls: list[str] | tuple[str, ...]) -> str | None:
    """The episode after `url` in a work's list, or None at its end."""
    normalised = [candidate.rstrip("/") for candidate in urls]
    try:
        position = normalised.index(url.rstrip("/"))
    except ValueError:
        return None
    return urls[position + 1] if position + 1 < len(urls) else None


def _signed_in(soup: BeautifulSoup) -> bool:
    """Whether a page's header names a signed-in account."""
    data = _embedded_json(soup, "app-login-account-data")
    return bool(data and data.get("loginAccount"))
