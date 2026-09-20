"""ヤンマガWeb (Kodansha's Young Magazine site), and the SpeedBinb reader it opens episodes in.

The site is a Rails app. A work lives at `/comics/<title>`, the title being
the work's name itself, percent-encoded, and each episode at
`/comics/<title>/<id>` with a 32-hex id. What that episode URL does says
whether the episode can be read:

- A free episode redirects to the reader, `/viewer/comics/<title>/<id>?cid=<cid>`,
  a page that mounts Voyager's SpeedBinb on `#content[data-ptbinb]` with the
  content id in the query.
- A login-only or paid episode answers the episode page itself, a
  `.mod-episode-rental-section` with sign-up buttons and no reader.
- An unknown id redirects back to the work page.

The work page lists only its first and last few episodes; the rest come from
`/comics/<title>/episodes?offset=&limit=&sort=older`, a JavaScript snippet
that inserts one `li.mod-episode-item` per episode, which `parse_listing()`
reads back.

The reader is the one `viewers/speedbinb.py` describes, in its `ServerType` 2 ("Rest")
form: `bibGetCntntInfo` (on the site itself) answers the scramble tables and
a contents server on `sbc.yanmaga.jp`, and sets the CloudFront cookies that
server wants; `<server>/content` is the page list as plain JSON, and
`<server>/img/<src>` the tiled page images, put back together by
`speedbinb.descramble()`. A locked episode opened through the reader URL
answers `bibGetCntntInfo` with `result: 0`.

Sign-in goes through Kodansha ID (`id-members.kodansha.co.jp`, OpenID
Connect on top of Gigya), which is out of reach here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from http import HTTPStatus
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, quote, unquote, urljoin, urlparse

from bs4 import BeautifulSoup
from bs4.element import Tag

from getjmanga.errors import NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractor import Episode, Extractor, Page, neighbours
from getjmanga.viewers import speedbinb

if TYPE_CHECKING:
    from httpx import Client
    from PIL import Image

HOST = "yanmaga.jp"

# A work page: `/comics/<title>`, the title anything but the site's own listings.
_WORK_PATH = re.compile(r"^/comics/(?!(?:series|authors)(?:/|$))(?P<title>[^/]+)/?$")
# An episode page, or the reader page it redirects to.
_EPISODE_PATH = re.compile(r"^/(?:viewer/)?comics/(?!(?:series|authors)/)(?P<title>[^/]+)/(?P<id>[0-9a-f]{32})/?$")


#: How many episodes to ask the listing for at once; the site takes far more than any work has.
_LISTING_LIMIT = 10000

# The reader's element: `data-ptbinb` names the API endpoint.
_VIEWER_SELECTOR = "#content[data-ptbinb]"
# What the episode page shows instead of the reader.
_RENTAL_SELECTOR = ".mod-episode-rental-section"

# One `\x` escape of the JavaScript the listing endpoint answers with.
_JS_ESCAPE = re.compile(r"\\(.)", re.DOTALL)
_JS_UNESCAPED = {"n": "\n", "r": "\r", "t": "\t"}


@dataclass(frozen=True)
class Listed:
    """One entry of a work's episode listing."""

    #: The episode URL, `https://yanmaga.jp/comics/<title>/<id>`.
    url: str
    #: The episode's title.
    title: str
    #: True when the listing marks the entry as wanting a sign-in or a rental.
    locked: bool


def canonical_title(raw: str) -> str:
    """The percent-encoded form of a work title as it appears in a URL path.

    Args:
        raw: The title segment, percent-encoded or not.

    Returns:
        The segment encoded the way the site writes it.
    """
    return quote(unquote(raw), safe="")


def episode_url(title: str, episode_id: str) -> str:
    """The canonical episode URL.

    Args:
        title: The work title segment, in either form.
        episode_id: The episode's 32-hex id.

    Returns:
        `https://yanmaga.jp/comics/<title>/<id>`.
    """
    return f"https://{HOST}/comics/{canonical_title(title)}/{episode_id}"


def work_url(title: str) -> str:
    """The canonical work URL.

    Args:
        title: The work title segment, in either form.

    Returns:
        `https://yanmaga.jp/comics/<title>`.
    """
    return f"https://{HOST}/comics/{canonical_title(title)}"


