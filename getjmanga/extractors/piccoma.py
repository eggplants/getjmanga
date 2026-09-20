"""Piccoma, and the seedrandom shuffle its viewer scrambles pages with."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Literal
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup
from bs4.element import Tag

from getjmanga.errors import GetjmangaError, LoginError, NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractor import Episode, Extractor, Page

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from PIL import Image
    from requests import Session

BASE_URL = "https://piccoma.com"
LOGIN_URL = f"{BASE_URL}/web/acc/email/signin"

#: What `eType` in the viewer's `_pdata_` calls the two kinds of listing.
EpisodeType = Literal["E", "V"]

#: The viewer slices every page into a grid of squares this wide and shuffles them.
TILE_SIZE = 50

# Piccoma bounces an episode the session may not open to its sign-in page.
_SIGNIN_PREFIX = "/web/acc/"

_VIEWER_PATH = re.compile(r"^/web/viewer/(\d+)/(\d+)/?$")
_PRODUCT_PATH = re.compile(r"^/web/product/(\d+)(?:/episodes)?/?$")

# `_pdata_` is a JavaScript object literal, not JSON: single-quoted strings,
# bare keys and trailing commas. Only the parts that matter are read out of it.
_PDATA = re.compile(r"var\s+_pdata_\s*=\s*\{(.*?)\n\s*\}", re.DOTALL)
_PDATA_FIELD = re.compile(r"'(?P<key>\w+)'\s*:\s*(?P<value>'[^']*'|true|false|-?\d+)")
_PDATA_IMAGE = re.compile(
    r"'path'\s*:\s*'(?P<path>[^']+)'"
    r"(?:[^{}]*?'width'\s*:\s*(?P<width>\d+))?"
    r"(?:[^{}]*?'height'\s*:\s*(?P<height>\d+))?",
)
_LOGIN_FLAG = re.compile(r"'login'\s*:\s*(?P<value>true|false)")

# Indices `_seed_key` leaves alone below index 10, and the ones it always flips above it.
_KEPT_BELOW_TEN = frozenset({3, 4, 5, 8})
_FLIPPED_ABOVE_TEN = frozenset({13, 14, 16})


@dataclass(frozen=True)
class Entry:
    """One row of a product's episode or volume list."""

    id: str
    title: str
    url: str


# ---------------------------------------------------------------------------
# Descrambling
#
# The viewer shuffles the tiles of a page with the seedrandom PRNG (an ARC4
# stream keyed by a seed string), so putting a page back together means running
# the same PRNG over the same seed and replaying the shuffle. The seed is
# derived from the image URL alone; `parse_seed` does that derivation.
# ---------------------------------------------------------------------------

_ARC4_WIDTH = 256
_ARC4_MASK = _ARC4_WIDTH - 1
_PRNG_CHUNKS = 6
_PRNG_STARTDENOM = _ARC4_WIDTH**_PRNG_CHUNKS
_PRNG_SIGNIFICANCE = 2**52
_PRNG_OVERFLOW = _PRNG_SIGNIFICANCE * 2


class _ARC4:
    """The ARC4 keystream seedrandom draws its bits from."""

    def __init__(self, key: list[int]) -> None:
        self._state = list(range(_ARC4_WIDTH))
        self._i = 0
        self._j = 0

        j = 0
        for i in range(_ARC4_WIDTH):
            swapped = self._state[i]
            j = _ARC4_MASK & (j + key[i % len(key)] + swapped)
            self._state[i] = self._state[j]
            self._state[j] = swapped
        # seedrandom discards a full round before handing any bits out.
        self.generate(_ARC4_WIDTH)

    def generate(self, count: int) -> int:
        """Return `count` keystream bytes packed into one big-endian integer."""
        state, i, j, result = self._state, self._i, self._j, 0
        for _ in range(count):
            i = _ARC4_MASK & (i + 1)
            swapped = state[i]
            j = _ARC4_MASK & (j + swapped)
            state[i] = state[j]
            state[j] = swapped
            result = result * _ARC4_WIDTH + state[_ARC4_MASK & (state[i] + state[j])]
        self._i, self._j = i, j
        return result


def _mixkey(seed: str) -> list[int]:
    """Fold a seed string into an ARC4 key the way seedrandom does."""
    key: list[int] = []
    for index, char in enumerate(seed):
        key.insert(_ARC4_MASK & index, ord(char))
    return key


