"""パチクリ (Pachikuri!, 主婦と生活社): a WordPress site that serves its episodes as plain posts.

Every work is a category at `https://pachikuri.jp/<work>/` and every episode
a post under it at `https://pachikuri.jp/<work>/<slug>/`; the site's own
"next"/"previous"/"latest" buttons use the short link
`https://pachikuri.jp/?p=<id>` instead, which 301s to the slug URL. An
episode page carries `main#js-manga` with one `<img>` per page in reading
order (a thumbnail inside `span.hidden_image` is not a page) and a
`section.mangaFuncs` pager whose `次の話へ` is an `<a>` with the short link,
or a `<span>` with `--disabled` at the latest episode. The images are ordinary
uploads under `/wp-content/uploads/`: no scrambling, no Referer or cookie
check. A `-scaled.jpg` in the post is WordPress's 2560px copy; the unscaled
original sits next to it and is preferred when the site still serves it.

A work page lists eight episodes per page, newest first, `link[rel=next]`
pointing at `/<work>/page/<n>/` for the rest. The site is free without an
account ("登録不要完全無料"), so there is no `login()`; a post without a
single page image is reported as locked rather than as not an episode.
The REST API is disabled, which is why the listing is walked page by page.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from http import HTTPStatus
from io import BytesIO
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup
from bs4.element import Tag
from PIL import Image

from getjmanga.errors import NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractor import Episode, Extractor, Page

HOST = "pachikuri.jp"

# Single-segment paths that are site pages, not works.
_NOT_A_WORK = frozenset({"news", "list_manga", "sitepolicy", "personaldata", "page", "feed", "wp-content", "wp-admin"})
# A work page: one path segment (the WordPress category of the work).
_WORK_PATH = re.compile(r"^/(?P<work>[\w.-]+)/?$")
# An episode page: `/<work>/<slug>/`, WordPress-style.
_EPISODE_PATH = re.compile(r"^/(?P<work>[\w.-]+)/(?P<slug>[^/]+)/?$")
# The number the header puts in front of the date: `第710話 │ 2026.9.16 (Wed)`.
_EPISODE_NUMBER = re.compile(r"第\s*\S+?話")
# WordPress's 2560px copy of a large upload: `<name>-scaled.<ext>`.
_SCALED = re.compile(r"-scaled(?P<ext>\.\w+)$")


@dataclass(frozen=True)
class Post:
    """What one episode page says."""

    #: The episode URL, redirects followed (the slug URL, not the short link).
    url: str
    #: `ul.post-categories`: the work's title.
    series_title: str
    #: `h1.mangaHead__title`: the episode's own title.
    title: str
    #: `第710話`, read off `div.mangaHead__updated`. Empty when the header has none.
    number: str
    #: The date part of `div.mangaHead__updated`.
    date: str
    #: `div.headline__txt__author--mangaHead`: the author line.
    author: str
    #: The page images in reading order.
    images: tuple[str, ...]
    #: The `次の話へ` link, when there is a next episode.
    next_url: str | None
    #: The `前の話へ` link, when there is a previous episode.
    prev_url: str | None
    #: The link back to the work page.
    work_url: str | None


@dataclass(frozen=True)
class Listing:
    """What one page of a work's listing says."""

    #: `h1.sakuhinDtails__name`, the author span taken out.
    title: str
    #: The episode links of this page, newest first.
    episode_urls: tuple[str, ...]
    #: `link[rel=next]`: the next page of the listing, when there is one.
    next_page: str | None


