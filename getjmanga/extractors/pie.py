"""PIE COMICS (パイ インターナショナル): WordPress work pages, episodes on the site or on YONDEMILL.

A work page at `https://comics.pie.co.jp/series/<slug>/` lists its episodes
in one or more `section.p-series` blocks (the running ones, then one per
collected volume). Each entry is either a story on the site itself
(`https://comics.pie.co.jp/story/<slug>`, a WordPress post whose images sit
inline in `.c-content`), a YONDEMILL content
(`https://www.yondemill.jp/contents/<id>?view=1`, Voyager's SpeedBinb reader
behind Toko-Ai's shared ebook platform, which `ohta.py` already reads), a shop
link for an episode sold as a single (Amazon), or a `p-series_nolink` span for
one that is not public any more. Most work pages run newest first, a few
oldest first, so the dates decide.

A story page names the next post of the same series itself, so it needs no
work page; a YONDEMILL content is looked up on its work page (the content
page links back to it through `pie.co.jp/series/<n>/`) for its listed title
and what follows it. The images are plain files: no cookie, no Referer
check. WordPress serves a `-scaled` copy of a big upload; the original next
to it is fetched when it is there. There are no reader accounts, so there is
no `login()`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from http import HTTPStatus
from io import BytesIO
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from bs4.element import Tag
from PIL import Image

from getjmanga.errors import NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractor import Episode, Extractor, Page

from .ohta import Ohta, content_url

if TYPE_CHECKING:
    from requests import Session

#: Where the work and story pages are.
HOST = "comics.pie.co.jp"
#: The publisher's own site, whose `/series/<n>/` redirects to a work page.
PUBLISHER_HOST = "pie.co.jp"
#: The suffix the site appends to every `<title>`.
_TITLE_SUFFIX = " | PIE COMICS"

# A work page and a story: one slug each, possibly percent-encoded Japanese.
_SERIES_PATH = re.compile(r"^/series/(?P<slug>[^/]+)/?$")
_STORY_PATH = re.compile(r"^/story/(?P<slug>[^/]+)/?$")
# `2026.09.02更新` on a listing entry and a story header.
_DATE = re.compile(r"(?P<date>\d{2,4}\.\d{2}\.\d{2})")
# WordPress's scaled-down copy of a big upload; the original sits next to it.
_SCALED = re.compile(r"-scaled(?P<ext>\.\w+)$")


@dataclass(frozen=True)
class Listed:
    """One entry of a work page's episode list."""

    #: The listed title, `【無料】第1話 まどろみの星`.
    title: str
    #: The listed date, `2021.07.09`.
    date: str
    #: The href of the entry, whatever it points at; None for an unlinked one.
    href: str | None
    #: The episode URL when the entry is readable: a story, or a YONDEMILL content (canonical).
    url: str | None


@dataclass(frozen=True)
class Work:
    """What a work page says."""

    #: The work page URL.
    url: str
    #: `h1.p-work_title`: the work's title.
    title: str
    #: `p.p-work_author`.
    author: str
    #: Every listed entry, oldest first.
    entries: tuple[Listed, ...]

    @property
    def urls(self) -> list[str]:
        """The readable episode URLs, oldest first, deduplicated."""
        return list(dict.fromkeys(entry.url for entry in self.entries if entry.url))

    def next_url(self, url: str) -> str | None:
        """The readable episode after `url`, or None when it is the last (or unlisted)."""
        urls = self.urls
        if url not in urls:
            return None
        index = urls.index(url) + 1
        return urls[index] if index < len(urls) else None

    def listed_title(self, url: str) -> str:
        """The listed title of the episode at `url`, or `""` when it is not listed."""
        return next((entry.title for entry in self.entries if entry.url == url), "")


@dataclass(frozen=True)
class Story:
    """What a story page says."""

    #: The story page URL.
    url: str
    #: The post's title, `<title>` without the site's suffix: `【Part 1】Alicia`.
    title: str
    #: `h2.c-contentBlock_title`: the short heading, `Alicia`.
    heading: str
    #: The work's title, from the breadcrumb.
    series_title: str
    #: The work page the breadcrumb points at, when it does.
    series_url: str | None
    #: The date in the header, `26.02.04`.
    updated: str
    #: The image URLs in `.c-content`, in page order.
    images: tuple[str, ...]
    #: The previous and the next post of the same work, when the page names them.
    prev_url: str | None
    next_url: str | None