def seedrandom(seed: str) -> Callable[[], float]:
    """Build the seeded PRNG the viewer shuffles its tiles with.

    Args:
        seed: The seed string, as `parse_seed` returns it.

    Returns:
        A callable handing out floats in `[0, 1)`.

    Raises:
        GetjmangaError: The seed is empty, which keys nothing.
    """
    if not seed:
        msg = "cannot seed the shuffle with an empty string."
        raise GetjmangaError(msg)
    arc4 = _ARC4(_mixkey(seed))

    def prng() -> float:
        numerator: float = arc4.generate(_PRNG_CHUNKS)
        denominator: float = _PRNG_STARTDENOM
        extra = 0
        # Pull more bytes until the fraction carries a full mantissa of them,
        # then halve it back under the range a double represents exactly.
        while numerator < _PRNG_SIGNIFICANCE:
            numerator = (numerator + extra) * _ARC4_WIDTH
            denominator *= _ARC4_WIDTH
            extra = arc4.generate(1)
        while numerator >= _PRNG_OVERFLOW:
            numerator /= 2
            denominator /= 2
            extra >>= 1
        return (numerator + extra) / denominator

    return prng


def shuffle_order(size: int, seed: str) -> list[int]:
    """Replay the viewer's shuffle of `size` tiles.

    Args:
        size: How many tiles are being shuffled.
        seed: The seed string, as `parse_seed` returns it.

    Returns:
        The source tile index for each destination tile.
    """
    prng = seedrandom(seed)
    remaining = list(range(size))
    return [remaining.pop(math.floor(prng() * len(remaining))) for _ in range(size)]


def _seed_key(checksum: str) -> str:
    """Flip the low bit of the bytes of `checksum` that the viewer flips.

    The viewer leaves a handful of positions alone, so the transform is a fixed
    pattern of positions rather than anything derived from the value itself.
    """
    last = len(checksum) - 1
    out = bytearray()
    for index, code in enumerate(checksum.encode()):
        if index < 10:  # noqa: PLR2004 (the viewer's own boundary)
            flip = index not in _KEPT_BELOW_TEN
        elif index in _FLIPPED_ABOVE_TEN:
            flip = True
        else:
            flip = index in (last, last - 1)
        out.append(code ^ 1 if flip else code)
    return out.decode()


def parse_seed(image_url: str) -> str | None:
    """Work out the seed a page image was shuffled with.

    The CDN path carries a per-episode checksum, which the viewer rotates right
    by every non-zero digit of the URL's `expires` stamp and then perturbs.
    Unscrambled pages are served under a lowercase checksum instead.

    Args:
        image_url: The page's image URL, `expires` query and all.

    Returns:
        The seed string, or None when the image is not scrambled.

    Raises:
        GetjmangaError: The URL carries no checksum or no `expires` stamp.
    """
    parsed = urlparse(image_url)
    segments = [segment for segment in parsed.path.split("/") if segment]
    if len(segments) < 2:  # noqa: PLR2004 (a checksum and a file name)
        msg = f"{image_url!r} carries no checksum path segment."
        raise GetjmangaError(msg)
    checksum = segments[-2]

    expires = parse_qs(parsed.query).get("expires")
    if not expires:
        msg = f"{image_url!r} carries no 'expires' stamp to rotate the checksum by."
        raise GetjmangaError(msg)

    for digit in expires[0]:
        shift = int(digit) if digit.isdigit() else 0
        if shift:
            checksum = checksum[-shift:] + checksum[:-shift]

    return _seed_key(checksum) if checksum.isupper() else None


def _tile_groups(width: int, height: int, tile_size: int) -> Iterator[list[tuple[int, int]]]:
    """Group the tiles of an image by shape, top-left corner first.

    Tiles along the right and bottom edge are cut short, and the viewer shuffles
    each differently shaped group among itself rather than all tiles as one.
    """
    columns = math.ceil(width / tile_size)
    rows = math.ceil(height / tile_size)
    groups: dict[tuple[int, int], list[tuple[int, int]]] = {}
    for index in range(columns * rows):
        row, column = divmod(index, columns)
        x, y = column * tile_size, row * tile_size
        shape = (min(tile_size, width - x), min(tile_size, height - y))
        groups.setdefault(shape, []).append((x, y))
    yield from groups.values()


