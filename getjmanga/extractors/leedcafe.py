"""LEED Cafe (リイド社): a WordPress site whose episodes are plain image galleries.

A work lives at `/webcomicinfo/<slug>/` and an episode at `/webcomic/<slug>/`,
the slug being the Japanese title, percent-encoded the way WordPress does it
(a raw one works too). An episode page carries its pages as a run of
`jQuery('.gallery').append('<img ... src="...">')` calls at the bottom, one
per page in reading order: ordinary uploads under `/wp-content/uploads/`,
no scrambling, no Referer or cookie check. Its footer navigation names the
previous and the next episode, and the header links back to the work.

A work page lists its episodes through the Ajax Load More plugin: the list
is empty in the HTML, and the browser fetches it from
`/wp-admin/admin-ajax.php?action=alm_get_posts` with the work's post id as
`meta_value`, newest first by default. The extractor asks for it oldest
first, 100 at a time, and keeps it per work, so an episode's `next_url` can
come from the listing (the footer arrow is missing on some pages) and a
series run needs the listing only once.

Everything on the site is free; an episode whose free run has ended is
deleted outright (HTTP 404), and there is no account to sign in to.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from http import HTTPStatus
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, unquote, urljoin, urlparse

from bs4 import BeautifulSoup
from bs4.element import Tag

from .common import Episode, Extractor, NotAnEpisodePageError, Page, UnsupportedUrlError

if TYPE_CHECKING:
    from requests import Session

HOST = "leedcafe.com"

#: The Ajax Load More endpoint a work page fetches its episode list from.
ALM_URL = f"https://{HOST}/wp-admin/admin-ajax.php"
#: How many episodes to ask the listing for per call.
_LISTING_PAGE_SIZE = 100
#: A ceiling on listing calls, so a listing that keeps promising more cannot loop forever.
_LISTING_MAX_PAGES = 50

# An episode page and a work page: one slug each, raw or percent-encoded.
_EPISODE_PATH = re.compile(r"^/webcomic/(?P<slug>[^/]+)/?$")
_WORK_PATH = re.compile(r"^/webcomicinfo/(?P<slug>[^/]+)/?$")

# One page image, as the episode page appends it to the gallery.
_GALLERY_IMAGE = re.compile(r"jQuery\(\s*['\"]\.gallery['\"]\s*\)\.append\(\s*'(?P<img><img [^']*)'\s*\)")
_ATTR = re.compile(r"""(?P<name>[\w-]+)\s*=\s*"(?P<value>[^"]*)\"""")
# `47. <title>`: the counter the episode heading starts with.
_HEADING_COUNTER = re.compile(r"^\s*(?P<number>\d+)\.\s*(?P<title>.*)$", re.DOTALL)
# `post-81705` on the episode's `article`.
_POST_ID = re.compile(r"^post-(?P<id>\d+)$")


@dataclass(frozen=True)
class EpisodePage:
    """What an episode page says."""

    #: The URL the page was served at, redirects followed.
    url: str
    #: The WordPress post id, from `article#post-<id>`.
    post_id: str
    #: `h1#title-comic` without its `<n>. ` counter.
    title: str
    #: The counter in front of the heading, `0` when there is none.
    number: int
    #: The work page the header links back to.
    work_url: str | None
    #: The page images in reading order, `(url, width, height)` each.
    images: tuple[tuple[str, int, int], ...]
    #: The footer's previous-episode link.
    prev_url: str | None
    #: The footer's next-episode link.
    next_url: str | None
    #: The `更新日` date, ISO formatted, when shown.
    updated: str | None


@dataclass(frozen=True)
class Work:
    """What a work page says, before its episode list is fetched."""

    #: The URL the page was served at, redirects followed.
    url: str
    #: The WordPress post id the episode list is keyed on.
    post_id: str
    #: The work's title.
    title: str


def episode_slug(url: str) -> str | None:
    """The decoded slug of an episode URL, or None for another shape."""
    match = _EPISODE_PATH.match(urlparse(url).path)
    return unquote(match["slug"]) if match else None


def work_slug(url: str) -> str | None:
    """The decoded slug of a work URL, or None for another shape."""
    match = _WORK_PATH.match(urlparse(url).path)
    return unquote(match["slug"]) if match else None


def episode_url(slug: str) -> str:
    """The canonical `https://leedcafe.com/webcomic/<slug>/` of a decoded slug."""
    return f"https://{HOST}/webcomic/{quote(slug, safe='')}/"


def work_url(slug: str) -> str:
    """The canonical `https://leedcafe.com/webcomicinfo/<slug>/` of a decoded slug."""
    return f"https://{HOST}/webcomicinfo/{quote(slug, safe='')}/"


def parse_episode_page(html: str | bytes, url: str) -> EpisodePage:
    """Read an episode page into an `EpisodePage`.

    Args:
        html: The page.
        url: The URL it came from, to resolve links against.

    Returns:
        The titles, the page images and the navigation links.

    Raises:
        NotAnEpisodePageError: The page has no episode heading and no gallery.
    """
    text = _text(html)
    soup = BeautifulSoup(text, "html.parser")
    heading = soup.select_one("h1#title-comic")
    gallery = soup.select_one("div.gallery")
    if not isinstance(heading, Tag) and not isinstance(gallery, Tag):
        msg = f"no episode on {url}."
        raise NotAnEpisodePageError(msg)

    raw_title = heading.get_text(strip=True) if isinstance(heading, Tag) else ""
    counter = _HEADING_COUNTER.match(raw_title)
    title, number = (counter["title"].strip(), int(counter["number"])) if counter else (raw_title, 0)

    article = soup.select_one("article[id^=post-]")
    post_id = ""
    if isinstance(article, Tag):
        found = _POST_ID.match(str(article.get("id", "")))
        post_id = found["id"] if found else ""

    back = soup.select_one("div.backto-list a[href]")
    work_link = urljoin(url, str(back["href"])) if isinstance(back, Tag) else None

    images = []
    for match in _GALLERY_IMAGE.finditer(text):
        attrs = {attr["name"]: attr["value"] for attr in _ATTR.finditer(match["img"])}
        src = attrs.get("src", "")
        if src:
            images.append((urljoin(url, src), _int(attrs.get("width")), _int(attrs.get("height"))))

    prev_url = next_url = None
    for anchor in soup.select("nav.footer-nav a[href]"):
        icon = anchor.find("i")
        classes = " ".join(icon.get_attribute_list("class")) if isinstance(icon, Tag) else ""
        if "fa-arrow-circle-left" in classes:
            prev_url = urljoin(url, str(anchor["href"]))
        elif "fa-arrow-circle-right" in classes:
            next_url = urljoin(url, str(anchor["href"]))

    updated = soup.select_one("time.updated[datetime]")
    return EpisodePage(
        url=url,
        post_id=post_id,
        title=title,
        number=number,
        work_url=work_link,
        images=tuple(images),
        prev_url=prev_url,
        next_url=next_url,
        updated=str(updated["datetime"]) if isinstance(updated, Tag) else None,
    )


def parse_work_page(html: str | bytes, url: str) -> Work:
    """Read a work page into a `Work`.

    Args:
        html: The page.
        url: The URL it came from.

    Returns:
        The work's title and the post id its episode list is keyed on.

    Raises:
        NotAnEpisodePageError: The page carries no episode list.
    """
    soup = BeautifulSoup(_text(html), "html.parser")
    listing = soup.select_one("ul.alm-listing[data-meta-value]")
    if not isinstance(listing, Tag):
        msg = f"no work page at {url}."
        raise NotAnEpisodePageError(msg)
    post_id = str(listing["data-meta-value"])

    title = ""
    crumb = soup.select_one("div.breadcrumbs strong")
    if isinstance(crumb, Tag):
        title = crumb.get_text(strip=True)
    if not title:
        cover = soup.select_one("div.webcomic-header img[alt]")
        title = str(cover["alt"]).strip() if isinstance(cover, Tag) else ""
    if not title:
        page_title = soup.find("title")
        title = page_title.get_text(strip=True).split(" - ")[0] if isinstance(page_title, Tag) else ""
    return Work(url=url, post_id=post_id, title=title)


def parse_listing(html: str) -> list[tuple[str, str]]:
    """Read one Ajax Load More answer into `(episode URL, title)` pairs, in the order listed.

    Args:
        html: The `html` field of the endpoint's JSON answer.

    Returns:
        One pair per listed episode.
    """
    soup = BeautifulSoup(html, "html.parser")
    episodes = []
    for item in soup.select("div.item-episode"):
        link = next(
            (anchor for anchor in item.select("a[href]") if _EPISODE_PATH.match(urlparse(str(anchor["href"])).path)),
            None,
        )
        if link is None:
            continue
        title = item.select_one("span")
        episodes.append((str(link["href"]), title.get_text(strip=True) if isinstance(title, Tag) else ""))
    return episodes


class LeedCafe(Extractor):
    """Fetch episodes from LEED Cafe.

    An episode URL is `/webcomic/<slug>/`, a series URL `/webcomicinfo/<slug>/`.
    A work's episode list is fetched once per run and reused for every
    episode of the work.
    """

    NAME = "leedcafe"
    HOSTS = (HOST,)
    URL_FORMS = (
        "https://leedcafe.com/webcomic/<slug>/",
        "https://leedcafe.com/webcomicinfo/<slug>/",
    )

    def __init__(self, session: Session | None = None) -> None:
        """Build an extractor.

        Args:
            session: A session to reuse. A retrying one is made when omitted.
        """
        super().__init__(session)
        # Work slug -> the work and its episodes (slug -> title), oldest first.
        self._works: dict[str, tuple[Work, dict[str, str]]] = {}

    @classmethod
    def suitable(cls, url: str) -> bool:
        """Report whether `url` is an episode or a work page.

        Args:
            url: The URL to check.

        Returns:
            True for `https://leedcafe.com/webcomic/<slug>/` and
            `https://leedcafe.com/webcomicinfo/<slug>/`.
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
            True for `https://leedcafe.com/webcomicinfo/<slug>/`.
        """
        return _WORK_PATH.match(urlparse(url).path) is not None

    def series_urls(self, url: str) -> list[str]:
        """List the episodes of a work, oldest first.

        Args:
            url: A work page URL.

        Returns:
            One episode URL per listed episode, oldest first.

        Raises:
            UnsupportedUrlError: The URL is not a work page.
            NotAnEpisodePageError: The work lists no episode.
        """
        slug = work_slug(url)
        if slug is None:
            msg = f"{url} is not a work page."
            raise UnsupportedUrlError(msg)
        _, episodes = self._work(slug)
        if not episodes:
            msg = f"the work at {url} lists no episode."
            raise NotAnEpisodePageError(msg)
        return [episode_url(episode) for episode in episodes]

    def episode(self, url: str) -> Episode:
        """Read one episode: its titles, its pages and the episode after it.

        Args:
            url: An episode URL.

        Returns:
            The episode. `pages` is empty when the page carries no image.

        Raises:
            UnsupportedUrlError: The URL is not an episode URL.
            NotAnEpisodePageError: The site has no such episode (404), or the
                page describes none.
        """
        slug = episode_slug(url)
        if slug is None:
            msg = f"{url} is not an episode page."
            raise UnsupportedUrlError(msg)
        canonical = episode_url(slug)

        res = self._session.get(canonical, headers=self.HEADERS, timeout=self.TIMEOUT)
        if res.status_code == HTTPStatus.NOT_FOUND:
            msg = f"{url} is gone (HTTP 404): its free run has ended."
            raise NotAnEpisodePageError(msg)
        res.raise_for_status()
        # WordPress guesses a post from a slug it does not know and redirects
        # there; the episode is then the one it landed on.
        served = episode_slug(str(res.url or canonical))
        if served is not None:
            slug, canonical = served, episode_url(served)
        page = parse_episode_page(res.content, str(res.url or canonical))

        series_title = ""
        episode_title = page.title
        next_url = page.next_url
        listed_next: str | None = None
        work_link = page.work_url
        if work_link is not None and (work_key := work_slug(work_link)) is not None:
            work, episodes = self._work(work_key)
            series_title = work.title
            episode_title = episodes.get(slug) or page.title
            listed_next = _next_in(episodes, slug)
            work_link = work.url
        if listed_next is not None:
            next_url = listed_next
        if not series_title:
            series_title = page.title

        return Episode(
            url=canonical,
            series_title=series_title,
            episode_title=episode_title,
            pages=tuple(Page(url=src, width=width, height=height) for src, width, height in page.images),
            next_url=next_url,
            metadata={
                "post_id": page.post_id,
                "number": page.number,
                "title": page.title,
                "work_url": work_link,
                "updated": page.updated,
                "prev_url": page.prev_url,
                "nav_next_url": page.next_url,
            },
        )

    def _work(self, slug: str) -> tuple[Work, dict[str, str]]:
        """Read a work page and its episode list, once per work."""
        if slug not in self._works:
            url = work_url(slug)
            res = self._session.get(url, headers=self.HEADERS, timeout=self.TIMEOUT)
            if res.status_code == HTTPStatus.NOT_FOUND:
                msg = f"{url} is gone (HTTP 404): not a work page."
                raise NotAnEpisodePageError(msg)
            res.raise_for_status()
            work = parse_work_page(res.content, str(res.url or url))
            self._works[slug] = (work, self._listing(work))
        return self._works[slug]

    def _listing(self, work: Work) -> dict[str, str]:
        """Fetch a work's whole episode list, oldest first: episode slug -> title."""
        episodes: dict[str, str] = {}
        for page_index in range(_LISTING_MAX_PAGES):
            res = self._get(
                ALM_URL,
                params={
                    "action": "alm_get_posts",
                    "post_type": "webcomic",
                    "meta_key": "a9_webcomic_info",
                    "meta_value": work.post_id,
                    "order": "ASC",
                    "orderby": "date",
                    "posts_per_page": _LISTING_PAGE_SIZE,
                    "page": page_index,
                },
                headers={**self.HEADERS, "Referer": work.url, "X-Requested-With": "XMLHttpRequest"},
            )
            body = res.json()
            if not isinstance(body, dict):
                break
            listed = parse_listing(str(body.get("html") or ""))
            for href, title in listed:
                slug = episode_slug(href)
                if slug is not None:
                    episodes.setdefault(slug, title)
            meta = body.get("meta") if isinstance(body.get("meta"), dict) else {}
            total = _int(meta.get("totalposts"))
            if not listed or len(listed) < _LISTING_PAGE_SIZE or (total and len(episodes) >= total):
                break
        return episodes


def _next_in(episodes: dict[str, str], slug: str) -> str | None:
    """The listed episode after `slug`, or None when it is the last one (or unlisted)."""
    slugs = list(episodes)
    if slug not in slugs:
        return None
    index = slugs.index(slug) + 1
    return episode_url(slugs[index]) if index < len(slugs) else None


def _text(html: str | bytes) -> str:
    """`html` as text; the site serves UTF-8."""
    return html.decode("utf-8", errors="replace") if isinstance(html, bytes) else html


def _int(value: Any) -> int:  # noqa: ANN401 (a JSON or attribute value of any kind)
    """`value` as an int, 0 when it is not one."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
