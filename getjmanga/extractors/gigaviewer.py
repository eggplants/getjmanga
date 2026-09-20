"""Sites running Hatena's GigaViewer: Shonen Jump+, Comic DAYS and the like."""

from __future__ import annotations

import json
import re
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

from bs4 import BeautifulSoup
from bs4.element import Tag

from getjmanga.errors import LoginError, NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractor import Episode, Extractor, Page

if TYPE_CHECKING:
    from httpx import Client
    from PIL import Image

# https://regex101.com/r/j0nUsd/1
_MAGAZINE_TITLE = re.compile(r"\s*([0-90-9]+年)?([0-90-9]+?(・?[0-90-9]+(合併)?)?月?号|(No|vol).[0-90-9]+)$")

# `/series/<id>/first_episode` redirects to the first `/episode/<id>` of the
# series; `/rss/series/<id>` is a feed whose items link to `/episode/<id>`s.
_EPISODE_PATH = re.compile(r"^(/(episode|magazine|volume)/\d+(\.json)?|/series/\d+/first_episode)$")
_FEED_PATH = re.compile(r"^/rss/series/\d+$")

# `script#episode-json` is occasionally missing from an otherwise valid
# response (the site sporadically serves a placeholder page), so retry before
# giving up.
_EPISODE_JSON_RETRY = 3
_EPISODE_JSON_RETRY_INTERVAL = 3

#: The viewer slices every page into a DIV x DIV grid of tiles whose side is a
#: multiple of MUL pixels, and transposes the grid.
DIV = 4
MUL = 8


def descramble(image: Image.Image, div: int = DIV, mul: int = MUL) -> Image.Image:
    """Transpose the tile grid of a page back into reading order.

    Tile size is floored to a multiple of `mul`, so any leftover strip on the
    right and bottom edge is never shuffled and is kept as-is.

    Args:
        image: The page exactly as the CDN serves it.
        div: How many tiles a side the grid is.
        mul: The multiple the tile side is floored to.

    Returns:
        A new image with the tiles back in reading order.
    """
    width, height = image.size
    tile_width = int(width / (div * mul)) * mul
    tile_height = int(height / (div * mul)) * mul
    out = image.copy()
    for x in range(div):
        for y in range(div):
            tile = image.crop((tile_width * x, tile_height * y, tile_width * (x + 1), tile_height * (y + 1)))
            out.paste(tile, (tile_width * y, tile_height * x))
    return out


