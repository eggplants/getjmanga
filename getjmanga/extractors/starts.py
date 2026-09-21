"""スターツ出版's serial comic viewer, run by ベリーズカフェ, noicomi and comicグラスト.

The three sites are one Laravel app under three hostnames: they ship the same
hashed JS bundles, lay their `/comic/serial/` pages out the same way and
serve page images from the same `/img/serial-comic/` tree. Each page is cut
into a grid of square tiles shuffled with `shuffle-seed` over `seedrandom`
-- the very shuffle Piccoma's viewer does -- so `viewers/seedrandom.py`
does the descrambling.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from http import HTTPStatus
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from bs4.element import Tag

from getjmanga.errors import GetjmangaError, NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractor import Episode, Extractor, Page, published_on
from getjmanga.viewers.seedrandom import descramble as _descramble_tiles

if TYPE_CHECKING:
    from httpx import Client
    from PIL import Image

# `/comic/serial/n<serial>/n<story>/<page>`; the site itself redirects a URL
# without the page number to `/1`, which is the shape used everywhere here.
_EPISODE_PATH = re.compile(r"^/comic/serial/n(?P<serial>\d+)/n(?P<story>\d+)(?:/(?P<page>\d+))?/?$")
_SERIES_PATH = re.compile(r"^/comic/serial/n(?P<serial>\d+)/?$")

_JSON_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "X-Requested-With": "XMLHttpRequest",
}


@dataclass(frozen=True)
class Story:
    """One row of a series page's episode list."""

    number: int
    title: str
    url: str
    #: False for an episode whose free period is over ("各電子書店で読む"),
    #: which the site lists without a link and answers 404 for.
    readable: bool
    #: The `更新` line, `2026/09/17 更新`, when the row has one.
    updated: str = ""


def descramble(image: Image.Image, seed: str, size: int) -> Image.Image:
    """Put a shuffled page back together.

    The viewer calls `unscrambleImg(image, max(width, height) / size, seed)`:
    the longer side of the page is cut into `size` tiles, the shorter one
    into as many as fit (the last one cut short), and every group of
    same-shaped tiles is shuffled among itself with `shuffle-seed`, which
    draws its floats from `seedrandom(seed)`. That is Piccoma's scramble
    with a per-page tile side, so the shared port does the work.

    Args:
        image: The page exactly as the site serves it.
        seed: The page's `seed` from `index.json`.
        size: The page's `size` from `index.json`: tiles along the longer side.

    Returns:
        A new image with the tiles back where they belong.

    Raises:
        GetjmangaError: The longer side does not split into `size` whole pixels.
    """
    side, remainder = divmod(max(image.size), size)
    if remainder:
        msg = f"a {image.width}x{image.height} page does not cut into {size} whole tiles."
        raise GetjmangaError(msg)
    return _descramble_tiles(image, seed, side)


def episode_url(origin: str, serial: str | int, story: str | int) -> str:
    """The canonical URL of an episode, first page.

    Args:
        origin: `https://<host>` of the site.
        serial: The series id, `/comic/serial/n<serial>`.
        story: The episode's story number, `/n<story>`.

    Returns:
        The episode URL.
    """
    return f"{origin}/comic/serial/n{serial}/n{story}/1"


def series_url(origin: str, serial: str | int) -> str:
    """The URL of a series page.

    Args:
        origin: `https://<host>` of the site.
        serial: The series id, `/comic/serial/n<serial>`.

    Returns:
        The series URL.
    """
    return f"{origin}/comic/serial/n{serial}"


