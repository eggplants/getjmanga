"""新都社 (neetsha.jp), the doujin web comic and novel site.

A hand-written PHP site with no viewer script, no API, no scrambling, no
paywall and no age gate: everything is free to read, and plain `http://` is
still served (https answers the same, and `www.neetsha.jp` is an alias).

- A work page, `/inside/comic.php?id=<id>`, names the work in `h1`, links the
  author (`main.php?author=`) and the magazine it runs in (`main.php?magazine=`),
  and lists the work's stories oldest first in `table.story`, each a
  `comic.php?id=<id>&story=<n>` link holding the story's title, next to a
  `comic2p.php` link to the two-page-spread layout of the same story.
- A story page, `/inside/comic.php?id=<id>&story=<n>`, is one episode: `h1`
  holds `<work><br><story title>`, every page is a `div.image` holding one
  `<img>`, and `a.prev` / `a.next` link the neighbouring stories. `<n>` is the
  number of the story's first page in the work, not a running story number.
- `comic2p.php?id=<id>&story=<n>` shows the same pages laid out two to a row
  in spread order, so it is read as its `comic.php` counterpart.
- A story number that names no story redirects to the work page; an id that
  names no work is a 404 (or a 500 on the oldest, deleted ids); a work hosted
  off-site redirects to the author's own domain.
- The page images are plain JPEG/PNG/GIF files under `/inside/up/`, served
  without a Referer or a cookie.

The pages declare `<meta charset="UTF-8">` and are served as UTF-8; they are
handed to BeautifulSoup as bytes so the declared charset, not a guess, decodes
them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from http import HTTPStatus
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup
from bs4.element import Tag

from getjmanga.errors import NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractor import Episode, Extractor, Page

if TYPE_CHECKING:
    from httpx import Response

# A work or story page: `/inside/comic.php`, or `/inside/comic2p.php` for the spread layout.
_COMIC_PATH = re.compile(r"^/inside/comic(?:2p)?\.php$")


@dataclass(frozen=True)
class ComicUrl:
    """What a `comic.php` URL names."""

    #: The work id.
    work: str
    #: The story number, or None for the work page.
    story: str | None


@dataclass(frozen=True)
class Story:
    """What a story page says."""

    #: The work's title: `h1` up to the line break.
    series_title: str
    #: The story's title: `h1` after the line break.
    episode_title: str
    #: The author, from the `main.php?author=` link.
    author: str
    #: The magazine the work runs in, from the `main.php?magazine=` link.
    magazine: str
    #: The page images in reading order, absolute, deduplicated.
    images: tuple[str, ...]
    #: The previous story, absolute, or None on the first.
    prev_url: str | None
    #: The next story, absolute, or None on the latest.
    next_url: str | None


@dataclass(frozen=True)
class Work:
    """What a work page says."""

    #: The work's title, from `h1`.
    title: str
    #: Story URL (`comic.php?id=<id>&story=<n>`, absolute) -> the title the listing gives it, oldest first.
    titles: dict[str, str]

    @property
    def urls(self) -> list[str]:
        """The story URLs, oldest first."""
        return list(self.titles)


def comic_url(url: str) -> ComicUrl | None:
    """Read the work id and story number off a `comic.php` URL.

    Args:
        url: The URL to read.

    Returns:
        What the URL names, or None when it is not a `comic.php` URL with a numeric `id`.
    """
    parsed = urlparse(url)
    if _COMIC_PATH.match(parsed.path) is None:
        return None
    query = parse_qs(parsed.query)
    ids = query.get("id", [])
    if not ids or not ids[0].isdigit():
        return None
    stories = query.get("story", [])
    story = stories[0] if stories and stories[0].isdigit() else None
    return ComicUrl(work=ids[0], story=story)


def story_url(url: str) -> str | None:
    """The canonical story URL of `url`, or None when it names no story.

    Args:
        url: A story URL, in either layout.

    Returns:
        `/inside/comic.php?id=<id>&story=<n>` on the scheme and host given.
    """
    named = comic_url(url)
    if named is None or named.story is None:
        return None
    return f"{_origin(url)}/inside/comic.php?id={named.work}&story={named.story}"


def work_url(url: str) -> str | None:
    """The canonical work page URL of `url`, or None when it names no work.

    Args:
        url: A work or story URL.

    Returns:
        `/inside/comic.php?id=<id>` on the scheme and host given.
    """
    named = comic_url(url)
    if named is None:
        return None
    return f"{_origin(url)}/inside/comic.php?id={named.work}"


def parse_story(html: str | bytes, url: str) -> Story:
    """Read a story page.

    Args:
        html: The page.
        url: The URL it came from, to resolve the links against.

    Returns:
        What the page says. `images` is empty when the story shows no page.

    Raises:
        NotAnEpisodePageError: The page has no story heading, so it is not a story.
    """
    soup = BeautifulSoup(html, "html.parser")
    main = soup.find(id="main")
    heading = main.find("h1") if isinstance(main, Tag) else None
    if not isinstance(main, Tag) or not isinstance(heading, Tag) or main.find("table", class_="story") is not None:
        msg = f"no story on {url}."
        raise NotAnEpisodePageError(msg)
    # `<h1>work<br>story</h1>`: the line break splits the two titles.
    titles = [part.strip() for part in heading.get_text("\n").split("\n")]
    titles = [part for part in titles if part]
    series_title = titles[0] if titles else ""
    episode_title = " ".join(titles[1:])
    images: dict[str, None] = {}
    for block in main.find_all("div", class_="image"):
        if not isinstance(block, Tag):
            continue
        for img in block.find_all("img", src=True):
            if isinstance(img, Tag):
                images[urljoin(url, str(img["src"]).strip())] = None
    return Story(
        series_title=series_title,
        episode_title=episode_title,
        author=_link_text(soup, "author"),
        magazine=_link_text(soup, "magazine"),
        images=tuple(images),
        prev_url=_neighbour(soup, "prev", url),
        next_url=_neighbour(soup, "next", url),
    )


def parse_work(html: str | bytes, url: str) -> Work:
    """Read the stories a work page lists.

    The listing holds a thumbnail link, a title link and a spread-layout
    link per story; the title link is the one with text, and only links
    into the page's own work count.

    Args:
        html: The work page.
        url: The URL it came from, to resolve the links against.

    Returns:
        The listing, oldest first as the site orders it, deduplicated.
    """
    named = comic_url(url)
    soup = BeautifulSoup(html, "html.parser")
    main = soup.find(id="main")
    heading = main.find("h1") if isinstance(main, Tag) else None
    title = heading.get_text(strip=True) if isinstance(heading, Tag) else ""
    titles: dict[str, str] = {}
    table = main.find("table", class_="story") if isinstance(main, Tag) else None
    anchors = table.find_all("a", href=True) if isinstance(table, Tag) else []
    for anchor in anchors:
        if not isinstance(anchor, Tag):
            continue
        href = urljoin(url, str(anchor["href"]).strip())
        linked = comic_url(href)
        if linked is None or linked.story is None or (named is not None and linked.work != named.work):
            continue
        key = f"{_origin(url)}/inside/comic.php?id={linked.work}&story={linked.story}"
        text = anchor.get_text(strip=True)
        # Keep the first link with a title; a thumbnail link has none.
        if text and text != "[見開き]" and not titles.get(key):
            titles[key] = text
        elif key not in titles:
            titles[key] = ""
    return Work(title=title, titles=titles)


def _origin(url: str) -> str:
    """The `scheme://host` part of `url`."""
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def _link_text(soup: BeautifulSoup, parameter: str) -> str:
    """The text of the first `main.php?<parameter>=` link with any: the magazine's first link is an image."""
    for anchor in soup.find_all("a", href=True):
        if not isinstance(anchor, Tag):
            continue
        parsed = urlparse(str(anchor["href"]))
        text = anchor.get_text(strip=True)
        if text and parsed.path.endswith("main.php") and parameter in parse_qs(parsed.query):
            return text
    return ""


