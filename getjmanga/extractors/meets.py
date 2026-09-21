"""マンガMeets (集英社's manga submission site), a Next.js app over a JSON:API backend.

Every work is `/comics/<dir_name>` and every episode `/comics/<dir_name>/<sort_volume>`,
where `dir_name` is the work's UUID and `sort_volume` the episode's 1-based number.
The pages render nothing server-side but the titles: the work's episode list comes
from `/api/comics/<dir_name>/episodes.json` (a JSON:API document, the work itself
in `included`), and the viewer fetches `/api/comics/<dir_name>/episodes/<sort_volume>/viewer.json`,
a plain document listing the pages with ready-made Cloudinary URLs -- `pc_url` is
what the browser viewer shows. The images are ordinary Cloudinary deliveries: no
scrambling, no Referer or cookie check. The site hosts amateur submissions and
gates nothing behind a purchase; an episode the viewer endpoint answers 404 for
(off its publication span, or withdrawn) is treated as locked. An author's
`/profiles/<short_name>` page lists their works through
`/api/profiles/<short_name>/comics.json`, so it is accepted as a series URL that
walks every episode of every work. `challenge-mee.manga-meets.jp` (the contest
sub-site) serves the same app and the same API.
"""

from __future__ import annotations

import re
from http import HTTPStatus
from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import urlparse

from getjmanga.errors import NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractor import Episode, Extractor, Page, neighbours

if TYPE_CHECKING:
    from httpx import Client

# `/comics/<dir_name>/<sort_volume>`: one episode of a work.
_EPISODE_PATH = re.compile(r"^/comics/(?P<dir>[A-Za-z0-9_-]+)/(?P<vol>\d+)/?$")
# `/comics/<dir_name>`: the work page with its episode list.
_COMIC_PATH = re.compile(r"^/comics/(?P<dir>[A-Za-z0-9_-]+)/?$")
# `/profiles/<short_name>`: an author's page with their works.
_PROFILE_PATH = re.compile(r"^/profiles/(?P<name>[A-Za-z0-9_.-]+)/?$")

_API_HEADERS = {"Accept": "application/json"}


def episode_url(origin: str, dir_name: str, sort_volume: int) -> str:
    """The canonical URL of an episode.

    Args:
        origin: The site's `scheme://host`.
        dir_name: The work's `dir_name`.
        sort_volume: The episode's `sort_volume`.

    Returns:
        The episode URL.
    """
    return f"{origin}/comics/{dir_name}/{sort_volume}"


def episode_title(entry: dict[str, Any]) -> str:
    """Name an episode the way the site's heading does.

    Args:
        entry: An episode's attributes as the API describes them.

    Returns:
        `volume` (`1話`), followed by `title` when the author set one.
    """
    volume = str(entry.get("volume") or "")
    title = str(entry.get("title") or "")
    return f"{volume} {title}".strip() or str(entry.get("sort_volume") or "")


def _attributes(resource: Any) -> dict[str, Any]:  # noqa: ANN401 (whatever JSON the site sent)
    """A JSON:API resource's `attributes` with its `id`, or {} for anything else."""
    if not isinstance(resource, dict) or not isinstance(resource.get("attributes"), dict):
        return {}
    return {"id": resource.get("id"), **resource["attributes"]}


