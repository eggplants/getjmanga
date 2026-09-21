"""Splush (イースト・プレス): a WordPress site that serves its episodes as plain pages.

Every work and every episode is a `series` post, so `/series/<id>/` is both
URL shapes at once: a work page carries a `section.lineup` linking its
readable episodes newest first, an episode page carries one
`div.comicImg > img` per page in reading order (or a `p.notice` once the
free run has ended) and a pager naming the next episode. Nothing tells the
two apart but the page itself, so `is_series()` fetches it and keeps what it
read for `episode()` and `series_urls()`. The images are ordinary uploads:
no scrambling, no Referer or cookie check. The members' area signs in on an
external mailing service and gates nothing under `/series/`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from http import HTTPStatus
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from bs4.element import Tag

from getjmanga.errors import NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractor import Episode, Extractor, Page

if TYPE_CHECKING:
    from collections.abc import Iterator

    from httpx import Client

# A work page or an episode page: WordPress hands both out at `/series/<id>/`.
_SERIES_PATH = re.compile(r"^/series/(?P<id>\d+)/?$")

# What the site puts on a page instead of the pages once an episode's free run has ended.
_EXPIRED_NOTICE = "公開終了"


@dataclass(frozen=True)
class Document:
    """What one `/series/<id>/` page says, whichever of the two kinds it is."""

    #: The URL the page was served at, redirects followed.
    url: str
    #: True for a work page, which lists episodes; False for an episode page.
    is_work: bool
    #: `h3.title`: the work's title, on both kinds of page.
    series_title: str
    #: `h4.seriesNum` of the episode page's navigation. Empty on a work page.
    episode_title: str
    #: The page images in reading order. Empty on a work page or an expired episode.
    images: tuple[str, ...]
    #: The `次の話へ` link, when the episode page carries one.
    next_url: str | None
    #: The `前の話へ` link, when the episode page carries one.
    prev_url: str | None
    #: The `作品トップへ` link back to the work page, when the episode page carries one.
    work_url: str | None
    #: The `p.notice` text, `公開終了しました。` on an expired episode.
    notice: str
    #: The episodes a work page lists, oldest first, deduplicated.
    episode_urls: tuple[str, ...]
    #: The page's `<title>`.
    title: str
    #: `p.author` under the work's title.
    writer: str = ""


def parse_document(html: str | bytes, url: str) -> Document:
    """Read a `/series/<id>/` page into a `Document`.

    Args:
        html: The page.
        url: The URL it came from, to resolve relative links against.

    Returns:
        What the page says.

    Raises:
        NotAnEpisodePageError: The page is neither a work page nor an episode page.
    """
    soup = BeautifulSoup(html, "html.parser")
    lineup = soup.find("section", class_="lineup")
    navi = soup.find("div", class_="navi")
    comic_area = soup.find("section", class_="comicArea")
    if not isinstance(lineup, Tag) and not isinstance(navi, Tag) and not isinstance(comic_area, Tag):
        msg = f"neither a work page nor an episode page at {url}."
        raise NotAnEpisodePageError(msg)

    heading = navi.find("h4", class_="seriesNum") if isinstance(navi, Tag) else None
    notice = soup.find("p", class_="notice")
    return Document(
        url=url,
        is_work=isinstance(lineup, Tag),
        series_title=_text(soup.find("h3", class_="title")),
        episode_title=_text(heading),
        images=tuple(
            urljoin(url, str(img["src"])) for img in soup.select("div.comicImg img[src]") if isinstance(img, Tag)
        ),
        next_url=_link(soup.select_one("ul.seriesPager li.next a[href]"), url),
        prev_url=_link(soup.select_one("ul.seriesPager li.prev a[href]"), url),
        work_url=_link(soup.select_one("p.back a[href]"), url),
        notice=_text(notice),
        episode_urls=tuple(_episode_links(lineup, url)) if isinstance(lineup, Tag) else (),
        title=_text(soup.title),
        writer=_text(soup.find("p", class_="author")),
    )


def _text(tag: Tag | None) -> str:
    return tag.get_text(strip=True) if isinstance(tag, Tag) else ""


def _link(anchor: Tag | None, url: str) -> str | None:
    if not isinstance(anchor, Tag):
        return None
    href = str(anchor["href"]).split("#", 1)[0]
    return urljoin(url, href) if href else None


def _episode_links(lineup: Tag, url: str) -> Iterator[str]:
    """The episode links of a work page's lineup, oldest first, deduplicated.

    The lineup runs newest first and only links the episodes still free to
    read; an expired one is dropped from it altogether, so there is nothing to
    skip here.
    """
    seen: set[str] = set()
    for anchor in reversed(lineup.select("a[href]")):
        absolute = urljoin(url, str(anchor["href"]).split("#", 1)[0])
        if _SERIES_PATH.match(urlparse(absolute).path) and absolute not in seen:
            seen.add(absolute)
            yield absolute


class Splush(Extractor):
    """Fetch episodes from Splush.

    A work page and an episode page share the `/series/<id>/` URL shape, so
    `is_series()` has to fetch the page; what it read is kept on the instance,
    keyed by URL, and `episode()` / `series_urls()` reuse it, so the CLI's
    `is_series()` + `episode()` sequence costs one request.
    """

    NAME = "splush"
    HOSTS = ("splush.jp", "www.splush.jp")
    PUBLISHER = "イースト・プレス"
    URL_FORMS = ("https://www.splush.jp/series/<id>",)

    def __init__(self, session: Client | None = None) -> None:
        """Build an extractor.

        Args:
            session: A session to reuse. A retrying one is made when omitted.
        """
        super().__init__(session)
        # Normalised page URL -> what the page said, so a page is read once.
        self._documents: dict[str, Document] = {}

    @classmethod
    def suitable(cls, url: str) -> bool:
        """Report whether the extractor takes `url`.

        Args:
            url: The URL to check.

        Returns:
            True for an https `/series/<id>/` URL on a known host.
        """
        return super().suitable(url) and _SERIES_PATH.match(urlparse(url).path) is not None

    def is_series(self, url: str) -> bool:
        """Report whether `url` is a work page rather than an episode.

        Fetches the page, since the URL alone cannot tell, and keeps it for
        `episode()` or `series_urls()`. Only the path shape is checked first,
        so a forced `--extractor splush` works on an unlisted host too.

        Args:
            url: The URL to check.

        Returns:
            True when the page lists episodes.

        Raises:
            NotAnEpisodePageError: The page is neither a work page nor an episode page.
        """
        if _SERIES_PATH.match(urlparse(url).path) is None:
            return False
        return self._document(url).is_work

    def series_urls(self, url: str) -> list[str]:
        """List every readable episode a work page links to, oldest first.

        Args:
            url: A work page URL.

        Returns:
            One episode URL per linked episode, oldest first.

        Raises:
            UnsupportedUrlError: The URL is not a work page.
            NotAnEpisodePageError: The page links no episode.
        """
        if _SERIES_PATH.match(urlparse(url).path) is None:
            msg = f"{url} is not a series page."
            raise UnsupportedUrlError(msg)
        document = self._document(url)
        if not document.is_work:
            msg = f"{url} is an episode, not a series page."
            raise UnsupportedUrlError(msg)
        if not document.episode_urls:
            msg = f"the series at {url} lists no readable episode."
            raise NotAnEpisodePageError(msg)
        return list(document.episode_urls)

    def episode(self, url: str) -> Episode:
        """Read one episode and list its pages.

        Args:
            url: The episode URL.

        Returns:
            The episode. `pages` is empty once its free run has ended, when the
            page says `公開終了しました。` in place of the images; the pager
            still names the next episode.

        Raises:
            NotAnEpisodePageError: The page is a work page, or neither kind.
        """
        document = self._document(url)
        if document.is_work:
            msg = f"{url} is a work page, not an episode."
            raise NotAnEpisodePageError(msg)
        return Episode(
            url=document.url,
            series_title=document.series_title,
            episode_title=document.episode_title,
            pages=tuple(Page(url=src) for src in document.images),
            prev_url=document.prev_url,
            next_url=document.next_url,
            metadata={
                "title": document.title,
                "notice": document.notice,
                "expired": _EXPIRED_NOTICE in document.notice,
                "prev_url": document.prev_url,
                "work_url": document.work_url,
                "images": list(document.images),
            },
            writer=document.writer,
            publisher=self.PUBLISHER,
        )

    def _document(self, url: str) -> Document:
        """Read a `/series/<id>/` page, once per URL."""
        key = self._key(url)
        if key not in self._documents:
            res = self._session.get(key, headers=self.HEADERS, timeout=self.TIMEOUT)
            if res.status_code == HTTPStatus.NOT_FOUND:
                msg = f"{url} is gone (HTTP 404): not a work page nor an episode."
                raise NotAnEpisodePageError(msg)
            res.raise_for_status()
            self._documents[key] = parse_document(res.content, str(res.url or key))
        return self._documents[key]

    @staticmethod
    def _key(url: str) -> str:
        """`url` without query, fragment or a missing trailing slash, as WordPress serves it."""
        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}/"
