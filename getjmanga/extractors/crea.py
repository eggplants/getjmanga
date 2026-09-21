"""CREA コミックエッセイルーム (文藝春秋): comic essays served as plain magazine articles."""

from __future__ import annotations

import re
from http import HTTPStatus
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from bs4.element import Tag

from getjmanga.errors import NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractor import Episode, Extractor, Page, published_on

if TYPE_CHECKING:
    from httpx import Response

BASE_URL = "https://crea.bunshun.jp"

#: What an episode belongs to when its page names no series.
ROOM_TITLE = "コミックエッセイルーム"

# Every CREA article, comic or not, is `/articles/-/<id>`; the page itself
# says whether it is a comic essay.
_EPISODE_PATH = re.compile(r"^/articles/-/(?P<id>\d+)/?$")

# A series is a `/list/<slug>` listing of its articles, newest first and 20
# to a page. `/list/comic-essay` itself is the room's front page, laid out
# differently, and the two-level lists (`/list/genre/...`, `/list/matome/...`)
# are magazine sections, so neither is taken.
_SERIES_PATH = re.compile(r"^/list/(?!comic-essay/?$)(?P<slug>[^/]+)/?$")

# An author page lists every series of one artist, each newest first.
_AUTHOR_PATH = re.compile(r"^/list/comic-essay/author/(?P<name>[^/]+)/?$")

# The CDN path of an image, `/mwimgs/<a>/<b>/<size>/img_<hash>.<ext>`, holds
# the rendition size; `-` is the file as uploaded.
_IMAGE_PATH = re.compile(r"^(?P<prefix>/mwimgs/[0-9a-f]/[0-9a-f]/)(?P<size>[^/]+)/(?P<name>img_[^/]+)$")

_PREV_LABEL = "前のお話"
_NEXT_LABEL = "次のお話"
_PREV_LABEL = "前のお話"
_SUMMARY_LABEL = "まとめページ"


