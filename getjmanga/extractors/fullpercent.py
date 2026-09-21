"""full% (フルパーセント, RelatyLS), a manga submission site with a server-rendered viewer.

A work page `/comic/detail/<work>` lists its episodes newest first (the
latest few in one `ul.comic_manga_list_box`, the rest in a second one hidden
behind `全部表示する`); `?order=episode_id_asc` turns the whole list around,
oldest first. Each entry opens the viewer through
`onclick="return openViewer(<work>,<episode>)"`, which is
`/comic/view/<work>/<episode>`: a jQuery page whose `#pagedata` holds one
`<img data-src>` per page in reading order. The images are ordinary JPEGs
under `/img/episode/<work>/<episode>_<hash>.jpg`, served without a Referer or a
cookie, and nothing is scrambled.

The viewer names no neighbour (`他のエピソードへ` closes the window), so
`next_url` comes from the work page's ordering, which is read once per work.
The viewer's heading is `<work title>　<episode title>`, and a work title may
itself hold a full-width space, so the titles are split with the work page's
`og:title` rather than at the first space. An episode that is missing (or has
been taken down) answers `エラー / 閲覧できません` with no `#pagedata`, which is
"not an episode"; the site sells nothing and gates no viewer behind a login.
The header's sign-in form posts `loginid` / `password` to `/user/login_ajax`
and answers `{"result": false, "reason": "auth_failed"}` when refused.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from bs4.element import Tag

from getjmanga.errors import LoginError, NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractor import Episode, Extractor, Page, neighbours

if TYPE_CHECKING:
    from collections.abc import Iterator

    from httpx import Client

BASE_URL = "https://fullpercent.net"
LOGIN_URL = f"{BASE_URL}/user/login_ajax"

_WORK_PATH = re.compile(r"^/comic/detail/(?P<work>\d+)/?$")
_EPISODE_PATH = re.compile(r"^/comic/view/(?P<work>\d+)/(?P<episode>\d+)/?$")
#: How a work page's list entries open the viewer.
_OPEN_VIEWER = re.compile(r"openViewer\(\s*(?P<work>\d+)\s*,\s*(?P<episode>\d+)\s*\)")
#: What the viewer puts between the work title and the episode title.
_TITLE_SEPARATOR = "　"
#: What the viewer appends to its `<title>`.
_TITLE_SUFFIX = "　｜　comic viewer"


@dataclass(frozen=True)
class WorkEpisode:
    """One entry of a work page's episode list."""

    id: str
    title: str
    thumbnail: str


@dataclass(frozen=True)
class Work:
    """What a work page says, its episodes oldest first."""

    id: str
    title: str
    author: str
    episodes: tuple[WorkEpisode, ...]

    def index_of(self, episode_id: str) -> int | None:
        """Where `episode_id` sits in the listing, or None when it is not listed."""
        for index, episode in enumerate(self.episodes):
            if episode.id == episode_id:
                return index
        return None


@dataclass(frozen=True)
class Viewer:
    """What a `/comic/view/<work>/<episode>` page says."""

    #: The heading, `<work title>　<episode title>`.
    heading: str
    #: The page images in reading order.
    images: tuple[str, ...]


def parse_work(html: str | bytes, url: str) -> Work | None:
    """Read a work page.

    Args:
        html: The page, fetched with `order=episode_id_asc` so the list runs oldest first.
        url: The URL it came from, to resolve relative links against.

    Returns:
        The work, or None when the page is not a work page (the site answers
        a missing work with its 404 page, HTTP 200).
    """
    soup = BeautifulSoup(html, "html.parser")
    match = _WORK_PATH.match(urlparse(url).path)
    if match is None or soup.find(id="epilist") is None:
        return None
    og_title = soup.find("meta", property="og:title")
    title = str(og_title["content"]) if isinstance(og_title, Tag) and og_title.has_attr("content") else ""
    if not title:
        heading = soup.select_one("div.left_title_t h2")
        if isinstance(heading, Tag):
            for span in heading.find_all("span"):
                span.decompose()
            title = heading.get_text(strip=True)
    return Work(
        id=match["work"],
        title=title,
        author=_text(soup.select_one("div.right_user_box h3 a")),
        episodes=tuple(_work_episodes(soup, match["work"], url)),
    )


