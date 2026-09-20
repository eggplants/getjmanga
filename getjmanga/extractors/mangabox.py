"""マンガボックス (MangaBox), whose viewer XORs every page file with a per-episode byte."""

from __future__ import annotations

import re
from http import HTTPStatus
from io import BytesIO
from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from bs4.element import Tag
from PIL import Image

from getjmanga.errors import LoginError, NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractor import Episode, Extractor, Page

if TYPE_CHECKING:
    from requests import Session

BASE_URL = "https://www.mangabox.me"
#: The Nuxt app's own backend; the viewer reads everything through it.
API_URL = f"{BASE_URL}/api/honshi"
LOGIN_URL = f"{BASE_URL}/browser/auser/login_mail/"

# `/reader/<manga>/episodes/<episode>/`. The site itself answers 404 without the
# trailing slash, so every URL built here carries one.
_EPISODE_PATH = re.compile(r"^/reader/(?P<manga>\d+)/episodes/(?P<episode>\d+)/?$")
# `/reader/<manga>/` (which redirects to `/episodes/`), the episode list, or its `all` tab.
_SERIES_PATH = re.compile(r"^/reader/(?P<manga>\d+)(?:/episodes(?:/all)?)?/?$")

_API_HEADERS = {
    "Accept": "application/json",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}


def unmask(data: bytes, mask: int) -> bytes:
    """Undo the viewer's masking of a page file.

    The CDN serves every page as a WebP whose bytes are all XORed with one
    value, `mask` of the episode's `images` answer. The viewer applies it as
    `Uint8Array.map(b => b ^ mask)`, so only the low byte of `mask` counts,
    which is what turns a negative `mask` such as `-7` into `0xF9`.

    Args:
        data: The file exactly as the CDN serves it.
        mask: The episode's `mask`.

    Returns:
        The WebP file.
    """
    key = mask & 0xFF
    return data.translate(bytes(byte ^ key for byte in range(256)))


def episode_url(manga_id: str | int, episode_id: str | int) -> str:
    """The canonical URL of an episode.

    Args:
        manga_id: The series id, `/reader/<manga>/`.
        episode_id: The episode id, `/episodes/<episode>/`.

    Returns:
        The episode URL, with the trailing slash the site insists on.
    """
    return f"{BASE_URL}/reader/{manga_id}/episodes/{episode_id}/"


def format_volume(entry: dict[str, Any]) -> str:
    """Name an episode the way the viewer's `formatVolume` does.

    Args:
        entry: An episode as the API describes it.

    Returns:
        `displayVolume` when the site set one, else `第<volume>話`.
    """
    display = entry.get("displayVolume")
    if display:
        return str(display)
    return f"第{entry.get('volume', '')}話"