def episode_url(href: str) -> str | None:
    """The episode URL an entry's href stands for.

    Args:
        href: The href, absolute.

    Returns:
        The story URL as it is, the canonical content URL for a YONDEMILL
        link, None for a shop or anything else.
    """
    parsed = urlparse(href)
    if parsed.scheme != "https":
        return None
    if parsed.hostname == HOST:
        return href if _STORY_PATH.match(parsed.path) else None
    return content_url(href)


def parse_work(html: str | bytes, url: str) -> Work:
    """Read a work page into a `Work`.

    Args:
        html: The page.
        url: The URL it came from, to resolve links against.

    Returns:
        The titles and the listed episodes, oldest first.

    Raises:
        NotAnEpisodePageError: The page is not a work page.
    """
    soup = BeautifulSoup(html, "html.parser")
    heading = soup.select_one("h1.p-work_title")
    if not isinstance(heading, Tag):
        msg = f"no work page at {url}."
        raise NotAnEpisodePageError(msg)
    author = soup.select_one("p.p-work_author")

    entries = []
    for item in soup.select("section.p-series li.p-series_item"):
        anchor = item.select_one("a.p-series_link[href]")
        title = item.select_one(".p-series_itemTitle")
        time = item.select_one("time")
        date = _DATE.search(time.get_text() if isinstance(time, Tag) else "")
        href = urljoin(url, str(anchor["href"])) if isinstance(anchor, Tag) else None
        entries.append(
            Listed(
                title=title.get_text(strip=True) if isinstance(title, Tag) else "",
                date=date["date"] if date else "",
                href=href,
                url=episode_url(href) if href else None,
            ),
        )
    dates = [entry.date for entry in entries if entry.date]
    # Most work pages list newest first; the first and the last date tell.
    if len(dates) > 1 and dates[0] > dates[-1]:
        entries.reverse()
    return Work(
        url=url,
        title=heading.get_text(strip=True),
        author=author.get_text(strip=True) if isinstance(author, Tag) else "",
        entries=tuple(entries),
    )


def parse_story(html: str | bytes, url: str) -> Story:
    """Read a story page into a `Story`.

    Args:
        html: The page.
        url: The URL it came from, to resolve links against.

    Returns:
        The titles, the images and the neighbouring posts.

    Raises:
        NotAnEpisodePageError: The page is not a story.
    """
    soup = BeautifulSoup(html, "html.parser")
    content = soup.select_one(".c-contentBlock .c-content")
    if not isinstance(content, Tag):
        msg = f"no story on {url}."
        raise NotAnEpisodePageError(msg)

    title = soup.find("title")
    heading = soup.select_one("h2.c-contentBlock_title")
    header = soup.select_one("h1.p-work_headerTitle")
    updated = _DATE.search(header.get_text() if isinstance(header, Tag) else "")

    series_title = ""
    series_url = None
    for anchor in soup.select(".p-work_nav a[href]"):
        href = urljoin(url, str(anchor["href"]))
        if urlparse(href).hostname == HOST and _SERIES_PATH.match(urlparse(href).path):
            series_title = anchor.get_text(strip=True)
            series_url = href
            break
    if not series_title and isinstance(header, Tag):
        for span in header.find_all("span"):
            span.decompose()
        series_title = header.get_text(strip=True)

    images = tuple(urljoin(url, str(img["src"])) for img in content.select("img[src]"))
    return Story(
        url=url,
        title=(title.get_text(strip=True).removesuffix(_TITLE_SUFFIX) if isinstance(title, Tag) else ""),
        heading=heading.get_text(strip=True) if isinstance(heading, Tag) else "",
        series_title=series_title,
        series_url=series_url,
        updated=updated["date"] if updated else "",
        images=images,
        prev_url=_neighbour(soup, url, "prev"),
        next_url=_neighbour(soup, url, "next"),
    )


def _neighbour(soup: BeautifulSoup, url: str, which: str) -> str | None:
    """The `c-prevNext_<which>` link, or None when it is not a link."""
    anchor = soup.select_one(f".c-prevNext_{which} a[href]")
    return urljoin(url, str(anchor["href"])) if isinstance(anchor, Tag) else None