def _neighbour(soup: BeautifulSoup, direction: str, url: str) -> str | None:
    """The `a.prev` / `a.next` link, absolute and in the `comic.php` layout."""
    anchor = soup.find("a", class_=direction, href=True)
    if not isinstance(anchor, Tag):
        return None
    return story_url(urljoin(url, str(anchor["href"]).strip()))


class Neetsha(Extractor):
    """Fetch stories from 新都社.

    A story page lists its own pages and links its neighbours, so it is read
    on its own; the work page is only read to list a series.
    """

    NAME = "neetsha"
    HOSTS = ("neetsha.jp", "www.neetsha.jp")
    PUBLISHER = "新都社"
    URL_FORMS = (
        "http://neetsha.jp/inside/comic.php?id=<id>&story=<n>",
        "http://neetsha.jp/inside/comic2p.php?id=<id>&story=<n>",
        "http://neetsha.jp/inside/comic.php?id=<id>",
    )

    @classmethod
    def suitable(cls, url: str) -> bool:
        """Report whether the extractor takes `url`.

        The site is served over plain http (https answers too), so unlike
        the default this takes http as well as https.

        Args:
            url: The URL to check.

        Returns:
            True for a work or story URL on a known host.
        """
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or parsed.hostname not in cls.HOSTS:
            return False
        return comic_url(url) is not None

    @classmethod
    def is_series(cls, url: str) -> bool:
        """Report whether `url` is a work page.

        Args:
            url: The URL to check.

        Returns:
            True for `/inside/comic.php?id=<id>` without a `story`.
        """
        named = comic_url(url)
        return named is not None and named.story is None

    def series_urls(self, url: str) -> list[str]:
        """List every story a work page links, oldest first.

        Args:
            url: The work page URL.

        Returns:
            The story URLs.

        Raises:
            UnsupportedUrlError: The URL is not a work page.
            NotAnEpisodePageError: The work is gone, hosted elsewhere, or lists no story.
        """
        key = work_url(url) if self.is_series(url) else None
        if key is None:
            msg = f"{url} is not a series page."
            raise UnsupportedUrlError(msg)
        res = self._fetch(key)
        work = parse_work(res.content, key)
        if not work.titles:
            msg = f"the series at {url} lists no episode."
            raise NotAnEpisodePageError(msg)
        return work.urls

    def episode(self, url: str) -> Episode:
        """Read one story and list its pages.

        Args:
            url: The story URL, in either layout.

        Returns:
            The story. Nothing on the site is locked, so `pages` is only
            empty for a story that shows no page.

        Raises:
            NotAnEpisodePageError: The URL is not a story page, or the story is gone.
        """
        key = story_url(url)
        if key is None:
            msg = f"{url} is not an episode page."
            raise NotAnEpisodePageError(msg)
        res = self._fetch(key)
        named = comic_url(key)
        story = parse_story(res.content, key)
        return self._dated_by_upload(
            Episode(
                url=key,
                series_title=story.series_title or (named.work if named else ""),
                episode_title=story.episode_title or f"story {named.story if named else ''}",
                pages=tuple(Page(url=src) for src in story.images),
                prev_url=story.prev_url,
                next_url=story.next_url,
                metadata={
                    "id": named.work if named else "",
                    "story": named.story if named else "",
                    "author": story.author,
                    "magazine": story.magazine,
                    "work_url": work_url(key),
                    "prev_url": story.prev_url,
                    "next_url": story.next_url,
                    "images": list(story.images),
                },
                writer=story.author,
                publisher=self.PUBLISHER,
            )
        )

    def _fetch(self, url: str) -> Response:
        """GET a work or story page, turning what is not one into `NotAnEpisodePageError`.

        A missing work is a 404 (a 500 on the oldest ids), a work hosted
        elsewhere redirects off the site, and a story number that names no
        story redirects to the work page.
        """
        res = self._session.get(url, headers=self.HEADERS, timeout=self.TIMEOUT)
        if res.status_code in (HTTPStatus.NOT_FOUND, HTTPStatus.INTERNAL_SERVER_ERROR):
            msg = f"{url} is gone (HTTP {res.status_code}): not a work nor a story."
            raise NotAnEpisodePageError(msg)
        res.raise_for_status()
        landed = str(res.url or url)
        if urlparse(landed).hostname not in self.HOSTS:
            msg = f"{url} is hosted elsewhere ({landed}): not a story the site serves."
            raise NotAnEpisodePageError(msg)
        if landed != url and comic_url(landed) != comic_url(url):
            msg = f"{url} names no story: the site sent it to {landed}."
            raise NotAnEpisodePageError(msg)
        return res