class Meets(Extractor):
    """Fetch episodes from マンガMeets."""

    NAME = "meets"
    HOSTS = ("challenge-mee.manga-meets.jp", "manga-meets.jp")
    PUBLISHER = "集英社"
    URL_FORMS = (
        "https://manga-meets.jp/comics/<dir_name>/<sort_volume>",
        "https://manga-meets.jp/comics/<dir_name>",
        "https://manga-meets.jp/profiles/<short_name>",
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
        # `episodes.json` answers per work with every episode and the work
        # itself; a bulk run asks for the same work once per episode, so it is kept.
        self._listings: dict[str, tuple[dict[str, Any], list[dict[str, Any]]]] = {}

    @classmethod
    def suitable(cls, url: str) -> bool:
        """Report whether the extractor takes `url`.

        Args:
            url: The URL to check.

        Returns:
            True for an episode, a work or a profile page on a known host.
        """
        if not super().suitable(url):
            return False
        path = urlparse(url).path
        return any(pattern.match(path) is not None for pattern in (_EPISODE_PATH, _COMIC_PATH, _PROFILE_PATH))

    @classmethod
    def is_series(cls, url: str) -> bool:
        """Report whether `url` names a work or an author rather than one episode.

        Args:
            url: The URL to check.

        Returns:
            True for `/comics/<dir_name>` and `/profiles/<short_name>`.
        """
        path = urlparse(url).path
        return _COMIC_PATH.match(path) is not None or _PROFILE_PATH.match(path) is not None

    def series_urls(self, url: str) -> list[str]:
        """List every episode of a work, or of every work of an author.

        Args:
            url: A work or a profile URL.

        Returns:
            One episode URL per listed episode, first episode first; for a
            profile, work by work in the order the site lists them.

        Raises:
            UnsupportedUrlError: The URL is neither a work nor a profile page.
            NotAnEpisodePageError: Nothing is listed.
        """
        origin = self._origin(url)
        path = urlparse(url).path
        if match := _COMIC_PATH.match(path):
            dir_names = [match["dir"]]
        elif match := _PROFILE_PATH.match(path):
            dir_names = self._profile_works(origin, match["name"], url)
        else:
            msg = f"{url} is not a work or a profile page."
            raise UnsupportedUrlError(msg)

        urls: list[str] = []
        for dir_name in dir_names:
            _, entries = self._listing(origin, dir_name, url)
            for entry in entries:
                candidate = episode_url(origin, dir_name, int(entry["sort_volume"]))
                if candidate not in urls:
                    urls.append(candidate)
        if not urls:
            msg = f"nothing at {url} lists an episode."
            raise NotAnEpisodePageError(msg)
        return urls

    def episode(self, url: str) -> Episode:
        """Read one episode and list its pages.

        The work listing names the episode and the one after it; the viewer
        document hands the page URLs over, and is withheld (404) for an
        episode that is not readable.

        Args:
            url: The episode URL.

        Returns:
            The episode. `pages` is empty when it is not readable.

        Raises:
            UnsupportedUrlError: The URL is not an episode URL.
            NotAnEpisodePageError: The site knows no such work or episode.
        """
        match = _EPISODE_PATH.match(urlparse(url).path)
        if match is None:
            msg = f"{url} is not an episode page."
            raise UnsupportedUrlError(msg)
        origin, dir_name, sort_volume = self._origin(url), match["dir"], int(match["vol"])

        comic, entries = self._listing(origin, dir_name, url)
        index = next((i for i, entry in enumerate(entries) if entry["sort_volume"] == sort_volume), None)
        if index is None:
            msg = f"no episode {sort_volume} in the work at {url}."
            raise NotAnEpisodePageError(msg)
        entry = entries[index]

        viewer = self._viewer(origin, dir_name, sort_volume, url)
        pages = tuple(
            Page(
                url=str(image["pc_url"]),
                width=int((image.get("pc_geometry") or {}).get("width") or 0),
                height=int((image.get("pc_geometry") or {}).get("height") or 0),
            )
            for image in _page_images(viewer)
        )
        before, after = neighbours(entries, entries[index])
        prev_url = episode_url(origin, dir_name, int(before["sort_volume"])) if before else None
        next_url = episode_url(origin, dir_name, int(after["sort_volume"])) if after else None

        return Episode(
            url=episode_url(origin, dir_name, sort_volume),
            series_title=str(comic.get("title") or ((viewer or {}).get("comic") or {}).get("title") or dir_name),
            episode_title=episode_title(entry),
            pages=pages,
            prev_url=prev_url,
            next_url=next_url,
            metadata={"comic": comic, "episode": entry, "viewer": viewer},
            writer=", ".join(str(name) for name in comic.get("authors") or [] if name),
            publisher=self.PUBLISHER,
        )

    def _listing(self, origin: str, dir_name: str, referer: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """The work and its episodes as `episodes.json` describes them, fetched once.

        Returns:
            The work's attributes ({} when the work lists no episode) and the
            episodes' attributes, first episode first.

        Raises:
            NotAnEpisodePageError: The site knows no such work.
        """
        if dir_name not in self._listings:
            res = self._session.get(
                f"{origin}/api/comics/{dir_name}/episodes.json",
                headers={**self.HEADERS, **_API_HEADERS, "Referer": referer},
                timeout=self.TIMEOUT,
            )
            if res.status_code == HTTPStatus.NOT_FOUND:
                msg = f"no work {dir_name} on {origin}."
                raise NotAnEpisodePageError(msg)
            res.raise_for_status()
            body = res.json()
            entries = [_attributes(item) for item in body.get("data") or []]
            entries = sorted((e for e in entries if "sort_volume" in e), key=lambda e: int(e["sort_volume"]))
            comic = next(
                (_attributes(item) for item in body.get("included") or [] if _is_type(item, "comic")),
                {},
            )
            self._listings[dir_name] = (comic, entries)
        return self._listings[dir_name]

    def _viewer(self, origin: str, dir_name: str, sort_volume: int, referer: str) -> dict[str, Any] | None:
        """The `viewer.json` document of an episode, or None when the site withholds it."""
        res = self._session.get(
            f"{origin}/api/comics/{dir_name}/episodes/{sort_volume}/viewer.json",
            headers={**self.HEADERS, **_API_HEADERS, "Referer": referer},
            timeout=self.TIMEOUT,
        )
        if res.status_code == HTTPStatus.NOT_FOUND:
            return None
        res.raise_for_status()
        body = res.json()
        return body if isinstance(body, dict) else None

    def _profile_works(self, origin: str, short_name: str, referer: str) -> list[str]:
        """The `dir_name` of every work of an author, in the order the site lists them.

        Raises:
            NotAnEpisodePageError: The site knows no such author.
        """
        res = self._session.get(
            f"{origin}/api/profiles/{short_name}/comics.json",
            headers={**self.HEADERS, **_API_HEADERS, "Referer": referer},
            timeout=self.TIMEOUT,
        )
        if res.status_code == HTTPStatus.NOT_FOUND:
            msg = f"no author {short_name} on {origin}."
            raise NotAnEpisodePageError(msg)
        res.raise_for_status()
        works: list[str] = []
        for item in res.json().get("data") or []:
            dir_name = str(_attributes(item).get("dir_name") or "")
            if dir_name and dir_name not in works:
                works.append(dir_name)
        return works


def _is_type(resource: Any, kind: str) -> bool:  # noqa: ANN401 (whatever JSON the site sent)
    """Whether a JSON:API resource is of `kind`."""
    return isinstance(resource, dict) and resource.get("type") == kind


def _page_images(viewer: dict[str, Any] | None) -> list[dict[str, Any]]:
    """The `image` of every page of a viewer document, in reading order."""
    if viewer is None:
        return []
    pages = [p for p in viewer.get("episode_pages") or [] if isinstance(p, dict) and isinstance(p.get("image"), dict)]
    pages.sort(key=lambda p: int(p.get("order_index") or 0))
    return [p["image"] for p in pages if p["image"].get("pc_url")]