def _work_episodes(soup: BeautifulSoup, work_id: str, url: str) -> Iterator[WorkEpisode]:
    """The entries of every `ul.comic_manga_list_box`, in document order, deduplicated."""
    seen: set[str] = set()
    for anchor in soup.select("ul.comic_manga_list_box a[onclick]"):
        match = _OPEN_VIEWER.search(str(anchor["onclick"]))
        if match is None or match["work"] != work_id or match["episode"] in seen:
            continue
        seen.add(match["episode"])
        image = anchor.find("img")
        title = ""
        thumbnail = ""
        if isinstance(image, Tag):
            title = str(image.get("alt") or image.get("title") or "").strip()
            thumbnail = urljoin(url, str(image.get("src") or ""))
        if not title:
            entry = anchor.find_parent("li")
            title = _text(entry.select_one("div.comic_manga_list_title") if isinstance(entry, Tag) else None)
        yield WorkEpisode(id=match["episode"], title=title, thumbnail=thumbnail)


def parse_viewer(html: str | bytes, url: str) -> Viewer | None:
    """Read a viewer page.

    Args:
        html: The page.
        url: The URL it came from, to resolve the image links against.

    Returns:
        The viewer, or None when there is none on the page (`閲覧できません`).
    """
    soup = BeautifulSoup(html, "html.parser")
    pagedata = soup.find(id="pagedata")
    if not isinstance(pagedata, Tag):
        return None
    heading = _text(soup.select_one("#header div"))
    if not heading:
        heading = _text(soup.title).removesuffix(_TITLE_SUFFIX)
    return Viewer(
        heading=heading,
        images=tuple(
            urljoin(url, str(img["data-src"]).strip())
            for img in pagedata.find_all("img")
            if isinstance(img, Tag) and str(img.get("data-src") or "").strip()
        ),
    )


def split_heading(heading: str, work_title: str) -> tuple[str, str]:
    """Split the viewer's `<work title>　<episode title>` heading.

    Args:
        heading: The viewer's heading.
        work_title: The work's title as its own page states it, or empty when unknown.

    Returns:
        The work title and the episode title. Without a known work title the
        heading is split at its first full-width space, which is right unless
        the work title holds one itself.
    """
    if work_title and heading.startswith(work_title):
        return work_title, heading[len(work_title) :].lstrip(_TITLE_SEPARATOR).strip()
    series, separator, episode = heading.partition(_TITLE_SEPARATOR)
    if not separator:
        return heading.strip(), ""
    return series.strip(), episode.strip()


def _text(tag: Tag | None) -> str:
    return tag.get_text(strip=True) if isinstance(tag, Tag) else ""


def work_url(work_id: str) -> str:
    """The canonical work page URL of `work_id`."""
    return f"{BASE_URL}/comic/detail/{work_id}"


def episode_url(work_id: str, episode_id: str) -> str:
    """The canonical viewer URL of `episode_id` of `work_id`."""
    return f"{BASE_URL}/comic/view/{work_id}/{episode_id}"