def parse_listing(text: str) -> list[Listed]:
    """Read the episodes out of what `/comics/<title>/episodes` answers.

    The endpoint answers JavaScript that `insertAdjacentHTML`s one
    `<li class="mod-episode-item">` per episode, in the order asked for.

    Args:
        text: The response body.

    Returns:
        The listed episodes, in the order the site put them, deduplicated.
    """
    html = _JS_ESCAPE.sub(lambda match: _JS_UNESCAPED.get(match[1], match[1]), text)
    soup = BeautifulSoup(html, "html.parser")
    listed: list[Listed] = []
    seen: set[str] = set()
    for item in soup.select("li.mod-episode-item[data-original-url]"):
        match = _EPISODE_PATH.match(urlparse(str(item["data-original-url"])).path)
        if match is None:
            continue
        url = episode_url(match["title"], match["id"])
        if url in seen:
            continue
        seen.add(url)
        classes = item.get("class") or []
        listed.append(
            Listed(
                url=url,
                title=" ".join(str(item.get("data-episode-title") or "").split()),
                locked="js-modal" in classes,
            ),
        )
    return listed


def split_page_title(text: str) -> tuple[str, str]:
    """Split a page `<title>`, `"<work> - <episode> | ヤンマガWeb"`, into its two titles.

    Args:
        text: The `<title>` text.

    Returns:
        The work title and the episode title; the episode title is empty when
        the text has no ` - ` in it.
    """
    head = text.rsplit(" | ", 1)[0].strip()
    series, sep, episode = head.partition(" - ")
    return (series.strip(), episode.strip()) if sep else (head, "")


