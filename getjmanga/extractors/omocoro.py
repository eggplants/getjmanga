"""オモコロ (バーグハンバーグバーグ): a WordPress humour magazine whose comics are ordinary articles.

There is no viewer. A comic is an article whose body is a run of full-size
images in reading order: a 特集 article at `/kiji/<id>/` tagged 漫画 (or
マンガ, まんが, 4コマ, えほん) with one `<p><img></p>` per page, or a 4コマ
post at `/comic/<id>/` whose page sits in `div.comic-image`. The same
`/kiji/<id>/` shape also serves the magazine's text articles, some of them
tagged 漫画 too (a talk about manga, a report with a few panels), so
`episode()` reads the body and keeps only an article that is a comic: the
prose set between its page images has to stay short next to them. A text
article is not an episode.

A series is a tag page, `/tag/<slug>/` -- every serialised strip has one
(サボり先輩, デーリィズ, 怪奇組, ...) and the site's own "この連載をはじめから
読む" link is that page with `?sort=old`, which lists the articles oldest
first, 20 a page, `/page/<n>/` after the first and a 404 past the last. The
4コマ category archive at `/comic/` pages the same way. Nothing is paywalled,
scrambled or Referer-checked, and there is no next-episode link on an
article, so `next_url` is always None.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from http import HTTPStatus
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from bs4.element import NavigableString, Tag

from getjmanga.errors import NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractor import Episode, Extractor, Page

if TYPE_CHECKING:
    from collections.abc import Iterator

BASE_URL = "https://omocoro.jp"

# An article: a 特集 (`kiji`) or a 4コマ (`comic`) post.
_EPISODE_PATH = re.compile(r"^/(?P<kind>kiji|comic)/(?P<id>\d+)/?$")
# A series: a tag archive, or the 4コマ category archive, either with an optional page number.
_SERIES_PATH = re.compile(r"^/(?:tag/(?P<slug>[^/]+)|(?P<comic>comic))(?:/page/(?P<page>\d+))?/?$")

#: A tag with one of these in its name marks a comic; such a tag names no series of its own.
COMIC_TAG_WORDS = ("漫画", "マンガ", "まんが", "4コマ", "えほん", "絵本")

#: How many characters of prose may sit between the page images, per page,
#: before the article counts as a text article with pictures in it. A comic
#: keeps its afterword outside that stretch; a report puts a paragraph after
#: every photo.
PROSE_PER_PAGE = 30

#: How many articles a listing page holds; a shorter page is the last one.
LISTING_PAGE_SIZE = 20

#: The series title when an article carries neither a series tag nor a writer.
SITE_NAME = "オモコロ"


@dataclass(frozen=True)
class Article:
    """What one article page says."""

    #: The page URL, redirects followed.
    url: str
    #: The category slug: `kiji` for a 特集 article, `comic` for a 4コマ post.
    category: str
    #: The category as shown, `特集` or `4コマ`.
    category_label: str
    #: The article title.
    title: str
    #: The tag names, `#` stripped, in the page's order.
    tags: tuple[str, ...]
    #: The credited writers.
    writers: tuple[str, ...]
    #: The `YYYY-MM-DD` date.
    date: str
    #: The one-line lead.
    description: str
    #: The page images in reading order: the images standing on their own in the body.
    images: tuple[str, ...]
    #: Characters of prose between the first and the last page image.
    prose_between: int

    @property
    def is_comic(self) -> bool:
        """Whether the article is a comic rather than a text article."""
        if not self.images:
            return False
        if self.category == "comic":
            return True
        tagged = any(_is_comic_tag(tag) for tag in self.tags)
        return tagged and self.prose_between <= PROSE_PER_PAGE * len(self.images)

    @property
    def series_title(self) -> str:
        """The first tag that names a series, else the writer, else the site."""
        for tag in self.tags:
            if not _is_comic_tag(tag):
                return tag
        return self.writers[0] if self.writers else SITE_NAME


def parse_article(html: str | bytes, url: str) -> Article:
    """Read an article page.

    Args:
        html: The page.
        url: The URL it came from, to resolve relative links against.

    Returns:
        What the page says.

    Raises:
        NotAnEpisodePageError: The page is not an article.
    """
    soup = BeautifulSoup(html, "html.parser")
    header = soup.select_one("div.article div.article-header")
    body = soup.select_one("div.article div.article-body")
    if not isinstance(header, Tag) or not isinstance(body, Tag):
        msg = f"no article at {url}."
        raise NotAnEpisodePageError(msg)

    category = header.select_one("div.category > span")
    category_classes = category.get("class") if isinstance(category, Tag) else None
    images, prose_between = _body_images(body, url)
    return Article(
        url=url,
        category=str(category_classes[0]) if isinstance(category_classes, list) and category_classes else "",
        category_label=_text(category),
        title=_text(header.select_one("div.title")),
        tags=tuple(_text(anchor).lstrip("#").strip() for anchor in header.select("div.tags a")),
        writers=tuple(_text(anchor) for anchor in header.select("div.staffs a")),
        date=_text(header.select_one("div.date")),
        description=_text(header.select_one("div.description")),
        images=images,
        prose_between=prose_between,
    )


def _body_images(body: Tag, url: str) -> tuple[tuple[str, ...], int]:
    """The page images of an article body, and the prose set between them.

    A 4コマ post keeps its page in `div.comic-image`. A 特集 article is a
    sequence of blocks: a block holding images and no text is a page block
    (its images are pages, in order), a block with text is prose. Dialogue
    lines with an avatar icon, embedded article cards and captioned photos
    are prose, so a report does not turn into pages.
    """
    for tag in body(["script", "style", "noscript"]):
        tag.decompose()
    comic_image = body.select_one("div.comic-image")
    if isinstance(comic_image, Tag):
        return tuple(_sources(comic_image.find_all("img"), url)), 0

    blocks: list[tuple[list[str], int]] = []  # (page image URLs, prose characters)
    for child in body.children:
        if isinstance(child, NavigableString):
            blocks.append(([], len(str(child).strip())))
        elif isinstance(child, Tag):
            images = [child] if child.name == "img" else child.find_all("img")
            text = child.get_text(strip=True)
            blocks.append(([], len(text)) if text else (list(_sources(images, url)), 0))

    page_blocks = [index for index, (images, _) in enumerate(blocks) if images]
    if not page_blocks:
        return (), 0
    inside = blocks[page_blocks[0] : page_blocks[-1] + 1]
    return tuple(src for images, _ in inside for src in images), sum(prose for _, prose in inside)


def _sources(images: list[Tag], url: str) -> Iterator[str]:
    for image in images:
        classes = image.get("class")
        if isinstance(classes, list) and "emoji" in classes:
            continue
        src = image.get("src")
        if isinstance(src, str) and src:
            yield urljoin(url, src)


def _is_comic_tag(tag: str) -> bool:
    return any(word in tag for word in COMIC_TAG_WORDS)


def _text(tag: Tag | None) -> str:
    return tag.get_text(strip=True) if isinstance(tag, Tag) else ""


def listing_urls(html: str | bytes, url: str) -> list[str]:
    """The article links of one listing page, in the page's order, deduplicated.

    Only the articles `episode()` reads are kept: the ブロス, まとめ, お知らせ
    and 連載 posts a tag also lists are not comics.

    Args:
        html: The listing page.
        url: The URL it came from, to resolve relative links against.

    Returns:
        The `/kiji/<id>/` and `/comic/<id>/` links of the listing.
    """
    soup = BeautifulSoup(html, "html.parser")
    urls: list[str] = []
    for anchor in soup.select(
        "div.tag-entries div.box div.title a[href], div.category-entries div.box div.title a[href]"
    ):
        absolute = urljoin(url, str(anchor["href"]).split("#", 1)[0])
        if _EPISODE_PATH.match(urlparse(absolute).path) and absolute not in urls:
            urls.append(absolute)
    return urls


class Omocoro(Extractor):
    """Fetch comics from オモコロ.

    An article is read off its HTML; a series is a tag page (or the 4コマ
    archive) walked oldest first through `?sort=old`. Every comic is free, so
    there is nothing to sign in to and no locked episode.
    """

    NAME = "omocoro"
    HOSTS = ("omocoro.jp",)
    URL_FORMS = (
        "https://omocoro.jp/kiji/<id>/",
        "https://omocoro.jp/comic/<id>/",
        "https://omocoro.jp/tag/<tag>/",
        "https://omocoro.jp/comic/",
    )

    @classmethod
    def suitable(cls, url: str) -> bool:
        """Report whether the extractor takes `url`.

        Args:
            url: The URL to check.

        Returns:
            True for an https article or listing URL on omocoro.jp.
        """
        if not super().suitable(url):
            return False
        path = urlparse(url).path
        return _EPISODE_PATH.match(path) is not None or _SERIES_PATH.match(path) is not None

    @classmethod
    def is_series(cls, url: str) -> bool:
        """Report whether `url` is a tag page or the 4コマ archive rather than an article.

        Args:
            url: The URL to check.

        Returns:
            True for `/tag/<slug>/` or `/comic/`, with or without a page number.
        """
        return _SERIES_PATH.match(urlparse(url).path) is not None

    def series_urls(self, url: str) -> list[str]:
        """List every article a tag page (or the 4コマ archive) covers, oldest first.

        The listing is walked page by page with `?sort=old`, from the first
        page whatever page `url` named, until the site answers 404 or a page
        comes up short. Text articles the tag also lists are kept out only by
        their URL shape; `episode()` tells the rest apart.

        Args:
            url: A listing URL.

        Returns:
            One article URL per listed article, oldest first.

        Raises:
            UnsupportedUrlError: The URL is not a listing.
            NotAnEpisodePageError: The listing is a 404 or lists no article.
        """
        match = _SERIES_PATH.match(urlparse(url).path)
        if match is None:
            msg = f"{url} is not a tag page."
            raise UnsupportedUrlError(msg)
        base = (
            f"{self._origin(url)}/comic/" if match.group("comic") else f"{self._origin(url)}/tag/{match.group('slug')}/"
        )

        urls: list[str] = []
        number = 1
        while True:
            page_url = base if number == 1 else f"{base}page/{number}/"
            res = self._session.get(page_url, headers=self.HEADERS, params={"sort": "old"}, timeout=self.TIMEOUT)
            if res.status_code == HTTPStatus.NOT_FOUND:
                break
            res.raise_for_status()
            listed = listing_urls(res.content, str(res.url or page_url))
            urls.extend(link for link in listed if link not in urls)
            if len(listed) < LISTING_PAGE_SIZE:
                break
            number += 1
        if not urls:
            msg = f"the listing at {url} names no article."
            raise NotAnEpisodePageError(msg)
        return urls

    def episode(self, url: str) -> Episode:
        """Read one article and list its pages.

        Args:
            url: The article URL.

        Returns:
            The comic. `pages` is never empty: nothing on the site is locked.

        Raises:
            NotAnEpisodePageError: The URL is not an article, is a 404, or is a
                text article rather than a comic.
        """
        if _EPISODE_PATH.match(urlparse(url).path) is None:
            msg = f"{url} is not an article."
            raise NotAnEpisodePageError(msg)
        res = self._session.get(url, headers=self.HEADERS, timeout=self.TIMEOUT)
        if res.status_code == HTTPStatus.NOT_FOUND:
            msg = f"{url} is gone (HTTP 404)."
            raise NotAnEpisodePageError(msg)
        res.raise_for_status()
        article = parse_article(res.content, str(res.url or url))
        if not article.is_comic:
            msg = (
                f"{url} is a text article, not a comic "
                f"({len(article.images)} pages, {article.prose_between} characters between them)."
            )
            raise NotAnEpisodePageError(msg)
        return Episode(
            url=article.url,
            series_title=article.series_title,
            episode_title=article.title,
            pages=tuple(Page(url=src) for src in article.images),
            next_url=None,
            metadata={
                "title": article.title,
                "date": article.date,
                "category": article.category,
                "category_label": article.category_label,
                "tags": list(article.tags),
                "writers": list(article.writers),
                "description": article.description,
                "images": list(article.images),
                "prose_between": article.prose_between,
            },
        )