class FullPercent(Extractor):
    """Fetch episodes from full%."""

    NAME = "fullpercent"
    HOSTS = ("fullpercent.net",)
    PUBLISHER = "RelatyLS"
    URL_FORMS = (
        "https://fullpercent.net/comic/view/<work_id>/<episode_id>",
        "https://fullpercent.net/comic/detail/<work_id>",
    )
    CONFIG_KEY = "fullpercent"

    def __init__(self, session: Client | None = None) -> None:
        """Build an extractor.

        Args:
            session: A session to reuse. A retrying one is made when omitted.
        """
        super().__init__(session)
        # Work id -> what its page said (None: no such work), so a work is read once.
        self._works: dict[str, Work | None] = {}

    @classmethod
    def suitable(cls, url: str) -> bool:
        """Report whether the extractor takes `url`.

        Args:
            url: The URL to check.

        Returns:
            True for an https viewer or work page on a known host.
        """
        if not super().suitable(url):
            return False
        path = urlparse(url).path
        return _EPISODE_PATH.match(path) is not None or _WORK_PATH.match(path) is not None

    @classmethod
    def is_series(cls, url: str) -> bool:
        """Report whether `url` is a work page.

        Args:
            url: The URL to check.

        Returns:
            True for `/comic/detail/<work_id>`.
        """
        return _WORK_PATH.match(urlparse(url).path) is not None

    def series_urls(self, url: str) -> list[str]:
        """List every episode a work page links to, oldest first.

        Args:
            url: A work page URL.

        Returns:
            One viewer URL per listed episode, oldest first, deduplicated.

        Raises:
            UnsupportedUrlError: The URL is not a work page.
            NotAnEpisodePageError: No work page there, or it lists no episode
                (an illustration work, say).
        """
        match = _WORK_PATH.match(urlparse(url).path)
        if match is None:
            msg = f"{url} is not a work page."
            raise UnsupportedUrlError(msg)
        work = self._work(match["work"])
        if work is None:
            msg = f"no work page at {url}."
            raise NotAnEpisodePageError(msg)
        if not work.episodes:
            msg = f"the work at {url} lists no episode."
            raise NotAnEpisodePageError(msg)
        return [episode_url(work.id, episode.id) for episode in work.episodes]

    def episode(self, url: str) -> Episode:
        """Read one episode and list its pages.

        Args:
            url: A viewer URL.

        Returns:
            The episode. `next_url` is the entry after it on the work page, or
            None on the latest episode or when the work page does not list it.

        Raises:
            NotAnEpisodePageError: The URL is not a viewer URL, or the site
                shows no viewer there (`閲覧できません`).
        """
        match = _EPISODE_PATH.match(urlparse(url).path)
        if match is None:
            msg = f"{url} is not an episode page."
            raise NotAnEpisodePageError(msg)
        work_id, episode_id = match["work"], match["episode"]
        canonical = episode_url(work_id, episode_id)
        res = self._get(canonical)
        viewer = parse_viewer(res.content, canonical)
        if viewer is None:
            msg = f"no viewer on {url}."
            raise NotAnEpisodePageError(msg)

        work = self._work(work_id)
        series_title, episode_title = split_heading(viewer.heading, work.title if work is not None else "")
        index = work.index_of(episode_id) if work is not None else None
        listed = work.episodes[index] if work is not None and index is not None else None
        before, after = neighbours(work.episodes, listed) if work is not None and listed is not None else (None, None)
        prev_url = episode_url(work_id, before.id) if before is not None else None
        next_url = episode_url(work_id, after.id) if after is not None else None
        metadata: dict[str, Any] = {
            "work_id": work_id,
            "episode_id": episode_id,
            "heading": viewer.heading,
            "author": work.author if work is not None else "",
            "thumbnail": listed.thumbnail if listed is not None else "",
            "index": index,
            "episode_count": len(work.episodes) if work is not None else None,
            "page_count": len(viewer.images),
            "images": list(viewer.images),
        }
        return Episode(
            url=canonical,
            series_title=series_title or (work.title if work is not None else work_id),
            episode_title=(listed.title if listed is not None else "") or episode_title or episode_id,
            pages=tuple(Page(url=src) for src in viewer.images),
            prev_url=prev_url,
            next_url=next_url,
            metadata=metadata,
            writer=str(metadata["author"]),
            publisher=self.PUBLISHER,
        )

    def login(self, url: str, username: str, password: str) -> None:  # noqa: ARG002 (one site, one login route)
        """Sign in through the header form's endpoint.

        Args:
            url: Any URL on the site.
            username: The account's email address (`loginid`).
            password: The account's password.

        Raises:
            LoginError: The site refused the credentials (`auth_failed`) or
                found them missing (`require`).
        """
        res = self._session.post(
            LOGIN_URL,
            data={"loginid": username, "password": password},
            headers={**self.HEADERS, "X-Requested-With": "XMLHttpRequest", "Referer": f"{BASE_URL}/"},
            timeout=self.TIMEOUT,
        )
        res.raise_for_status()
        body = res.json()
        if not isinstance(body, dict) or body.get("result") is not True:
            reason = body.get("reason") if isinstance(body, dict) else None
            msg = f"{BASE_URL} refused the credentials for {username!r} ({reason or 'no reason given'})."
            raise LoginError(msg)

    def _work(self, work_id: str) -> Work | None:
        """Read a work page once, its episodes oldest first."""
        if work_id not in self._works:
            res = self._get(work_url(work_id), params={"order": "episode_id_asc"})
            self._works[work_id] = parse_work(res.content, str(res.url or work_url(work_id)))
        return self._works[work_id]
