"""カドコミ (KADOKAWA's ComicWalker), whose viewer XORs every page file with a per-page key."""

from __future__ import annotations

import re
from http import HTTPStatus
from io import BytesIO
from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import urlparse

from PIL import Image

from getjmanga.cipher import xor_unmask
from getjmanga.errors import NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractor import Episode, Extractor, Page, neighbours, published_on

if TYPE_CHECKING:
    from httpx import Client, Response

BASE_URL = "https://comic-walker.com"
#: The Next.js app's own backend; the work page and the viewer read everything through it.
API_URL = f"{BASE_URL}/api/contents"
#: What the viewer asks for on a desktop browser; `width:768` is the phone size.
IMAGE_SIZE = "width:1284"

# `/detail/<work>/episodes/<episode>`; the site itself redirects a trailing slash away.
_EPISODE_PATH = re.compile(r"^/detail/(?P<work>[A-Za-z0-9]+_\d+_S)/episodes/(?P<episode>[A-Za-z0-9]+_\d+_E)/?$")
# `/detail/<work>`, the work page with the episode list.
_SERIES_PATH = re.compile(r"^/detail/(?P<work>[A-Za-z0-9]+_\d+_S)/?$")

_API_HEADERS = {
    "Accept": "application/json",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}


def episode_url(work_code: str, episode_code: str) -> str:
    """The canonical URL of an episode.

    Args:
        work_code: The work code, `KC_001981_S`.
        episode_code: The episode code, `KC_0019810000100011_E`.

    Returns:
        The episode URL.
    """
    return f"{BASE_URL}/detail/{work_code}/episodes/{episode_code}"


def episode_title(entry: dict[str, Any]) -> str:
    """Name an episode the way the site's heading does.

    Args:
        entry: An episode as the work API describes it.

    Returns:
        `title`, followed by `subTitle` when the site set one.
    """
    title = str(entry.get("title") or entry.get("code") or "")
    subtitle = str(entry.get("subTitle") or "")
    return f"{title} {subtitle}" if subtitle else title