def descramble(image: Image.Image, seed: str, tile_size: int = TILE_SIZE) -> Image.Image:
    """Put a shuffled page back together.

    Args:
        image: The page exactly as the CDN serves it.
        seed: The seed string, from `parse_seed`.
        tile_size: The side of a full tile.

    Returns:
        A new image with the tiles back where they belong.
    """
    out = image.copy()
    for corners in _tile_groups(*image.size, tile_size):
        tile_width = min(tile_size, image.width - corners[0][0])
        tile_height = min(tile_size, image.height - corners[0][1])
        group_x, group_y = corners[0]
        # A group's own grid is as wide as the run of tiles sharing its first row.
        group_columns = sum(1 for _, y in corners if y == group_y)

        for (dest_x, dest_y), source in zip(corners, shuffle_order(len(corners), seed), strict=True):
            row, column = divmod(source, group_columns)
            x, y = group_x + column * tile_width, group_y + row * tile_height
            out.paste(image.crop((x, y, x + tile_width, y + tile_height)), (dest_x, dest_y))
    return out


class Piccoma(Extractor):
    """Fetch episodes from Piccoma."""

    NAME = "piccoma"
    HOSTS = ("piccoma.com",)
    URL_FORMS = (
        "https://piccoma.com/web/viewer/<product-id>/<episode-id>",
        "https://piccoma.com/web/product/<product-id>/episodes",
        "https://piccoma.com/web/product/<product-id>/episodes?etype=V",
    )
    CONFIG_KEY = "piccoma"
    HEADERS: ClassVar[dict[str, str]] = {**Extractor.HEADERS, "Referer": f"{BASE_URL}/"}

    def __init__(self, session: Session | None = None) -> None:
        """Build an extractor.

        Args:
            session: A session to reuse. A retrying one is made when omitted.
        """
        super().__init__(session)
        self._logged_in = False
        self._lists: dict[tuple[str, EpisodeType], list[Entry]] = {}
        self._series_titles: dict[str, str] = {}

    @property
    def logged_in(self) -> bool:
        """Whether `login` has signed this extractor in."""
        return self._logged_in

    @classmethod
    def suitable(cls, url: str) -> bool:
        """Report whether `url` is a Piccoma viewer or product page.

        Args:
            url: The URL to check.

        Returns:
            True when the URL is an https Piccoma viewer or product URL.
        """
        if not super().suitable(url):
            return False
        path = urlparse(url).path
        return bool(_VIEWER_PATH.match(path) or _PRODUCT_PATH.match(path))

    @classmethod
    def is_series(cls, url: str) -> bool:
        """Report whether `url` is a product page.

        Args:
            url: The URL to check.

        Returns:
            True for `/web/product/<id>`, with or without `/episodes`.
        """
        return cls.suitable(url) and _PRODUCT_PATH.match(urlparse(url).path) is not None

    @staticmethod
    def parse_uri(url: str) -> tuple[str, str | None]:
        """Split a Piccoma URL into the ids it names.

        Args:
            url: A viewer or product URL.

        Returns:
            The series' id, and the episode's id when the URL names one.

        Raises:
            UnsupportedUrlError: The URL is not one this extractor reads.
        """
        parsed = urlparse(url)
        if parsed.hostname in Piccoma.HOSTS:
            viewer = _VIEWER_PATH.match(parsed.path)
            if viewer:
                return viewer.group(1), viewer.group(2)
            product = _PRODUCT_PATH.match(parsed.path)
            if product:
                return product.group(1), None
        msg = f"'{url}' is not a Piccoma viewer or product url."
        raise UnsupportedUrlError(msg)

    @staticmethod
    def viewer_url(product_id: str | int, episode_id: str | int) -> str:
        """Build the viewer URL of one episode.

        Args:
            product_id: The series' id.
            episode_id: The episode's id.

        Returns:
            The viewer URL.
        """
        return f"{BASE_URL}/web/viewer/{product_id}/{episode_id}"

    def series_urls(self, url: str) -> list[str]:
        """List a product's episodes, or its volumes with `?etype=V` on the URL.

        Args:
            url: A `/web/product/<id>` URL.

        Returns:
            The viewer URL of every row of the list, in reading order.

        Raises:
            NotAnEpisodePageError: The product lists nothing of that kind.
        """
        product_id, _ = self.parse_uri(url)
        etype = (parse_qs(urlparse(url).query).get("etype") or ["E"])[0].upper()
        episode_type: EpisodeType = "V" if etype.startswith("V") else "E"
        entries = self.entries(product_id, episode_type)
        if not entries:
            msg = f"the product at {url} lists no {'volumes' if episode_type == 'V' else 'episodes'}."
            raise NotAnEpisodePageError(msg)
        return [entry.url for entry in entries]

    def episode(self, url: str) -> Episode:
        """Read the page list and the titles off a viewer page.

        Args:
            url: The viewer URL.

        Returns:
            The episode. `pages` is empty when it is not readable.

        Raises:
            NotAnEpisodePageError: The page carries no viewer.
        """
        product_id, episode_id = self.parse_uri(url)
        if episode_id is None:
            msg = f"{url} is a product page; list it with series_urls()."
            raise UnsupportedUrlError(msg)

        res = self._get(url)
        if urlparse(res.url).path.startswith(_SIGNIN_PREFIX):
            return self._locked_episode(url, product_id, episode_id)
        html = res.text

        block = _PDATA.search(html)
        if block is None:
            msg = f"no '_pdata_' on {url}; is it a Piccoma viewer page?"
            raise NotAnEpisodePageError(msg)
        body = block.group(1)
        fields = {match["key"]: _literal(match["value"]) for match in _PDATA_FIELD.finditer(body)}

        product_id = str(fields.get("product_id") or product_id)
        episode_id = str(fields.get("episode_id") or episode_id)
        episode_type: EpisodeType = "V" if fields.get("eType") == "V" else "E"
        scrambled = bool(fields.get("isScrambled"))

        return Episode(
            url=url,
            series_title=_title_part(BeautifulSoup(html, "html.parser"), 1) or product_id,
            episode_title=str(fields.get("title") or "").strip() or episode_id,
            pages=tuple(
                Page(url=path, width=width, height=height, extra={"scrambled": scrambled})
                for path, width, height in _pages(body)
            ),
            next_url=self._next_url(product_id, episode_id, episode_type),
            metadata={
                "product_id": product_id,
                "episode_id": episode_id,
                "episode_type": episode_type,
                "scrambled": scrambled,
            },
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
        seed = parse_seed(page.url) if page.extra.get("scrambled") else None
        return descramble(image, seed) if seed else image

    def login(self, url: str, username: str, password: str) -> None:  # noqa: ARG002 (one sign-in page)
        """Sign in, so episodes the account may read become readable.

        The sign-in form is a plain Django one: a CSRF token off the page goes
        back with the credentials, and the session cookie lands on the shared
        session. This grants nothing the account does not already own.

        Args:
            url: Ignored; Piccoma has one sign-in page.
            username: The email address the account uses.
            password: The account's password.

        Raises:
            LoginError: Piccoma refused the credentials.
        """
        form = self._get(LOGIN_URL)
        token = BeautifulSoup(form.content, "html.parser").find("input", attrs={"name": "csrfmiddlewaretoken"})
        if not isinstance(token, Tag):
            msg = f"{LOGIN_URL} carries no sign-in form."
            raise LoginError(msg)

        res = self._session.post(
            LOGIN_URL,
            data={
                "csrfmiddlewaretoken": str(token.attrs.get("value", "")),
                "next_url": "/web/",
                "email": username,
                "password": password,
            },
            headers={**self.HEADERS, "Origin": BASE_URL, "Referer": LOGIN_URL},
            timeout=self.TIMEOUT,
        )
        res.raise_for_status()

        if not self._login_status():
            msg = f"piccoma.com refused the credentials for {username!r}."
            raise LoginError(msg)
        self._logged_in = True

    def entries(self, product_id: str, episode_type: EpisodeType = "E") -> list[Entry]:
        """List a series' episodes or volumes, in reading order.

        The result is cached, so walking a whole series costs one request for
        the list however many episodes are downloaded off it.

        Args:
            product_id: The series' id.
            episode_type: `"E"` for the episode list, `"V"` for the volume one.

        Returns:
            The rows of the list. Empty when the product has none of that kind.
        """
        cached = self._lists.get((product_id, episode_type))
        if cached is not None:
            return cached

        res = self._get(f"{BASE_URL}/web/product/{product_id}/episodes?etype={episode_type}")
        soup = BeautifulSoup(res.content, "html.parser")
        # The product page names the series; the viewer page of a locked
        # episode does not, so it is worth keeping hold of.
        self._series_titles.setdefault(product_id, _title_part(soup, 0))
        listing = soup.find(id="js_volumeList" if episode_type == "V" else "js_episodeList")

        entries: list[Entry] = []
        if isinstance(listing, Tag):
            entries = [
                entry
                for item in listing.find_all("li", recursive=False)
                if isinstance(item, Tag) and (entry := _entry(item, product_id)) is not None
            ]
        self._lists[product_id, episode_type] = entries
        return entries

    def series_title(self, product_id: str) -> str:
        """Read a series' title off its product page.

        Args:
            product_id: The series' id.

        Returns:
            The title, or the id when the page does not name one.
        """
        if product_id not in self._series_titles:
            self.entries(product_id)
        return self._series_titles.get(product_id) or product_id

    def _locked_episode(self, url: str, product_id: str, episode_id: str) -> Episode:
        """Describe an episode Piccoma would not open, from its series' list."""
        entry = None
        for episode_type in ("E", "V"):
            entry = next((row for row in self.entries(product_id, episode_type) if row.id == episode_id), None)
            if entry is not None:
                break
        return Episode(
            url=url,
            series_title=self.series_title(product_id),
            episode_title=entry.title if entry else episode_id,
            next_url=self._next_url(product_id, episode_id, "E"),
            metadata={"product_id": product_id, "episode_id": episode_id, "episode_type": "E", "scrambled": False},
        )

    def _next_url(self, product_id: str, episode_id: str, episode_type: EpisodeType) -> str | None:
        """Find the episode that follows `episode_id` in its series' list."""
        if not product_id or not episode_id:
            return None
        entries = self.entries(product_id, episode_type)
        ids = [entry.id for entry in entries]
        try:
            position = ids.index(episode_id)
        except ValueError:
            return None
        return entries[position + 1].url if position + 1 < len(entries) else None

    def _login_status(self) -> bool:
        """Ask any page whether the session is signed in."""
        res = self._get(LOGIN_URL)
        flag = _LOGIN_FLAG.search(res.text)
        return flag is not None and flag["value"] == "true"


def _title_part(soup: BeautifulSoup, index: int) -> str:
    """Read one `｜`-separated part of a page's title.

    A viewer page is titled `"<episode>｜<series>｜ピッコマ"` and a product page
    `"<series>｜<blurb>｜<authors>"`, so which part names the series depends on
    which page it came off.
    """
    og = soup.find("meta", property="og:title")
    heading = str(og.attrs.get("content", "")) if isinstance(og, Tag) else ""
    if not heading and soup.title:
        heading = soup.title.get_text()
    parts = [part.strip() for part in heading.split("｜") if part.strip()]
    return parts[index] if len(parts) > index + 1 else ""


def _literal(value: str) -> str | int | bool:
    """Read one `_pdata_` value: a single-quoted string, a number or a bool."""
    if value == "true":
        return True
    if value == "false":
        return False
    if value.startswith("'"):
        return value[1:-1]
    return int(value)


def _pages(body: str) -> Iterator[tuple[str, int, int]]:
    """Read the `img` array of a `_pdata_` block, in reading order."""
    for match in _PDATA_IMAGE.finditer(body):
        path = match["path"]
        yield (
            f"https:{path}" if path.startswith("//") else path,
            int(match["width"] or 0),
            int(match["height"] or 0),
        )


def _entry(item: Tag, product_id: str) -> Entry | None:
    """Turn one `<li>` of an episode or volume list into an `Entry`."""
    link = item.find("a", attrs={"data-episode_id": True})
    if not isinstance(link, Tag):
        return None
    episode_id = str(link.attrs["data-episode_id"])

    heading = item.find("h2")
    title = heading.get_text(strip=True) if isinstance(heading, Tag) else episode_id
    # The row's badges (free, wait-ticket, purchased) are not read: whether an
    # episode opens is what `episode()` finds out, and a bulk run carries on
    # past one that does not.
    return Entry(id=episode_id, title=title or episode_id, url=Piccoma.viewer_url(product_id, episode_id))