class Crea(Extractor):
    """Fetch comic essays from CREA (crea.bunshun.jp).

    The comics of the コミックエッセイルーム are ordinary articles of the
    magazine site: every page is a `<figure>` of the article body, served
    as a plain JPEG or PNG to anyone, with no account, no scrambling and no
    paywall. An episode the site has taken down is a 404. Non-comic articles
    on the same URL shape are rejected, so the `next` chain and a series
    listing never mistake a text essay for a comic.
    """

    NAME = "crea"
    HOSTS = ("crea.bunshun.jp",)
    PUBLISHER = "文藝春秋"
    URL_FORMS = (
        "https://crea.bunshun.jp/articles/-/<id>",
        "https://crea.bunshun.jp/list/<series>",
        "https://crea.bunshun.jp/list/comic-essay/author/<name>",
    )

    @classmethod
    def suitable(cls, url: str) -> bool:
        """Report whether the extractor takes `url`.

        Args:
            url: The URL to check.

        Returns:
            True for an article, a series listing or an author page on crea.bunshun.jp.
        """
        if not super().suitable(url):
            return False
        path = urlparse(url).path
        return any(pattern.match(path) is not None for pattern in (_EPISODE_PATH, _SERIES_PATH, _AUTHOR_PATH))

    @classmethod
    def is_series(cls, url: str) -> bool:
        """Report whether `url` names a series listing or an author page rather than one article.

        Args:
            url: The URL to check.

        Returns:
            True for `/list/<slug>` and `/list/comic-essay/author/<name>`.
        """
        path = urlparse(url).path
        return _SERIES_PATH.match(path) is not None or _AUTHOR_PATH.match(path) is not None

    def series_urls(self, url: str) -> list[str]:
        """List every article of a series, oldest first.

        A series listing shows 20 articles a page, newest first, with a
        `次の20件を表示` link to the next page; an author page shows every
        series of the artist, each newest first, on one page.

        Args:
            url: A series listing or an author page.

        Returns:
            One article URL per listed article, in reading order.

        Raises:
            UnsupportedUrlError: The URL is neither a series listing nor an author page.
            NotAnEpisodePageError: The page is a 404 or lists no article.
        """
        path = urlparse(url).path
        if _AUTHOR_PATH.match(path) is not None:
            urls = self._author_urls(url)
        elif _SERIES_PATH.match(path) is not None:
            urls = self._listing_urls(url)
        else:
            msg = f"{url} is not a series page."
            raise UnsupportedUrlError(msg)
        if not urls:
            msg = f"the series at {url} lists no episode."
            raise NotAnEpisodePageError(msg)
        return urls

    def episode(self, url: str) -> Episode:
        """Read one comic essay: its titles, its page images and what follows it.

        Args:
            url: The article URL.

        Returns:
            The episode. `pages` is empty when the article is a comic-essay
            page without page images, such as a series' まとめ index.

        Raises:
            NotAnEpisodePageError: The page is a 404 or is not a comic essay.
        """
        match = _EPISODE_PATH.match(urlparse(url).path)
        article_id = match["id"] if match else ""

        res = self._fetch_page(url, "article")
        soup = BeautifulSoup(res.content, "html.parser")
        page_url = str(res.url or url)

        body = soup.find("article", class_="article-body")
        if not isinstance(body, Tag):
            msg = f"no article on {url}."
            raise NotAnEpisodePageError(msg)
        # Only the comic essays carry the room's logo above the title.
        if not _is_comic(soup):
            msg = f"{url} is not a comic essay; is it a コミックエッセイルーム article?"
            raise NotAnEpisodePageError(msg)

        pages = [
            _page(img, page_url)
            for figure in body.find_all("figure", class_="image-area", recursive=False)
            if isinstance(figure, Tag)
            for img in figure.find_all("img")
            if isinstance(img, Tag)
        ]
        pages = [page for page in pages if page is not None]

        episode_title = _text(soup, "article-head__title") or article_id
        series_title = _series_title(soup) or _text(soup, "comic-essay-bottom", "title") or ROOM_TITLE

        return Episode(
            url=url,
            series_title=series_title,
            episode_title=episode_title,
            pages=tuple(pages),
            prev_url=_button_link(body, _PREV_LABEL, page_url),
            next_url=_button_link(body, _NEXT_LABEL, page_url),
            metadata={
                "article_id": article_id,
                "date": _text(soup, "article-head__date"),
                "author": _text(soup, "article-head__author"),
                "author_url": _author_url(soup, page_url),
                "prev_url": _button_link(body, _PREV_LABEL, page_url),
                "summary_url": _button_link(body, _SUMMARY_LABEL, page_url),
                "images": [page.url for page in pages],
            },
            writer=_text(soup, "article-head__author"),
            publisher=self.PUBLISHER,
            published=published_on(_text(soup, "article-head__date")),
        )

    def _listing_urls(self, url: str) -> list[str]:
        """The article URLs of a series listing across its pages, oldest first."""
        newest_first: list[str] = []
        seen_pages: set[str] = set()
        next_page: str | None = url
        while next_page and next_page not in seen_pages:
            seen_pages.add(next_page)
            res = self._fetch_page(next_page, "series")
            soup = BeautifulSoup(res.content, "html.parser")
            page_url = str(res.url or next_page)
            for listing in soup.find_all("div", class_="lists-default"):
                if isinstance(listing, Tag):
                    _collect_article_links(listing, page_url, newest_first)
            next_page = _next_listing_page(soup, page_url)
        return list(reversed(newest_first))

    def _author_urls(self, url: str) -> list[str]:
        """The article URLs of every series on an author page, each oldest first."""
        res = self._fetch_page(url, "author")
        soup = BeautifulSoup(res.content, "html.parser")
        page_url = str(res.url or url)
        urls: list[str] = []
        for work in soup.find_all("div", class_="list-authors-work__list"):
            if not isinstance(work, Tag):
                continue
            newest_first: list[str] = []
            for items in work.find_all("div", class_="list-authors-work__items"):
                if isinstance(items, Tag):
                    _collect_article_links(items, page_url, newest_first)
            urls.extend(link for link in reversed(newest_first) if link not in urls)
        return urls

    def _fetch_page(self, url: str, kind: str) -> Response:
        """GET a site page, turning a 404 into `NotAnEpisodePageError`."""
        res = self._session.get(url, headers=self.HEADERS, timeout=self.TIMEOUT)
        if res.status_code == HTTPStatus.NOT_FOUND:
            msg = f"no {kind} at {url}."
            raise NotAnEpisodePageError(msg)
        res.raise_for_status()
        return res