class ComicWalker(Extractor):
    """Fetch episodes from カドコミ."""

    NAME = "comicwalker"
    HOSTS = ("comic-walker.com",)
    PUBLISHER = "KADOKAWA"
    URL_FORMS = (
        "https://comic-walker.com/detail/<work>/episodes/<episode>",
        "https://comic-walker.com/detail/<work>",
    )
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
        # `details/work` answers per work with every episode; a bulk run asks
        # for the same work once per episode, so it is kept.
        self._works: dict[str, dict[str, Any]] = {}

    @classmethod
    def suitable(cls, url: str) -> bool:
        """Report whether the extractor takes `url`.

        Args:
            url: The URL to check.

        Returns:
            True for an episode or a work page on the known host.
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
            True for `/detail/<work>`.
        """
        return _SERIES_PATH.match(urlparse(url).path) is not None

    def series_urls(self, url: str) -> list[str]:
        """List every episode of a work, first episode first.

        Args:
            url: A work URL.

        Returns:
            One episode URL per listed episode, in the order the site lists them.

        Raises:
            UnsupportedUrlError: The URL is not a work page.
            NotAnEpisodePageError: The work lists no episode.
        """
        match = _SERIES_PATH.match(urlparse(url).path)
        if match is None:
            msg = f"{url} is not a work page."
            raise UnsupportedUrlError(msg)
        work_code = match["work"]
        urls: list[str] = []
        for entry in self._entries(self._work(work_code, url)):
            candidate = episode_url(work_code, str(entry.get("code") or ""))
            if entry.get("code") and candidate not in urls:
                urls.append(candidate)
        if not urls:
            msg = f"the work at {url} lists no episode."
            raise NotAnEpisodePageError(msg)
        return urls

    def episode(self, url: str) -> Episode:
        """Read one episode and list its pages.

        The work listing names the episode and the one after it; the
        `viewer` endpoint hands the page files over, with an empty
        `manuscripts` when the episode's free period is over.

        Args:
            url: The episode URL.

        Returns:
            The episode. `pages` is empty when it is not readable.

        Raises:
            UnsupportedUrlError: The URL is not an episode URL.
            NotAnEpisodePageError: The work does not list the episode.
        """
        match = _EPISODE_PATH.match(urlparse(url).path)
        if match is None:
            msg = f"{url} is not an episode page."
            raise UnsupportedUrlError(msg)
        work_code, episode_code = match["work"], match["episode"]

        work = self._work(work_code, url)
        entries = self._entries(work)
        index = next((i for i, entry in enumerate(entries) if entry.get("code") == episode_code), None)
        if index is None:
            msg = f"no episode {episode_code} in the work at {url}."
            raise NotAnEpisodePageError(msg)
        entry = entries[index]

        viewer = self._viewer(str(entry.get("id") or ""), url) if entry.get("id") else None
        manuscripts = sorted(
            (m for m in (viewer or {}).get("manuscripts") or [] if m.get("drmImageUrl")),
            key=lambda m: int(m.get("page") or 0),
        )
        pages = tuple(
            Page(
                url=str(m["drmImageUrl"]),
                width=int(m.get("width") or 0),
                height=int(m.get("height") or 0),
                extra={"drm_mode": str(m.get("drmMode") or "raw"), "drm_hash": str(m.get("drmHash") or "")},
            )
            for m in manuscripts
        )
        before, after = neighbours(entries, entry)
        prev_url = episode_url(work_code, str(before["code"])) if before and before.get("code") else None
        next_url = episode_url(work_code, str(after["code"])) if after and after.get("code") else None

        return Episode(
            url=episode_url(work_code, episode_code),
            series_title=str((work.get("work") or {}).get("title") or work_code),
            episode_title=episode_title(entry),
            pages=pages,
            prev_url=prev_url,
            next_url=next_url,
            metadata={"work": work.get("work"), "episode": entry, "viewer": viewer},
            writer=_authors(work.get("work") or {}),
            publisher=self.PUBLISHER,
            published=published_on(entry.get("updateDate")),
        )

    def image(self, page: Page, episode: Episode) -> Image.Image:
        """Fetch one page from the CDN and unmask it.

        The page URL is signed (a CloudFront policy the viewer answer carries),
        so it is fetched as given; no cookie or Referer is needed.

        Args:
            page: The page to fetch.
            episode: The episode the page belongs to.

        Returns:
            The page image.
        """
        res = self._get(
            page.url,
            headers={**self.HEADERS, "Accept": "image/webp,image/*,*/*", "Referer": episode.url},
            timeout=self.IMAGE_TIMEOUT,
        )
        data = res.content
        if page.extra.get("drm_mode") == "xor":
            data = xor_unmask(data, str(page.extra.get("drm_hash") or ""))
        return Image.open(BytesIO(data))

    @staticmethod
    def _entries(work: dict[str, Any]) -> list[dict[str, Any]]:
        """The episodes of a work, first episode first.

        `firstEpisodes` is the whole list in reading order (`latestEpisodes`
        is the same list reversed), whatever its `total` says.
        """
        listing = work.get("firstEpisodes") or {}
        return [entry for entry in listing.get("result") or [] if isinstance(entry, dict)]

    def _work(self, work_code: str, referer: str) -> dict[str, Any]:
        """The work as `details/work` describes it, fetched once.

        Raises:
            NotAnEpisodePageError: The site knows no such work (it answers with its 404 page).
        """
        if work_code not in self._works:
            res = self._session.get(
                f"{API_URL}/details/work",
                params={"workCode": work_code},
                headers={**self.HEADERS, **_API_HEADERS, "Referer": referer},
                timeout=self.TIMEOUT,
            )
            body = _json_or_none(res) if res.status_code == HTTPStatus.OK else None
            if not isinstance(body, dict) or not isinstance(body.get("work"), dict):
                msg = f"no work {work_code} on {BASE_URL}."
                raise NotAnEpisodePageError(msg)
            self._works[work_code] = body
        return self._works[work_code]

    def _viewer(self, episode_id: str, referer: str) -> dict[str, Any] | None:
        """The `viewer` answer for an episode, or None when the site withholds it.

        A readable episode lists its pages under `manuscripts`; one whose
        free period is over answers with an empty list, and an unknown id
        with the site's 404 page.
        """
        res = self._session.get(
            f"{API_URL}/viewer",
            params={"episodeId": episode_id, "imageSizeType": IMAGE_SIZE},
            headers={**self.HEADERS, **_API_HEADERS, "Referer": referer},
            timeout=self.TIMEOUT,
        )
        if res.status_code == HTTPStatus.NOT_FOUND:
            return None
        res.raise_for_status()
        body = _json_or_none(res)
        return body if isinstance(body, dict) else None


def _authors(work: dict[str, Any]) -> str:
    """The work's `authors`, each `名前 (役割)` the way the work page credits them."""
    credited = []
    for author in work.get("authors") or []:
        name, role = str(author.get("name") or "").strip(), str(author.get("role") or "").strip()
        if name:
            credited.append(f"{name} ({role})" if role else name)
    return ", ".join(credited)


def _json_or_none(res: Response) -> Any:  # noqa: ANN401 (whatever JSON the site sent)
    """`res.json()`, or None when the body is not JSON (the site's 404 page is HTML)."""
    try:
        return res.json()
    except ValueError:
        return None
