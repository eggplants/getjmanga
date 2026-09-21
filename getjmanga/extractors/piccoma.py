"""Piccoma, whose viewer shuffles page tiles with `shuffle-seed` over `seedrandom`."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Literal
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup
from bs4.element import Tag

from getjmanga.errors import GetjmangaError, LoginError, NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractor import Episode, Extractor, Page, neighbours, ordinal
from getjmanga.viewers.seedrandom import descramble as descramble_tiles

if TYPE_CHECKING:
    from collections.abc import Iterator

    from httpx import Client
    from PIL import Image

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
# The seed
#
# The viewer shuffles the tiles of a page with `shuffle-seed` over `seedrandom`
# (`viewers/seedrandom.py` replays both). The seed is derived from the image
# URL alone; `parse_seed` does that derivation.
# ---------------------------------------------------------------------------


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


class Piccoma(Extractor):
    """Fetch episodes from Piccoma."""

    NAME = "piccoma"
    HOSTS = ("piccoma.com",)
    PUBLISHER = "カカオピッコマ"
    URL_FORMS = (
        "https://piccoma.com/web/viewer/<product-id>/<episode-id>",
        "https://piccoma.com/web/product/<product-id>/episodes",
        "https://piccoma.com/web/product/<product-id>/episodes?etype=V",
    )
    CONFIG_KEY = "piccoma"
    HEADERS: ClassVar[dict[str, str]] = {**Extractor.HEADERS, "Referer": f"{BASE_URL}/"}

    def __init__(self, session: Client | None = None) -> None:
        """Build an extractor.

        Args:
            session: A session to reuse. A retrying one is made when omitted.
        """
        super().__init__(session)
        self._logged_in = False
        self._lists: dict[tuple[str, EpisodeType], list[Entry]] = {}
        self._series_titles: dict[str, str] = {}
        #: The authors a product's list page names in its title, by product id.
        self._writers: dict[str, str] = {}
        #: The publisher a product page names, by product id.
        self._publishers: dict[str, str] = {}

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
        if urlparse(str(res.url)).path.startswith(_SIGNIN_PREFIX):
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
            prev_url=self._neighbours(product_id, episode_id, episode_type)[0],
            next_url=self._neighbours(product_id, episode_id, episode_type)[1],
            metadata={
                "product_id": product_id,
                "episode_id": episode_id,
                "episode_type": episode_type,
                "scrambled": scrambled,
            },
            writer=self.writer(product_id),
            publisher=self.publisher_of(product_id),
            number=self._number(product_id, episode_id, episode_type),
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
        return descramble_tiles(image, seed, TILE_SIZE) if seed else image

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
        # The product page names the series and its authors; the viewer page
        # of a locked episode does not, so they are worth keeping hold of.
        self._series_titles.setdefault(product_id, _title_part(soup, 0))
        self._writers.setdefault(product_id, _authors_part(soup))
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

    def writer(self, product_id: str) -> str:
        """The authors a series' list page names in its title, comma-separated, read once per series."""
        if product_id not in self._writers:
            self.entries(product_id)
        return self._writers.get(product_id, "")

    def publisher_of(self, product_id: str) -> str:
        """The publisher a series' product page names (`ul.PCM-productPub`), read once per series."""
        if product_id not in self._publishers:
            soup = BeautifulSoup(self._get(f"{BASE_URL}/web/product/{product_id}").content, "html.parser")
            link = soup.select_one("ul.PCM-productPub a")
            self._publishers[product_id] = link.get_text(strip=True) if isinstance(link, Tag) else ""
        return self._publishers[product_id] or self.PUBLISHER

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
            prev_url=self._neighbours(product_id, episode_id, "E")[0],
            next_url=self._neighbours(product_id, episode_id, "E")[1],
            metadata={"product_id": product_id, "episode_id": episode_id, "episode_type": "E", "scrambled": False},
            writer=self.writer(product_id),
            publisher=self.publisher_of(product_id),
            number=self._number(product_id, episode_id, "E"),
        )

    def _neighbours(self, product_id: str, episode_id: str, episode_type: EpisodeType) -> tuple[str | None, str | None]:
        """Find the episodes either side of `episode_id` in its series' list."""
        if not product_id or not episode_id:
            return None, None
        entries = self.entries(product_id, episode_type)
        entry = next((entry for entry in entries if entry.id == episode_id), None)
        if entry is None:
            return None, None
        before, after = neighbours(entries, entry)
        return before.url if before else None, after.url if after else None

    def _number(self, product_id: str, episode_id: str, episode_type: EpisodeType) -> int | None:
        """Where `episode_id` stands in its series' list, counted from 1; None when unlisted."""
        if not product_id or not episode_id:
            return None
        return ordinal([entry.id for entry in self.entries(product_id, episode_type)], episode_id)

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


def _authors_part(soup: BeautifulSoup) -> str:
    """The authors a product page's title ends with, space-separated there, comma-separated here."""
    og = soup.find("meta", property="og:title")
    heading = str(og.attrs.get("content", "")) if isinstance(og, Tag) else ""
    if not heading and soup.title:
        heading = soup.title.get_text()
    parts = [part.strip() for part in heading.split("｜") if part.strip()]
    return ", ".join(parts[2].split()) if len(parts) >= _PRODUCT_TITLE_PARTS else ""


#: `"<series>｜<blurb>｜<authors>"`: how many parts a product page's title has.
_PRODUCT_TITLE_PARTS = 3


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