def parse_comic_data(html: str | bytes) -> dict[str, Any] | None:
    """Read the `script#comic-data` JSON the viewer boots from.

    Args:
        html: An episode page.

    Returns:
        The JSON object, or None when the page carries no viewer.
    """
    script = BeautifulSoup(html, "html.parser").find("script", id="comic-data")
    if not isinstance(script, Tag):
        return None
    try:
        data = json.loads(script.get_text())
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def parse_listing(html: str | bytes, origin: str, serial: str) -> tuple[str, list[Story]]:
    """Read the series title and the episode list off a series page.

    The page lists episodes newest first; an upcoming one has no story
    number yet and is left out, a closed one has a number but no link.

    Args:
        html: The series page.
        origin: `https://<host>` the page came from.
        serial: The series id of the page.

    Returns:
        The series title and its stories, first story first.
    """
    soup = BeautifulSoup(html, "html.parser")
    title_tag = soup.find("dt", class_="comicTit")
    title = " ".join(title_tag.get_text().split()) if isinstance(title_tag, Tag) else ""

    stories: dict[int, Story] = {}
    for label in soup.find_all("span", class_="storyTitle"):
        if not isinstance(label, Tag) or not str(label.attrs.get("data-story-number", "")).isdigit():
            continue
        number = int(str(label.attrs["data-story-number"]))
        article = label.find_parent("article")
        readable = isinstance(article, Tag) and article.find("a", href=True) is not None
        updated = article.find("p", class_="update") if isinstance(article, Tag) else None
        stories.setdefault(
            number,
            Story(
                number=number,
                title=" ".join(label.get_text().split()),
                url=episode_url(origin, serial, number),
                readable=readable,
                updated=updated.get_text(strip=True) if isinstance(updated, Tag) else "",
            ),
        )
    return title, [stories[number] for number in sorted(stories)]


def parse_credits(html: str | bytes) -> str:
    """The `ul.credit` entries of a series page, `役割／名前` each, as `名前 (役割)`.

    Args:
        html: The series page.

    Returns:
        The names, comma-separated; empty when the page credits nobody.
    """
    soup = BeautifulSoup(html, "html.parser")
    credited = []
    for item in soup.select("div.creditBox ul.credit li"):
        role, sep, name = " ".join(item.get_text().split()).partition("／")
        if sep and name.strip():
            credited.append(f"{name.strip()} ({role.strip()})")
    return ", ".join(credited)