class GigaViewer(Extractor):
    """Fetch episodes from a site running GigaViewer."""

    NAME = "gigaviewer"
    HOSTS = (
        "comic-action.com",
        "comic-days.com",
        "comic-earthstar.com",
        "comic-gardo.com",
        "comic-ogyaaa.com",
        "comic-seasons.com",
        "comic-trail.com",
        "comic-y-ours.com",
        "comic-zenon.com",
        "comicborder.com",
        "feelweb.jp",
        "ichicomi.com",
        "kuragebunch.com",
        "magcomi.com",
        "mangatime-square.com",
        "ourfeel.jp",
        "shonenjumpplus.com",
        "tonarinoyj.jp",
        "www.sunday-webry.com",
    )
    URL_FORMS = (
        "https://<host>/episode/<id>",
        "https://<host>/magazine/<id>",
        "https://<host>/volume/<id>",
        "https://<host>/series/<id>/first_episode",
        "https://<host>/rss/series/<id>",
    )

    def __init__(self, session: Client | None = None) -> None:
        """Build an extractor.

        Args:
            session: A session to reuse. A retrying one is made when omitted.
        """
        super().__init__(session)
        self._logged_in_origins: set[str] = set()

    @classmethod
    def suitable(cls, url: str) -> bool:
        """Report whether `url` is an episode or feed URL on a known host.

        Args:
            url: The URL to check.

        Returns:
            True for `/episode/<id>`, `/magazine/<id>`, `/volume/<id>`,
            `/series/<id>/first_episode` and `/rss/series/<id>` on `HOSTS`.
        """
        if not super().suitable(url):
            return False
        path = urlparse(url).path
        return _EPISODE_PATH.match(path) is not None or _FEED_PATH.match(path) is not None

    @classmethod
    def is_series(cls, url: str) -> bool:
        """Report whether `url` is a `/rss/series/<id>` feed.

        Args:
            url: The URL to check.

        Returns:
            True for a series feed.
        """
        return _FEED_PATH.match(urlparse(url).path) is not None

    def series_urls(self, url: str) -> list[str]:
        """List the episodes a series feed carries.

        Args:
            url: The `/rss/series/<id>` URL.

        Returns:
            One episode URL per feed item, in feed order.

        Raises:
            UnsupportedUrlError: The URL is no feed.
            NotAnEpisodePageError: The feed lists no episode.
        """
        if not self.is_series(url):
            msg = f"{url} is not a series feed."
            raise UnsupportedUrlError(msg)
        res = self._get(url)
        # The feed is served by an already trusted host (see `HOSTS`).
        feed = ET.fromstring(res.text)  # noqa: S314
        links = [link.strip() for elem in feed.findall("./channel/item/link") if (link := elem.text)]
        if not links:
            msg = f"the feed at {url} lists no episode."
            raise NotAnEpisodePageError(msg)
        return links

    def episode(self, url: str) -> Episode:
        """Read the titles and the page list off an episode page.

        Args:
            url: The episode URL, with or without `.json` on it.

        Returns:
            The episode. `pages` is empty when it wants a purchase.

        Raises:
            UnsupportedUrlError: The URL is no episode URL.
            NotAnEpisodePageError: The page carries no episode JSON.
        """
        # Only the path is checked, so `--extractor gigaviewer` can read a site
        # that is not in `HOSTS` yet.
        if _EPISODE_PATH.match(urlparse(url).path) is None:
            msg = f"{url} is not an episode url."
            raise UnsupportedUrlError(msg)
        url = url.removesuffix(".json")

        episode_json = self._episode_json(url)
        product = episode_json["readableProduct"]
        kind = product["typeName"]
        if kind == "magazine":
            series_title = self._series_title(url, product["title"])
            episode_title = product["title"].replace(series_title, "")
        elif kind in ("episode", "volume"):
            series_title = product["series"]["title"]
            episode_title = product["title"]
        else:
            msg = f"unknown typeName on {url}: {kind!r}"
            raise NotAnEpisodePageError(msg)

        # A locked episode carries no page structure at all.
        pages: tuple[Page, ...] = ()
        if product.get("isPublic") or product.get("hasPurchased"):
            pages = tuple(
                Page(url=page["src"], width=int(page.get("width") or 0), height=int(page.get("height") or 0))
                for page in (product.get("pageStructure") or {}).get("pages") or []
                if "src" in page
            )
        return Episode(
            url=url,
            series_title=series_title.strip(),
            episode_title=episode_title.strip(),
            pages=pages,
            prev_url=product.get("prevReadableProductUri"),
            next_url=product.get("nextReadableProductUri"),
            metadata=episode_json,
        )

    def image(self, page: Page, episode: Episode) -> Image.Image:
        """Fetch one page and transpose its tiles back.

        Args:
            page: The page to fetch.
            episode: The episode the page belongs to.

        Returns:
            The page in reading order.
        """
        return descramble(super().image(page, episode))

    def login(self, url: str, username: str, password: str) -> None:
        """Sign in to the site `url` is on.

        Every GigaViewer site takes an email address and a password at
        `/user_account/login`. The session cookie lands on the shared
        session, so a second sign-in to the same site is skipped.

        Args:
            url: Any URL on the site to sign in to.
            username: The email address the account uses.
            password: The account's password.

        Raises:
            LoginError: The site refused the credentials.
        """
        origin = self._origin(url)
        if origin in self._logged_in_origins:
            return
        login_url = f"{origin}/user_account/login"
        res = self._session.post(
            login_url,
            data={"email_address": username, "password": password, "return_location_path": url},
            headers={**self.HEADERS, "x-requested-with": "XMLHttpRequest"},
            timeout=self.TIMEOUT,
        )
        if not res.is_success:
            msg = f"{origin} refused the credentials for {username!r} (HTTP {res.status_code})."
            raise LoginError(msg)
        self._logged_in_origins.add(origin)

    def _episode_json(self, url: str) -> dict[str, Any]:
        for attempt in range(_EPISODE_JSON_RETRY):
            res = self._get(url)
            if "text/html" not in res.headers.get("content-type", ""):
                msg = f"{url} answered {res.headers.get('content-type')!r}, not a page."
                raise NotAnEpisodePageError(msg)

            script = BeautifulSoup(res.content, "html.parser").find("script", id="episode-json")
            if isinstance(script, Tag):
                value = script.attrs.get("data-value")
                if value is None:
                    msg = f"the episode JSON on {url} is empty."
                    raise NotAnEpisodePageError(msg)
                return dict(json.loads(str(value)))

            if attempt + 1 < _EPISODE_JSON_RETRY:
                time.sleep(_EPISODE_JSON_RETRY_INTERVAL * (attempt + 1))

        msg = f"no 'script#episode-json' on {url}; the site may be temporarily unavailable."
        raise NotAnEpisodePageError(msg)

    def _series_title(self, url: str, title: str) -> str:
        """The series a magazine issue belongs to, off its page or its title."""
        res = self._get(url)
        heading = BeautifulSoup(res.content, "html.parser").find("h1", class_="series-header-title")
        if isinstance(heading, Tag):
            return heading.get_text()
        return _MAGAZINE_TITLE.sub("", title)
