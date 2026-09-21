"""マンガよもんが (Bunkasha): a WordPress work page with a SpeedBinb viewer on it.

Every URL is a work page, `https://www.yomonga.com/titles/<id>/`; the
`episode=<n>` query picks the episode the viewer on that page shows, and the
site adds the matching `cid=<content-id>` itself (by redirect when it is
missing or wrong). The page lists the readable episodes newest first -- an
episode whose free run is over is dropped from the list, and asking for it
sends the browser to the newest episode instead.

The viewer is Voyager's SpeedBinb in its REST flavour (`ServerType` 2): the
`bibGetCntntInfo` handshake, the key, the scramble tables and the tile shuffle
are `viewers/speedbinb.py`'s. What differs
is where the pages live: the handshake answer sets CloudFront signed cookies
for `<ContentsServer>` on the session, the page list is `<ContentsServer>/content`
(plain JSON, not JSONP) and each page is `<ContentsServer>/img/<src>`, served
only with those cookies. No Referer check, no login.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from http import HTTPStatus
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup
from bs4.element import Tag

from getjmanga.errors import NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractor import Episode, Extractor, Page, published_on
from getjmanga.viewers import speedbinb

if TYPE_CHECKING:
    from collections.abc import Mapping

    from httpx2 import Client
    from PIL import Image

BASE_URL = "https://www.yomonga.com"


# A work page. With `?episode=<n>` it is an episode, without one a series.
_TITLE_PATH = re.compile(r"^/titles/(?P<id>\d+)/?$")

# The viewer element; `data-ptbinb` names the API endpoint.
_VIEWER_SELECTOR = "#content[data-ptbinb]"

# What the page tells its scripts: `const binb_cid = '10788';` and `const episode_no = 1;`.
_BINB_CID = re.compile(r"\bbinb_cid\s*=\s*['\"](?P<cid>\d+)['\"]")
_EPISODE_NO = re.compile(r"\bepisode_no\s*=\s*(?P<no>\d+)\s*;")


@dataclass(frozen=True)
class Listed:
    """One episode as the work page lists it."""

    number: int
    content_id: str
    title: str
    #: `"2026/10/01に公開終了"`, or "" when the episode has no end date.
    publish_end: str
    #: `update-date`, `2026/09/11`.
    updated: str = ""


@dataclass(frozen=True)
class WorkPage:
    """What a work page says about the work and the episode its viewer shows."""

    title_id: str
    #: `.intr-title`: the work's title.
    series_title: str
    #: `.detail-title`: the title of the episode the viewer shows.
    episode_title: str
    #: `episode_no` of the episode the viewer shows.
    episode_no: int
    #: `binb_cid` of the episode the viewer shows.
    content_id: str
    #: The `bibGetCntntInfo` endpoint, absolute; None when the page has no viewer.
    info_url: str | None
    #: The readable episodes, oldest first.
    episodes: tuple[Listed, ...]
    #: The authors the page links (`?author_id=`), comma-separated.
    writer: str = ""

    def listed(self, number: int) -> Listed | None:
        """The listed episode with `number`, or None when the list has none."""
        return next((episode for episode in self.episodes if episode.number == number), None)

    def before(self, number: int) -> Listed | None:
        """The last listed episode before `number`, or None when it is the first."""
        return next((episode for episode in reversed(self.episodes) if episode.number < number), None)

    def after(self, number: int) -> Listed | None:
        """The first listed episode after `number`, or None when it is the last."""
        return next((episode for episode in self.episodes if episode.number > number), None)


def parse_work_page(html: str | bytes, url: str) -> WorkPage:
    """Read a work page.

    Args:
        html: The page.
        url: The URL it came from, to resolve the API endpoint against.

    Returns:
        The titles, the episode the viewer is set to, and the episode list.

    Raises:
        NotAnEpisodePageError: The page is not a work page.
    """
    match = _TITLE_PATH.match(urlparse(url).path)
    if match is None:
        msg = f"{url} is not a work page."
        raise NotAnEpisodePageError(msg)
    soup = BeautifulSoup(html, "html.parser")
    scripts = "\n".join(script.get_text() for script in soup.find_all("script"))
    cid = _BINB_CID.search(scripts)
    number = _EPISODE_NO.search(scripts)
    series = soup.select_one(".intr-title")
    if cid is None or number is None or not isinstance(series, Tag):
        msg = f"no work page at {url}."
        raise NotAnEpisodePageError(msg)
    heading = soup.select_one(".detail-title")
    viewer = soup.select_one(_VIEWER_SELECTOR)

    episodes: dict[int, Listed] = {}
    for row in soup.select(".episode-list[data-episode_no]"):
        link = row.select_one("a.episode-list-button[href]")
        name = row.select_one(".episode-name")
        end = row.select_one(".publish-end-date")
        updated = row.select_one(".update-date")
        if not isinstance(link, Tag):
            continue
        query = parse_qs(urlparse(str(link["href"])).query)
        listed_cid = (query.get("cid") or [""])[0]
        try:
            listed_no = int(str(row["data-episode_no"]))
        except ValueError:
            continue
        if not listed_cid:
            continue
        expires = end.get_text(strip=True) if isinstance(end, Tag) and "disable" not in (end.get("class") or []) else ""
        episodes.setdefault(
            listed_no,
            Listed(
                number=listed_no,
                content_id=listed_cid,
                title=name.get_text(strip=True) if isinstance(name, Tag) else "",
                publish_end=expires,
                updated=updated.get_text(strip=True) if isinstance(updated, Tag) else "",
            ),
        )
    return WorkPage(
        title_id=match["id"],
        series_title=series.get_text(strip=True),
        episode_title=heading.get_text(strip=True) if isinstance(heading, Tag) else "",
        episode_no=int(number["no"]),
        content_id=cid["cid"],
        info_url=urljoin(url, str(viewer["data-ptbinb"])) if isinstance(viewer, Tag) else None,
        episodes=tuple(episodes[key] for key in sorted(episodes)),
        writer=", ".join(
            dict.fromkeys(
                a.get_text(strip=True) for a in soup.select('a[href*="author_id="]') if a.get_text(strip=True)
            )
        ),
    )


def episode_url(title_id: str, number: int, content_id: str = "") -> str:
    """The URL of episode `number` of a work, as the site writes it (minus the `#content`)."""
    url = f"{BASE_URL}/titles/{title_id}/?episode={number}"
    return f"{url}&cid={content_id}" if content_id else url


class Yomonga(Extractor):
    """Fetch episodes from マンガよもんが (www.yomonga.com)."""

    NAME = "yomonga"
    HOSTS = ("www.yomonga.com",)
    PUBLISHER = "ぶんか社"
    URL_FORMS = (
        "https://www.yomonga.com/titles/<id>/?episode=<n>",
        "https://www.yomonga.com/titles/<id>/?episode=<n>&cid=<content-id>",
        "https://www.yomonga.com/titles/<id>/",
    )

    def __init__(self, session: Client | None = None) -> None:
        """Build an extractor.

        Args:
            session: A session to reuse. A retrying one is made when omitted.
        """
        super().__init__(session)
        # Title id -> the episode list its work page carried, so a series is read once.
        self._episodes: dict[str, tuple[Listed, ...]] = {}

    @classmethod
    def suitable(cls, url: str) -> bool:
        """Report whether `url` is a work page, with or without an episode picked.

        Args:
            url: The URL to check.

        Returns:
            True for `https://www.yomonga.com/titles/<id>/`, any query.
        """
        return super().suitable(url) and _TITLE_PATH.match(urlparse(url).path) is not None

    @classmethod
    def is_series(cls, url: str) -> bool:
        """Report whether `url` is a work page with no episode picked.

        Args:
            url: The URL to check.

        Returns:
            True for `https://www.yomonga.com/titles/<id>/` without an `episode` query.
        """
        return cls.suitable(url) and _episode_number(url) is None

    def series_urls(self, url: str) -> list[str]:
        """List the readable episodes of a work, oldest first.

        Args:
            url: A work page URL.

        Returns:
            One episode URL per listed episode, oldest first.

        Raises:
            UnsupportedUrlError: The URL is not a work page, or picks an episode.
            NotAnEpisodePageError: The work lists no readable episode.
        """
        match = _TITLE_PATH.match(urlparse(url).path)
        if match is None or _episode_number(url) is not None:
            msg = f"{url} is not a series page."
            raise UnsupportedUrlError(msg)
        listed = self._listing(match["id"])
        if not listed:
            msg = f"the work at {url} lists no readable episode."
            raise NotAnEpisodePageError(msg)
        return [episode_url(match["id"], episode.number, episode.content_id) for episode in listed]

    def episode(self, url: str) -> Episode:
        """Read one episode: its titles, its pages and the episode after it.

        Args:
            url: A work page URL with `episode=<n>` on it.

        Returns:
            The episode. `pages` is empty when the episode's free run is over:
            the site then shows the newest episode instead of the one asked
            for, and `next_url` names the next one still listed.

        Raises:
            UnsupportedUrlError: The URL picks no episode.
            NotAnEpisodePageError: There is no such work (404), or no such
                episode after the newest listed one.
        """
        match = _TITLE_PATH.match(urlparse(url).path)
        number = _episode_number(url)
        if match is None or number is None:
            msg = f"{url} is not an episode page."
            raise UnsupportedUrlError(msg)
        title_id = match["id"]
        query = parse_qs(urlparse(url).query)
        page_url = episode_url(title_id, number, (query.get("cid") or [""])[0])

        res = self._session.get(page_url, headers=self.HEADERS, timeout=self.TIMEOUT)
        if res.status_code == HTTPStatus.NOT_FOUND:
            msg = f"{url} is not there (404)."
            raise NotAnEpisodePageError(msg)
        res.raise_for_status()
        work = parse_work_page(res.content, page_url)
        self._episodes[title_id] = work.episodes

        preceding, following = work.before(number), work.after(number)
        prev_url = episode_url(title_id, preceding.number, preceding.content_id) if preceding else None
        next_url = episode_url(title_id, following.number, following.content_id) if following else None
        listed = work.listed(number)
        if work.episode_no != number or listed is None:
            # The site answered with another episode: the one asked for is not
            # listed any more (its free run is over) -- or, with nothing listed
            # after it, never was.
            if following is None:
                msg = f"{url} names no episode: the work lists none after {number}."
                raise NotAnEpisodePageError(msg)
            return Episode(
                url=page_url,
                series_title=work.series_title,
                episode_title=f"Chapter.{number}",
                prev_url=prev_url,
                next_url=next_url,
                metadata={"title_id": title_id, "episode_no": number, "locked": True},
                writer=work.writer,
                publisher=self.PUBLISHER,
                published=published_on(listed.updated) if listed else None,
                number=number,
            )
        if work.info_url is None:
            msg = f"no SpeedBinb viewer on {page_url}."
            raise NotAnEpisodePageError(msg)

        content_id = work.content_id
        content = speedbinb.content_info(
            self,
            work.info_url,
            content_id,
            referer=page_url,
            server_types=frozenset({speedbinb.SERVER_TYPE_REST}),
        )
        if content is None:
            msg = f"{work.info_url} did not describe {content_id}."
            raise NotAnEpisodePageError(msg)
        book = speedbinb.page_list(self, content, referer=page_url)
        item = content.item

        return Episode(
            url=page_url,
            series_title=work.series_title,
            episode_title=work.episode_title or listed.title or f"Chapter.{number}",
            pages=book.pages,
            prev_url=prev_url,
            next_url=next_url,
            metadata={
                "title_id": title_id,
                "episode_no": number,
                "content_id": content_id,
                "contents_server": content.server,
                "publish_end": listed.publish_end,
                "title": item.get("Title"),
                "authors": item.get("Authors"),
                "publisher": item.get("Publisher"),
                "view_mode": item.get("ViewMode"),
            },
            writer=_authors(item) or work.writer,
            publisher=str(item.get("Publisher") or "") or self.PUBLISHER,
            published=published_on(listed.updated),
            number=number,
        )

    def image(self, page: Page, episode: Episode) -> Image.Image:
        """Fetch one page and put its tiles back where they belong.

        The page is only served with the CloudFront cookies `episode()` got
        from `bibGetCntntInfo`, which sit on the shared session.

        Args:
            page: The page to fetch.
            episode: The episode the page belongs to.

        Returns:
            The page in reading order, padding gone.
        """
        return speedbinb.fetch_page(self, page, referer=episode.url)

    def _listing(self, title_id: str) -> tuple[Listed, ...]:
        """The readable episodes of a work, oldest first, read once per work."""
        cached = self._episodes.get(title_id)
        if cached is not None:
            return cached
        page_url = f"{BASE_URL}/titles/{title_id}/"
        res = self._session.get(page_url, headers=self.HEADERS, timeout=self.TIMEOUT)
        if res.status_code == HTTPStatus.NOT_FOUND:
            msg = f"{page_url} is not there (404)."
            raise NotAnEpisodePageError(msg)
        res.raise_for_status()
        work = parse_work_page(res.content, page_url)
        self._episodes[title_id] = work.episodes
        return work.episodes


def _authors(item: Mapping[str, Any]) -> str:
    """The reader item's `Authors`, `名前 (役割)` each when a role is given."""
    credited = []
    for author in item.get("Authors") or []:
        if not isinstance(author, dict):
            continue
        name, role = str(author.get("Name") or "").strip(), str(author.get("Role") or "").strip()
        if name:
            credited.append(f"{name} ({role})" if role else name)
    return ", ".join(credited)


def _episode_number(url: str) -> int | None:
    """The `episode=<n>` of a URL, or None when it has none (or not a number)."""
    values = parse_qs(urlparse(url).query).get("episode") or []
    try:
        return int(values[0]) if values else None
    except ValueError:
        return None