def original_url(url: str) -> str | None:
    """The unscaled upload a `-scaled` image URL was made from, or None for any other."""
    return _SCALED.sub(r"\g<ext>", url) if _SCALED.search(url) else None


class Pie(Extractor):
    """Fetch episodes from PIE COMICS.

    A story on the site is read off its page. A YONDEMILL content is read
    through `Ohta`, which knows the reader, and titled after the work page
    that lists it; `suitable()` leaves bare YONDEMILL URLs to `Ohta`, but
    `episode()` takes the ones a work page lists. Work pages are fetched once
    per run.
    """

    NAME = "pie"
    HOSTS = (HOST,)
    URL_FORMS = (
        "https://comics.pie.co.jp/story/<slug>",
        "https://comics.pie.co.jp/series/<slug>/",
    )

    def __init__(self, session: Session | None = None) -> None:
        """Build an extractor.

        Args:
            session: A session to reuse. A retrying one is made when omitted.
        """
        super().__init__(session)
        self._ohta = Ohta(self._session)
        # Work page URL -> what it listed, so a work page is read once.
        self._works: dict[str, Work] = {}
        # Episode URL -> the work page that lists it.
        self._listed: dict[str, str] = {}

    @classmethod
    def suitable(cls, url: str) -> bool:
        """Report whether `url` is a story or a work page on the site.

        Args:
            url: The URL to check.

        Returns:
            True for `https://comics.pie.co.jp/story/<slug>` and
            `https://comics.pie.co.jp/series/<slug>/`.
        """
        if not super().suitable(url):
            return False
        path = urlparse(url).path
        return bool(_STORY_PATH.match(path) or _SERIES_PATH.match(path))

    @classmethod
    def is_series(cls, url: str) -> bool:
        """Report whether `url` is a work page.

        Args:
            url: The URL to check.

        Returns:
            True for `https://comics.pie.co.jp/series/<slug>/`.
        """
        parsed = urlparse(url)
        return parsed.hostname == HOST and _SERIES_PATH.match(parsed.path) is not None

    def series_urls(self, url: str) -> list[str]:
        """List the readable episodes a work page lists, oldest first.

        Args:
            url: A work page URL.

        Returns:
            One story or YONDEMILL content URL per readable episode.

        Raises:
            UnsupportedUrlError: The URL is not a work page.
            NotAnEpisodePageError: The page lists no readable episode.
        """
        if not self.is_series(url):
            msg = f"{url} is not a work page."
            raise UnsupportedUrlError(msg)
        work = self._work(url)
        if not work.urls:
            msg = f"the work at {url} lists no readable episode."
            raise NotAnEpisodePageError(msg)
        return work.urls

    def episode(self, url: str) -> Episode:
        """Read one episode: its titles, its pages and the episode after it.

        Args:
            url: A story URL, or a YONDEMILL content URL a work page lists.

        Returns:
            The episode. `pages` is empty when a story carries no image, or
            when YONDEMILL does not open the reader on the content.

        Raises:
            UnsupportedUrlError: The URL is neither a story nor a content.
            NotAnEpisodePageError: The site has no such page (404), or the
                page describes no episode.
        """
        parsed = urlparse(url)
        if parsed.hostname == HOST and _STORY_PATH.match(parsed.path):
            return self._story(url)
        canonical = content_url(url)
        if canonical is None:
            msg = f"{url} is not an episode page."
            raise UnsupportedUrlError(msg)
        return self._content(canonical)

    def image(self, page: Page, episode: Episode) -> Image.Image:
        """Fetch one page: a story's image (the original when there is one), or a reader page put back together.

        Args:
            page: The page to fetch.
            episode: The episode the page belongs to.

        Returns:
            The page image.
        """
        if "ctbl" in page.extra:
            return self._ohta.image(page, episode)
        original = page.extra.get("original")
        if original:
            res = self._session.get(original, headers=self.HEADERS, timeout=self.IMAGE_TIMEOUT)
            if res.ok and str(res.headers.get("content-type", "")).startswith("image/"):
                return Image.open(BytesIO(res.content))
        return self._fetch_image(page.url, headers={**self.HEADERS, "Referer": episode.url})

    def _story(self, url: str) -> Episode:
        """Read a story page."""
        res = self._session.get(url, headers=self.HEADERS, timeout=self.TIMEOUT)
        if res.status_code == HTTPStatus.NOT_FOUND:
            msg = f"{url} is gone (HTTP 404): no such story."
            raise NotAnEpisodePageError(msg)
        res.raise_for_status()
        story = parse_story(res.content, url)
        pages = tuple(
            Page(url=src, extra={"original": original} if (original := original_url(src)) else {})
            for src in story.images
        )
        return Episode(
            url=url,
            series_title=story.series_title or story.title,
            episode_title=story.title or story.heading,
            pages=pages,
            next_url=story.next_url,
            metadata={
                "kind": "story",
                "title": story.title,
                "heading": story.heading,
                "series_url": story.series_url,
                "updated": story.updated,
                "prev_url": story.prev_url,
            },
        )

    def _content(self, canonical: str) -> Episode:
        """Read a YONDEMILL content through `Ohta`, titled after the work page that lists it."""
        work = self._work_of(canonical)
        episode = self._ohta.episode(canonical)
        if work is None:
            return replace(episode, metadata={**episode.metadata, "kind": "yondemill"})
        return replace(
            episode,
            series_title=work.title,
            episode_title=work.listed_title(canonical) or episode.episode_title,
            next_url=work.next_url(canonical),
            metadata={**episode.metadata, "kind": "yondemill", "work_url": work.url, "author": work.author},
        )

    def _work(self, url: str) -> Work:
        """Read a work page, once per URL."""
        key = _work_key(url)
        if key not in self._works:
            res = self._session.get(key, headers=self.HEADERS, timeout=self.TIMEOUT)
            if res.status_code == HTTPStatus.NOT_FOUND:
                msg = f"{url} is gone (HTTP 404): not a work page."
                raise NotAnEpisodePageError(msg)
            res.raise_for_status()
            self._remember(parse_work(res.content, str(res.url or key)), key)
        return self._works[key]

    def _work_of(self, canonical: str) -> Work | None:
        """The work page listing a YONDEMILL content: the one already read, else the one the content links back to."""
        if canonical not in self._listed:
            res = self._session.get(canonical, headers=self.HEADERS, timeout=self.TIMEOUT)
            if res.status_code == HTTPStatus.NOT_FOUND:
                msg = f"{canonical} is gone (HTTP 404): YONDEMILL no longer serves it."
                raise NotAnEpisodePageError(msg)
            res.raise_for_status()
            work_url = work_link(res.content, canonical)
            if work_url is None:
                return None
            # `pie.co.jp/series/<n>/` redirects to the work page; anything else is not one.
            work_res = self._session.get(work_url, headers=self.HEADERS, timeout=self.TIMEOUT)
            if not work_res.ok or not self.is_series(str(work_res.url or "")):
                return None
            work = parse_work(work_res.content, str(work_res.url))
            key = self._remember(work, _work_key(work.url))
            self._listed.setdefault(canonical, key)
        return self._works.get(self._listed[canonical])

    def _remember(self, work: Work, key: str) -> str:
        """Cache a work page under `key`, and every episode it lists under that key."""
        self._works[key] = work
        for url in work.urls:
            self._listed.setdefault(url, key)
        return key


def _work_key(url: str) -> str:
    """A work page URL with its trailing slash and without a query, the way the site canonicalises it."""
    return urljoin(url, urlparse(url).path.rstrip("/") + "/")


def work_link(html: str | bytes, url: str) -> str | None:
    """The link back to the publisher a YONDEMILL content page carries, or None.

    Args:
        html: The content page.
        url: The URL it came from, to resolve links against.

    Returns:
        A `https://pie.co.jp/series/<n>/` (redirects to the work page) or a
        `https://comics.pie.co.jp/series/<slug>/` link.
    """
    soup = BeautifulSoup(html, "html.parser")
    for anchor in soup.select("a[href]"):
        href = urljoin(url, str(anchor["href"]))
        parsed = urlparse(href)
        hosts = (HOST, PUBLISHER_HOST, f"www.{PUBLISHER_HOST}")
        if parsed.scheme == "https" and parsed.hostname in hosts and _SERIES_PATH.match(parsed.path):
            return href
    return None