def parse_post(html: str | bytes, url: str) -> Post:
    """Read an episode page into a `Post`.

    Args:
        html: The page.
        url: The URL it came from, to resolve links against.

    Returns:
        What the page says.

    Raises:
        NotAnEpisodePageError: The page is not an episode page.
    """
    soup = BeautifulSoup(html, "html.parser")
    main = soup.select_one("main#js-manga")
    head = soup.select_one("section.mangaHead")
    if not isinstance(main, Tag) or not isinstance(head, Tag):
        msg = f"no episode on {url}."
        raise NotAnEpisodePageError(msg)

    updated = " ".join(_text(head.select_one("div.mangaHead__updated")).split())
    number_match = _EPISODE_NUMBER.search(updated)
    number = number_match.group(0) if number_match else ""
    date = updated.replace(number, "", 1).strip(" 　│|")

    images = tuple(
        urljoin(url, str(img["src"]))
        for img in main.select("img[src]")
        if isinstance(img, Tag) and img.find_parent("span", class_="hidden_image") is None
    )
    return Post(
        url=url,
        series_title=_text(head.select_one("ul.post-categories a")),
        title=_text(head.select_one("h1.mangaHead__title")),
        number=number,
        date=date,
        author=_text(head.select_one("div.headline__txt__author--mangaHead")),
        images=images,
        next_url=_link(soup.select_one("a.mangaFuncs__btn--next[href]"), url),
        prev_url=_link(soup.select_one("a.mangaFuncs__btn--prev[href]"), url),
        work_url=_link(head.select_one("ul.post-categories a[href]"), url),
    )


def parse_listing(html: str | bytes, url: str) -> Listing:
    """Read one page of a work's listing into a `Listing`.

    Args:
        html: The page.
        url: The URL it came from, to resolve links against.

    Returns:
        The work's title, this page's episode links (newest first) and the next page.

    Raises:
        NotAnEpisodePageError: The page is not a work page.
    """
    soup = BeautifulSoup(html, "html.parser")
    heading = soup.select_one("h1.sakuhinDtails__name")
    listing = soup.select_one("section.mangaList")
    if not isinstance(heading, Tag) and not isinstance(listing, Tag):
        msg = f"no work page at {url}."
        raise NotAnEpisodePageError(msg)

    title = ""
    if isinstance(heading, Tag):
        for author in heading.select("span.sakuhinDtails__author"):
            author.decompose()
        title = heading.get_text(strip=True)
    urls: list[str] = []
    if isinstance(listing, Tag):
        for anchor in listing.select("a.mangaList__link[href]"):
            absolute = urljoin(url, str(anchor["href"]).split("#", 1)[0])
            if _EPISODE_PATH.match(urlparse(absolute).path) and absolute not in urls:
                urls.append(absolute)
    return Listing(
        title=title,
        episode_urls=tuple(urls),
        next_page=_link(soup.select_one('link[rel="next"][href]'), url),
    )


def original_url(url: str) -> str | None:
    """The unscaled upload a `-scaled` image URL was made from, or None for any other URL."""
    parsed = urlparse(url)
    if _SCALED.search(parsed.path) is None:
        return None
    return parsed._replace(path=_SCALED.sub(r"\g<ext>", parsed.path)).geturl()


def short_link_id(url: str) -> str | None:
    """The `<id>` of a `https://pachikuri.jp/?p=<id>` short link, or None for any other URL."""
    parsed = urlparse(url)
    if parsed.path not in ("", "/"):
        return None
    ids = parse_qs(parsed.query).get("p", [])
    return ids[0] if len(ids) == 1 and ids[0].isdigit() else None


def _text(tag: Tag | None) -> str:
    return tag.get_text(" ", strip=True) if isinstance(tag, Tag) else ""


def _link(anchor: Tag | None, url: str) -> str | None:
    if not isinstance(anchor, Tag):
        return None
    href = str(anchor["href"]).split("#", 1)[0]
    return urljoin(url, href) if href else None


def _work_slug(url: str) -> str | None:
    match = _WORK_PATH.match(urlparse(url).path)
    if match is None or match["work"] in _NOT_A_WORK:
        return None
    return match["work"]


def _is_episode_path(url: str) -> bool:
    match = _EPISODE_PATH.match(urlparse(url).path)
    return match is not None and match["work"] not in _NOT_A_WORK and match["slug"] != "page"