class Starts(Extractor):
    """Fetch episodes from the comic sections of スターツ出版's sites."""

    NAME = "starts"
    HOSTS = ("novema.jp", "www.berrys-cafe.jp", "www.no-ichigo.jp")
    PUBLISHER = "スターツ出版"
    URL_FORMS = (
        "https://www.berrys-cafe.jp/comic/serial/n<serial>/n<story>/<page>",
        "https://www.berrys-cafe.jp/comic/serial/n<serial>",
        "https://www.no-ichigo.jp/comic/serial/n<serial>/n<story>/<page>",
        "https://www.no-ichigo.jp/comic/serial/n<serial>",
        "https://novema.jp/comic/serial/n<serial>/n<story>/<page>",
        "https://novema.jp/comic/serial/n<serial>",
    )

    def __init__(self, session: Client | None = None) -> None:
        """Build an extractor.

        Args:
            session: A session to reuse. A retrying one is made when omitted.
        """
        super().__init__(session)
        # The series page names the series and orders its episodes; a bulk
        # run would fetch it once per episode otherwise.
        self._listings: dict[str, tuple[str, list[Story]]] = {}
        #: The credits of each series page read, by URL.
        self._credits: dict[str, str] = {}

    @classmethod
    def suitable(cls, url: str) -> bool:
        """Report whether the extractor takes `url`.

        Only the `/comic/serial/` part of each site is a comic viewer; the
        rest of the sites is novels, which this cannot read.

        Args:
            url: The URL to check.

        Returns:
            True for a serial comic episode or series page on a known host.
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
            True for `/comic/serial/n<serial>`.
        """
        return _SERIES_PATH.match(urlparse(url).path) is not None

    def series_urls(self, url: str) -> list[str]:
        """List every episode of a series, first story first.

        Closed episodes are listed too: `episode()` answers them with no
        pages, so a run skips them and carries on.

        Args:
            url: A series URL.

        Returns:
            One episode URL per listed story, in story order.

        Raises:
            UnsupportedUrlError: The URL is not a series page.
            NotAnEpisodePageError: The series lists no episode.
        """
        match = _SERIES_PATH.match(urlparse(url).path)
        if match is None:
            msg = f"{url} is not a series page."
            raise UnsupportedUrlError(msg)
        _, stories = self._listing(self._origin(url), match["serial"])
        if not stories:
            msg = f"the series at {url} lists no episode."
            raise NotAnEpisodePageError(msg)
        return [story.url for story in stories]

    def episode(self, url: str) -> Episode:
        """Read one episode and list its pages.

        The episode page boots the viewer from `script#comic-data`, which
        names the story and the timestamp the page files are versioned by;
        `index.json` under the story's image directory lists the pages with
        the seed and tile count each was shuffled with. An episode whose free
        period is over answers 404, but its series page still lists it, so it
        comes back with no pages and the story after it as `next_url`.

        Args:
            url: The episode URL.

        Returns:
            The episode. `pages` is empty when it is not readable.

        Raises:
            UnsupportedUrlError: The URL is not an episode URL.
            NotAnEpisodePageError: Neither the page nor the series knows the episode.
        """
        match = _EPISODE_PATH.match(urlparse(url).path)
        if match is None:
            msg = f"{url} is not an episode page."
            raise UnsupportedUrlError(msg)
        origin, serial, number = self._origin(url), match["serial"], int(match["story"])
        canonical = episode_url(origin, serial, number)

        res = self._session.get(canonical, headers=self.HEADERS, timeout=self.TIMEOUT)
        data = parse_comic_data(res.content) if res.status_code != HTTPStatus.NOT_FOUND else None
        if data is None and res.status_code != HTTPStatus.NOT_FOUND:
            res.raise_for_status()

        series_title, stories = self._listing(origin, serial)
        story = next((story for story in stories if story.number == number), None)
        if data is None and story is None:
            msg = f"no viewer on {canonical}."
            raise NotAnEpisodePageError(msg)
        prev_url = next((story.url for story in reversed(stories) if story.number < number), None)
        next_url = next((story.url for story in stories if story.number > number), None)

        pages: tuple[Page, ...] = ()
        images: list[dict[str, Any]] = []
        if data is not None:
            images = self._images(origin, serial, number, data, referer=canonical)
            content = f"{origin}/img/serial-comic/{serial}/{number}/content"
            stamp = data.get("story_updated_at", "")
            pages = tuple(
                Page(
                    url=f"{content}/{entry['name']}?t={stamp}",
                    extra={"seed": str(entry.get("seed", "")), "size": int(entry.get("size") or 1)},
                )
                for entry in images
                if entry.get("name")
            )
        episode_title = str((data or {}).get("story_title") or (story.title if story else number))
        return Episode(
            url=canonical,
            series_title=series_title or f"n{serial}",
            episode_title=episode_title,
            pages=pages,
            prev_url=prev_url,
            next_url=next_url,
            metadata={"comic_data": data, "story": asdict(story) if story else None, "images": images},
            writer=self._credits.get(series_url(origin, serial), ""),
            publisher=self.PUBLISHER,
            published=published_on(story.updated) if story else None,
            number=number,
        )

    def image(self, page: Page, episode: Episode) -> Image.Image:
        """Fetch one page and put its tiles back in order.

        Args:
            page: The page to fetch.
            episode: The episode the page belongs to.

        Returns:
            The page image.
        """
        image = self._fetch_image(page.url, headers={**self.HEADERS, "Referer": episode.url})
        seed = str(page.extra.get("seed") or "")
        size = int(page.extra.get("size") or 0)
        if not seed or size <= 1:
            return image
        return descramble(image, seed, size)

    def _listing(self, origin: str, serial: str) -> tuple[str, list[Story]]:
        """The series title and stories of a series page, fetched once."""
        url = series_url(origin, serial)
        if url not in self._listings:
            res = self._get(url)
            self._listings[url] = parse_listing(res.content, origin, serial)
            self._credits[url] = parse_credits(res.content)
        return self._listings[url]

    def _images(
        self, origin: str, serial: str, number: int, data: dict[str, Any], *, referer: str
    ) -> list[dict[str, Any]]:
        """The story's `index.json`, or nothing when the site withholds it."""
        res = self._session.get(
            f"{origin}/img/serial-comic/{serial}/{number}/content/index.json",
            params={"t": str(data.get("story_updated_at", ""))},
            headers={**self.HEADERS, **_JSON_HEADERS, "Referer": referer},
            timeout=self.TIMEOUT,
        )
        if res.status_code == HTTPStatus.NOT_FOUND:
            return []
        res.raise_for_status()
        body = res.json()
        return [entry for entry in body if isinstance(entry, dict)] if isinstance(body, list) else []