class Mangabox(Extractor):
    """Fetch episodes from マンガボックス."""

    NAME = "mangabox"
    HOSTS = ("mangabox.me", "www.mangabox.me")
    URL_FORMS = (
        "https://www.mangabox.me/reader/<manga>/episodes/<episode>/",
        "https://www.mangabox.me/reader/<manga>/",
        "https://www.mangabox.me/reader/<manga>/episodes/",
        "https://www.mangabox.me/reader/<manga>/episodes/all/",
    )
    # `mangabox.me` only redirects to `www.`; one account covers both.
    CONFIG_KEY = "mangabox"
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
        # `get_all_episodes_by_manga_id` answers per series; a bulk run asks
        # for the same series once per episode, so it is kept.
        self._listings: dict[str, dict[str, Any]] = {}

    @classmethod
    def suitable(cls, url: str) -> bool:
        """Report whether the extractor takes `url`.

        Args:
            url: The URL to check.

        Returns:
            True for an episode or a series page on a known host.
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
            True for `/reader/<manga>/`, its `/episodes/` list or the `/all` tab.
        """
        return _SERIES_PATH.match(urlparse(url).path) is not None

    def series_urls(self, url: str) -> list[str]:
        """List every episode of a series, first episode first.

        Args:
            url: A series URL.

        Returns:
            One episode URL per listed episode, in the order the site lists them.

        Raises:
            UnsupportedUrlError: The URL is not a series page.
            NotAnEpisodePageError: The series lists no episode.
        """
        match = _SERIES_PATH.match(urlparse(url).path)
        if match is None:
            msg = f"{url} is not a series page."
            raise UnsupportedUrlError(msg)
        manga_id = match["manga"]
        urls: list[str] = []
        for entry in self._listing(manga_id, url).get("episodes") or []:
            candidate = episode_url(manga_id, entry["id"])
            if candidate not in urls:
                urls.append(candidate)
        if not urls:
            msg = f"the series at {url} lists no episode."
            raise NotAnEpisodePageError(msg)
        return urls

    def episode(self, url: str) -> Episode:
        """Read one episode and list its pages.

        The series listing names the episode and the one after it; the
        `images` endpoint hands the page files over, or answers 404 when the
        episode wants a coin, a ticket or a sign-in.

        Args:
            url: The episode URL.

        Returns:
            The episode. `pages` is empty when it is not readable.

        Raises:
            UnsupportedUrlError: The URL is not an episode URL.
            NotAnEpisodePageError: Neither the series nor the API knows the episode.
        """
        match = _EPISODE_PATH.match(urlparse(url).path)
        if match is None:
            msg = f"{url} is not an episode page."
            raise UnsupportedUrlError(msg)
        manga_id, episode_id = match["manga"], match["episode"]

        listing = self._listing(manga_id, url)
        entries: list[dict[str, Any]] = list(listing.get("episodes") or [])
        index = next((i for i, entry in enumerate(entries) if str(entry.get("id")) == episode_id), None)
        images = self._images(episode_id, url)
        if index is None and images is None:
            msg = f"no episode {episode_id} in the series at {url}."
            raise NotAnEpisodePageError(msg)

        entry = entries[index] if index is not None else {}
        next_url = None
        if index is not None and index + 1 < len(entries):
            next_url = episode_url(manga_id, entries[index + 1]["id"])

        described = images or entry
        manga = (images or {}).get("manga") or {}
        series_title = str(listing.get("title") or manga.get("title") or manga_id)
        pages: tuple[Page, ...] = ()
        if images is not None:
            mask = int(images.get("mask") or 0)
            pages = tuple(Page(url=str(src), extra={"mask": mask}) for src in images.get("imageUrls") or [])
        return Episode(
            url=episode_url(manga_id, episode_id),
            series_title=series_title,
            episode_title=format_volume(described),
            pages=pages,
            next_url=next_url,
            metadata={"episode": entry, "images": images},
        )

    def image(self, page: Page, episode: Episode) -> Image.Image:
        """Fetch one page through the site's image proxy and unmask it.

        The viewer never touches the CDN itself: it asks `/api/honshi/image`
        for the file, which also carries the session's cookies to a rented
        episode's files, and the CDN URL only travels as its `d` parameter.

        Args:
            page: The page to fetch.
            episode: The episode the page belongs to.

        Returns:
            The page image.
        """
        res = self._get(
            f"{API_URL}/image",
            params={"d": page.url},
            headers={**self.HEADERS, "Accept": "*/*", "Referer": episode.url},
            timeout=self.IMAGE_TIMEOUT,
        )
        return Image.open(BytesIO(unmask(res.content, int(page.extra.get("mask") or 0))))

    def login(self, url: str, username: str, password: str) -> None:  # noqa: ARG002 (one site, one login page)
        """Sign in with an email address, so rented episodes become readable.

        The mail login is a plain form behind `/browser/auser/login_mail/`
        with a one-time `token`; the session cookie it sets is what the API
        reads afterwards. Accounts registered through LINE, X, Facebook or
        Apple cannot sign in this way.

        Args:
            url: Any URL on the site.
            username: The account's email address.
            password: The account's password.

        Raises:
            LoginError: The site refused the credentials.
        """
        form = BeautifulSoup(
            self._get(LOGIN_URL, headers={**self.HEADERS, "Referer": f"{BASE_URL}/"}).content, "html.parser"
        )
        token = form.find("input", attrs={"name": "token"})
        if not isinstance(token, Tag) or not token.attrs.get("value"):
            msg = f"{LOGIN_URL} carries no login form."
            raise LoginError(msg)

        res = self._session.post(
            f"{LOGIN_URL}exec/",
            data={"token": str(token.attrs["value"]), "email": username, "password": password},
            headers={**self.HEADERS, "Origin": BASE_URL, "Referer": LOGIN_URL},
            timeout=self.TIMEOUT,
        )
        res.raise_for_status()
        answer = BeautifulSoup(res.content, "html.parser")
        error = answer.find(class_="txt_err")
        if isinstance(error, Tag):
            reason = " ".join(error.get_text().split())
            msg = f"{BASE_URL} refused the credentials for {username!r}: {reason}"
            raise LoginError(msg)
        if answer.find("input", attrs={"name": "password"}) is not None:
            msg = f"{BASE_URL} refused the credentials for {username!r}."
            raise LoginError(msg)

    def _listing(self, manga_id: str, referer: str) -> dict[str, Any]:
        """The series as `get_all_episodes_by_manga_id` describes it, fetched once.

        Raises:
            NotAnEpisodePageError: The site knows no such series.
        """
        if manga_id not in self._listings:
            res = self._session.post(
                f"{API_URL}/jsonrpc",
                json={
                    "jsonrpc": "2.0",
                    "method": "get_all_episodes_by_manga_id",
                    "params": {"mangaId": int(manga_id), "withTags": 1},
                },
                headers={**self.HEADERS, **_API_HEADERS, "Referer": referer},
                timeout=self.TIMEOUT,
            )
            body = res.json() if res.status_code == HTTPStatus.OK else None
            result = body.get("result") if isinstance(body, dict) else None
            if not isinstance(result, dict):
                msg = f"no series {manga_id} on {BASE_URL}."
                raise NotAnEpisodePageError(msg)
            self._listings[manga_id] = result
        return self._listings[manga_id]

    def _images(self, episode_id: str, referer: str) -> dict[str, Any] | None:
        """The `images` answer for an episode, or None when the site withholds it.

        A 404 is the site's answer for a locked episode as much as for an
        unknown one; the caller tells them apart by the series listing.
        """
        res = self._session.get(
            f"{API_URL}/episode/{episode_id}/images",
            headers={**self.HEADERS, **_API_HEADERS, "Referer": referer},
            timeout=self.TIMEOUT,
        )
        if res.status_code == HTTPStatus.NOT_FOUND:
            return None
        res.raise_for_status()
        body = res.json()
        return body if isinstance(body, dict) and "imageUrls" in body else None