class Pachikuri(Extractor):
    """Fetch episodes from パチクリ (Pachikuri!).

    An episode URL is a post under its work, `https://pachikuri.jp/<work>/<slug>/`,
    or the short link `https://pachikuri.jp/?p=<id>` the site's own buttons
    use; a series URL is the work page `https://pachikuri.jp/<work>/`.
    """

    NAME = "pachikuri"
    HOSTS = (HOST,)
    URL_FORMS = (
        "https://pachikuri.jp/<work>/<slug>/",
        "https://pachikuri.jp/?p=<id>",
        "https://pachikuri.jp/<work>/",
    )

    @classmethod
    def suitable(cls, url: str) -> bool:
        """Report whether the extractor takes `url`.

        Args:
            url: The URL to check.

        Returns:
            True for an https episode post, short link or work page on the site.
        """
        if not super().suitable(url):
            return False
        return short_link_id(url) is not None or _is_episode_path(url) or _work_slug(url) is not None

    @classmethod
    def is_series(cls, url: str) -> bool:
        """Report whether `url` is a work page.

        Args:
            url: The URL to check.

        Returns:
            True for `https://pachikuri.jp/<work>/`.
        """
        return _work_slug(url) is not None

    def series_urls(self, url: str) -> list[str]:
        """List every episode a work page lists, oldest first.

        The listing is walked page by page through `link[rel=next]`.

        Args:
            url: A work page URL.

        Returns:
            One episode URL per listed episode, oldest first, deduplicated.

        Raises:
            UnsupportedUrlError: The URL is not a work page.
            NotAnEpisodePageError: The work lists no episode.
        """
        if _work_slug(url) is None:
            msg = f"{url} is not a work page."
            raise UnsupportedUrlError(msg)
        page_url: str | None = url
        seen: set[str] = set()
        newest_first: list[str] = []
        while page_url is not None and page_url not in seen:
            seen.add(page_url)
            res = self._session.get(page_url, headers=self.HEADERS, timeout=self.TIMEOUT)
            if res.status_code == HTTPStatus.NOT_FOUND:
                msg = f"{page_url} is gone (HTTP 404): not a work page."
                raise NotAnEpisodePageError(msg)
            res.raise_for_status()
            listing = parse_listing(res.content, str(res.url or page_url))
            newest_first.extend(u for u in listing.episode_urls if u not in newest_first)
            page_url = listing.next_page
        if not newest_first:
            msg = f"the work at {url} lists no episode."
            raise NotAnEpisodePageError(msg)
        return newest_first[::-1]

    def episode(self, url: str) -> Episode:
        """Read one episode: its titles, its pages and the episode after it.

        Args:
            url: An episode post URL or a `?p=<id>` short link.

        Returns:
            The episode. `pages` is empty when the post carries no page image.

        Raises:
            UnsupportedUrlError: The URL is neither an episode post nor a short link.
            NotAnEpisodePageError: There is no such post (404), or the page is
                not an episode page (a work page, a news post).
        """
        if short_link_id(url) is None and not _is_episode_path(url):
            msg = f"{url} is not an episode page."
            raise UnsupportedUrlError(msg)
        res = self._session.get(url, headers=self.HEADERS, timeout=self.TIMEOUT)
        if res.status_code == HTTPStatus.NOT_FOUND:
            msg = f"{url} is gone (HTTP 404): no such episode."
            raise NotAnEpisodePageError(msg)
        res.raise_for_status()
        post = parse_post(res.content, str(res.url or url))

        episode_title = f"{post.number} {post.title}".strip() if post.number else post.title
        metadata: dict[str, Any] = {
            "title": post.title,
            "number": post.number,
            "date": post.date,
            "author": post.author,
            "work_url": post.work_url,
            "prev_url": post.prev_url,
        }
        return Episode(
            url=post.url,
            series_title=post.series_title,
            episode_title=episode_title,
            pages=tuple(
                Page(url=src, extra={"original": original} if (original := original_url(src)) else {})
                for src in post.images
            ),
            prev_url=post.prev_url,
            next_url=post.next_url,
            metadata=metadata,
        )

    def image(self, page: Page, episode: Episode) -> Image.Image:
        """Fetch one page, the unscaled original first when the post shows a `-scaled` copy.

        Args:
            page: The page to fetch.
            episode: The episode the page belongs to.

        Returns:
            The page image.
        """
        headers = {**self.HEADERS, "Referer": episode.url}
        original = page.extra.get("original")
        if isinstance(original, str) and original:
            res = self._session.get(original, headers=headers, timeout=self.IMAGE_TIMEOUT)
            if res.is_success and res.headers.get("content-type", "").startswith("image/"):
                return Image.open(BytesIO(res.content))
        return self._fetch_image(page.url, headers=headers)