class YanMaga(Extractor):
    """Fetch episodes from ヤンマガWeb (yanmaga.jp)."""

    NAME = "yanmaga"
    HOSTS = (HOST,)
    URL_FORMS = (
        "https://yanmaga.jp/comics/<title>/<episode-id>",
        "https://yanmaga.jp/viewer/comics/<title>/<episode-id>?cid=<content-id>",
        "https://yanmaga.jp/comics/<title>",
    )

    def __init__(self, session: Client | None = None) -> None:
        """Build an extractor.

        Args:
            session: A session to reuse. A retrying one is made when omitted.
        """
        super().__init__(session)
        # Canonical work URL -> its listing, so a work is listed once per run.
        self._listings: dict[str, list[Listed]] = {}

    @classmethod
    def suitable(cls, url: str) -> bool:
        """Report whether `url` is an episode, a reader or a work page.

        Args:
            url: The URL to check.

        Returns:
            True for the URL shapes in `URL_FORMS`, the title encoded or not.
        """
        if not super().suitable(url):
            return False
        path = urlparse(url).path
        return bool(_EPISODE_PATH.match(path) or _WORK_PATH.match(path))

    @classmethod
    def is_series(cls, url: str) -> bool:
        """Report whether `url` is a work page.

        Args:
            url: The URL to check.

        Returns:
            True for `https://yanmaga.jp/comics/<title>`, whatever the query.
        """
        return cls.suitable(url) and _WORK_PATH.match(urlparse(url).path) is not None

    def series_urls(self, url: str) -> list[str]:
        """List a work's episodes, oldest first, locked ones included.

        Args:
            url: A work page URL.

        Returns:
            One episode URL per listed episode, oldest first.

        Raises:
            UnsupportedUrlError: The URL is not a work page.
            NotAnEpisodePageError: There is no such work, or it lists no episode.
        """
        match = _WORK_PATH.match(urlparse(url).path)
        if match is None:
            msg = f"{url} is not a work page."
            raise UnsupportedUrlError(msg)
        listed = self.listing(match["title"])
        if not listed:
            msg = f"the work at {url} lists no episode."
            raise NotAnEpisodePageError(msg)
        return [entry.url for entry in listed]

    def episode(self, url: str) -> Episode:
        """Read one episode: its titles, its pages and the episode after it.

        Args:
            url: An episode URL, or the reader URL it redirects to.

        Returns:
            The episode. `pages` is empty when the episode wants a sign-in or
            a rental; `next_url` then comes from the work's listing.

        Raises:
            UnsupportedUrlError: The URL is not an episode URL.
            NotAnEpisodePageError: There is no such episode (the site sends
                the browser back to the work page, or answers 404).
        """
        match = _EPISODE_PATH.match(urlparse(url).path)
        if match is None:
            msg = f"{url} is not an episode page."
            raise UnsupportedUrlError(msg)
        title, episode_id = match["title"], match["id"]
        canonical = episode_url(title, episode_id)

        res = self._session.get(canonical, headers=self.HEADERS, timeout=self.TIMEOUT)
        if res.status_code == HTTPStatus.NOT_FOUND:
            msg = f"{url} is not there (404)."
            raise NotAnEpisodePageError(msg)
        res.raise_for_status()
        landed = urlparse(str(res.url or canonical))
        if _EPISODE_PATH.match(landed.path) is None:
            msg = f"{url} is not an episode: the site sent the browser to {landed.geturl()}."
            raise NotAnEpisodePageError(msg)
        soup = BeautifulSoup(res.content, "html.parser")
        viewer = soup.select_one(_VIEWER_SELECTOR)
        content_id = (parse_qs(landed.query).get("cid") or [""])[0]
        if not isinstance(viewer, Tag) or not content_id:
            return self._locked(soup, canonical, title, episode_id)

        reader_url = landed.geturl()
        info_url = urljoin(reader_url, str(viewer["data-ptbinb"]))
        content = speedbinb.content_info(
            self,
            info_url,
            content_id,
            referer=reader_url,
            server_types=frozenset({speedbinb.SERVER_TYPE_DIRECT, speedbinb.SERVER_TYPE_REST}),
        )
        if content is None:
            # The reader page renders for a locked episode too; the API is what says no.
            return self._locked(soup, canonical, title, episode_id, content_id=content_id)

        book = speedbinb.page_list(self, content, referer=reader_url)

        page_series, page_episode = split_page_title(soup.title.get_text() if soup.title else "")
        item = content.item
        preceding, following = item.get("PrevEpisode"), item.get("NextEpisode")
        prev_path = preceding.get("ViewerPath") if isinstance(preceding, dict) else None
        next_path = following.get("ViewerPath") if isinstance(following, dict) else None
        return Episode(
            url=canonical,
            series_title=str(item.get("ParentTitle") or page_series),
            episode_title=str(item.get("Title") or page_episode or episode_id),
            pages=book.pages,
            prev_url=_episode_url_of(urljoin(canonical, str(prev_path))) if prev_path else None,
            next_url=_episode_url_of(urljoin(canonical, str(next_path))) if next_path else None,
            metadata={
                "episode_id": episode_id,
                "content_id": content_id,
                "contents_server": content.server,
                "locked": False,
                "info": content.info,
            },
        )

    def image(self, page: Page, episode: Episode) -> Image.Image:
        """Fetch one page and put its tiles back where they belong.

        Args:
            page: The page to fetch.
            episode: The episode the page belongs to.

        Returns:
            The page in reading order, padding gone.
        """
        return speedbinb.fetch_page(self, page, referer=episode.url)

    def listing(self, title: str) -> list[Listed]:
        """List a work's episodes, oldest first, fetched once per work.

        Args:
            title: The work title segment, in either form.

        Returns:
            The listed episodes, oldest first.

        Raises:
            NotAnEpisodePageError: There is no such work (404).
        """
        base = work_url(title)
        cached = self._listings.get(base)
        if cached is not None:
            return cached
        res = self._session.get(
            f"{base}/episodes",
            params={"offset": 0, "limit": _LISTING_LIMIT, "sort": "older"},
            headers={**self.HEADERS, "Referer": base},
            timeout=self.TIMEOUT,
        )
        if res.status_code == HTTPStatus.NOT_FOUND:
            msg = f"no work at {base} (404)."
            raise NotAnEpisodePageError(msg)
        res.raise_for_status()
        listed = parse_listing(res.text)
        self._listings[base] = listed
        return listed

    def _locked(
        self,
        soup: BeautifulSoup,
        canonical: str,
        title: str,
        episode_id: str,
        content_id: str | None = None,
    ) -> Episode:
        """An episode the site will not open: titles off the page, the next one off the listing."""
        page_series, page_episode = split_page_title(soup.title.get_text() if soup.title else "")
        heading = soup.select_one(f"{_RENTAL_SELECTOR}-title")
        # The rental section names the episode either bare or inside a sign-up lead's `<span>`.
        subheading = soup.select_one(f"{_RENTAL_SELECTOR}-eptitle span") or soup.select_one(
            f"{_RENTAL_SELECTOR}-eptitle",
        )
        series_title = _text(heading) or page_series
        episode_title = page_episode or _text(subheading)
        if not series_title and not episode_title:
            msg = f"no episode on {canonical}."
            raise NotAnEpisodePageError(msg)
        listed = self.listing(title)
        urls = [entry.url for entry in listed]
        if canonical in urls:
            episode_title = listed[urls.index(canonical)].title or episode_title
        prev_url, next_url = neighbours(urls, canonical)
        return Episode(
            url=canonical,
            series_title=series_title,
            episode_title=episode_title or episode_id,
            prev_url=prev_url,
            next_url=next_url,
            metadata={"episode_id": episode_id, "content_id": content_id, "locked": True},
        )


def _episode_url_of(url: str) -> str:
    """`url` in its canonical form when it is an episode URL, else as it is."""
    match = _EPISODE_PATH.match(urlparse(url).path)
    return episode_url(match["title"], match["id"]) if match else url


def _text(tag: Tag | None) -> str:
    return " ".join(tag.get_text(" ", strip=True).split()) if isinstance(tag, Tag) else ""