def original_image_url(url: str) -> str:
    """The as-uploaded rendition of a CDN image URL.

    The article embeds a `1280wm` (1280 px wide) rendition; the `-` size is
    the file as uploaded, which is never smaller and is compressed once less.

    Args:
        url: An image URL, `https://crea.ismcdn.jp/mwimgs/<a>/<b>/<size>/img_<hash>.jpg`.

    Returns:
        The same URL with the size replaced by `-`, or `url` unchanged when
        it is not a CDN image.
    """
    parsed = urlparse(url)
    match = _IMAGE_PATH.match(parsed.path)
    if match is None:
        return url
    return parsed._replace(path=f"{match['prefix']}-/{match['name']}", query="").geturl()


def _is_comic(soup: BeautifulSoup) -> bool:
    """Whether the article head carries the コミックエッセイルーム logo."""
    return any(
        isinstance(subtitle, Tag) and "comic" in (subtitle.get("class") or [])
        for head in soup.find_all("div", class_="article-head")
        if isinstance(head, Tag)
        for subtitle in head.find_all("p", class_="subtitle")
    )


def _page(img: Tag, page_url: str) -> Page | None:
    """One page image, or None when the `<img>` is a lazy-loading placeholder with no source."""
    src = str(img.get("data-src") or img.get("src") or "")
    if not src or src.endswith("blank.gif"):
        return None
    return Page(
        url=original_image_url(urljoin(page_url, src)),
        width=_int(img.get("data-width") or img.get("width")),
        height=_int(img.get("data-height") or img.get("height")),
    )


def _int(value: object) -> int:
    """`value` as an int, 0 when it is not one."""
    try:
        return int(str(value))
    except ValueError:
        return 0


def _text(root: Tag, *class_names: str) -> str:
    """The stripped text of the element reached by finding each of `class_names` under the last, or ""."""
    element = root
    for class_name in class_names:
        found = element.find(class_=class_name)
        if not isinstance(found, Tag):
            return ""
        element = found
    return element.get_text(" ", strip=True)


def _series_title(soup: BeautifulSoup) -> str:
    """The series named after the ` | ` of the page title, `<episode> | <series>`."""
    title = soup.title.get_text(" ", strip=True) if soup.title is not None else ""
    _, separator, series = title.rpartition(" | ")
    return series.strip() if separator else ""


def _button_link(body: Tag, label: str, page_url: str) -> str | None:
    """The article a `link-button` labelled `label` points to, if the body has one."""
    for button in body.find_all("div", class_="link-button"):
        if not isinstance(button, Tag):
            continue
        anchor = button.find("a", href=True)
        if isinstance(anchor, Tag) and label in anchor.get_text(" ", strip=True):
            return urljoin(page_url, str(anchor["href"]))
    return None


def _author_url(soup: BeautifulSoup, page_url: str) -> str:
    """The `作家ページへ` link of the series box under the comic, or ""."""
    bottom = soup.find("div", class_="comic-essay-bottom")
    if not isinstance(bottom, Tag):
        return ""
    for anchor in bottom.find_all("a", href=True):
        if isinstance(anchor, Tag) and _AUTHOR_PATH.match(urlparse(str(anchor["href"])).path):
            return urljoin(page_url, str(anchor["href"]))
    return ""


def _collect_article_links(root: Tag, page_url: str, into: list[str]) -> None:
    """Append the article URLs linked under `root`, in document order, skipping repeats."""
    for anchor in root.find_all("a", href=True):
        if not isinstance(anchor, Tag):
            continue
        absolute = urljoin(page_url, str(anchor["href"]).split("?", 1)[0].split("#", 1)[0])
        if _EPISODE_PATH.match(urlparse(absolute).path) and absolute not in into:
            into.append(absolute)


def _next_listing_page(soup: BeautifulSoup, page_url: str) -> str | None:
    """The `次の20件を表示` link of a series listing, if it has one."""
    holder = soup.find("div", class_="list-next")
    anchor = holder.find("a", href=True) if isinstance(holder, Tag) else None
    return urljoin(page_url, str(anchor["href"])) if isinstance(anchor, Tag) else None
