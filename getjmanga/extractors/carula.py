"""コミックカルラ (世界文化ブックス): a catalogue site whose episodes are note.com articles.

`carula.jp` itself is a static Vite app with two routes (`/` and `/books`)
that renders `works.csv`; every work links out to the publisher's note.com
account, where each episode is one article of `<figure><img>` pages. The
`/series/<13 hex>` shape left over from the site's Comici+ days is an
Apache redirect to the note article of the work, so it is taken too and
followed. The pages are read off note's JSON API
(`/api/v3/notes/<key>`), whose `body` is the article HTML: one
`<figure><img src="https://assets.st-note.com/img/...">` per page in reading
order, preceded by a paragraph linking every episode of the work, which
names the next episode. The images are plain JPEGs served without a Referer
or a cookie. A paid episode (`price` > 0, ¥100 apiece) comes back with the
free preview only and `remained_figure_num` > 0, so it counts as locked.
A note magazine (`/carula/m/<key>`) is the only series listing the account
has, read through `/api/v1/layout/magazine/<key>/section`, first episode
first. Signing in to note.com is not implemented.
"""

from __future__ import annotations

import csv
import re
from http import HTTPStatus
from io import StringIO
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from bs4.element import Tag

from getjmanga.errors import NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractor import Episode, Extractor, Page, neighbours, ordinal, published_on

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    from httpx2 import Client

_CATALOGUE_HOST = "carula.jp"
_NOTE_HOST = "note.com"
#: The publisher's note.com account.
CREATOR = "carula"

# One episode: a note article of the account.
_NOTE_PATH = re.compile(rf"^/{CREATOR}/n/(?P<key>n[0-9a-f]{{12}})/?$")
# A note magazine of the account, listing the episodes of one work.
_MAGAZINE_PATH = re.compile(rf"^/{CREATOR}/m/(?P<key>m[0-9a-f]{{12}})/?$")
# The Comici+-era work URL on carula.jp, redirected by Apache to a note article.
_LEGACY_SERIES_PATH = re.compile(r"^/series/(?P<id>[0-9a-f]{13})/?$")

# `『<work>』<episode>`, the way the account names every episode article.
_TITLE = re.compile(r"^(?P<before>.*?)『(?P<series>[^』]+)』(?P<after>.*)$", re.DOTALL)

_NOTE_API = f"https://{_NOTE_HOST}/api/v3/notes/{{key}}"
_MAGAZINE_API = f"https://{_NOTE_HOST}/api/v1/layout/magazine/{{key}}/section"
_NOTE_URL = f"https://{_NOTE_HOST}/{CREATOR}/n/{{key}}"
#: The catalogue the site renders: one work per row, its title and up to three credited authors.
CATALOGUE_URL = f"https://{_CATALOGUE_HOST}/works.csv"

_API_HEADERS: dict[str, str] = {**Extractor.HEADERS, "Accept": "application/json, text/plain, */*"}


def parse_catalogue(text: str) -> dict[str, str]:
    """The credits of every work in `works.csv`, by title.

    Args:
        text: The CSV, whose header names `タイトル`, `著者1`..`著者3` and `クレジット1`..`クレジット3`.

    Returns:
        `{title: "名前 (役割), 名前 (役割)"}`, a name without a credit on its own.
    """
    catalogue: dict[str, str] = {}
    for row in csv.DictReader(StringIO(text)):
        credited = []
        for n in (1, 2, 3):
            name, role = (row.get(f"著者{n}") or "").strip(), (row.get(f"クレジット{n}") or "").strip()
            if name:
                credited.append(f"{name} ({role})" if role else name)
        title = (row.get("タイトル") or "").strip()
        if title and credited:
            catalogue[title] = ", ".join(credited)
    return catalogue


def split_title(name: str, fallback: str) -> tuple[str, str]:
    """Split an article name into the work's title and the episode's.

    Args:
        name: The article name, `『<work>』<episode>` on every episode of the account.
        fallback: The series title to use when the name has no `『』` part.

    Returns:
        The series title and the episode title, both raw.
    """
    match = _TITLE.match(name)
    if match is None:
        return fallback, name.strip()
    episode = f"{match['before'].strip()} {match['after'].strip()}".strip()
    return match["series"].strip(), episode or name.strip()


def page_urls(body: str) -> list[str]:
    """The page images of an article body, in reading order.

    Args:
        body: The article HTML the API hands over.

    Returns:
        The `src` of every `<figure><img>`; embeds without an image are skipped.
    """
    soup = BeautifulSoup(body, "html.parser")
    return [str(img["src"]) for img in soup.select("figure img[src]") if isinstance(img, Tag)]


def listed_keys(body: str, key: str) -> list[str]:
    """Find the episode index `key` is in, off the article.

    Every episode article opens with a paragraph linking all the episodes of
    the work in order, the current one included.

    Args:
        body: The article HTML the API hands over.
        key: The current article's key.

    Returns:
        The keys of the linked episodes in order; empty when no paragraph
        links `key` at all.
    """
    soup = BeautifulSoup(body, "html.parser")
    for paragraph in soup.find_all("p"):
        keys = list(_linked_keys(paragraph))
        if key in keys:
            return keys
    return []


def _linked_keys(paragraph: Tag) -> Iterator[str]:
    for anchor in paragraph.find_all("a", href=True):
        if not isinstance(anchor, Tag):
            continue
        parsed = urlparse(str(anchor["href"]))
        match = _NOTE_PATH.match(parsed.path)
        if parsed.hostname == _NOTE_HOST and match is not None:
            yield match["key"]


def is_locked(note: Mapping[str, Any]) -> bool:
    """Report whether the API served a paywalled article without its pages.

    Args:
        note: The `data` object of `/api/v3/notes/<key>`.

    Returns:
        True when the article is for sale and not bought, or was cut short.
    """
    for_sale = int(note.get("price") or 0) > 0 and not note.get("is_purchased")
    cut_short = int(note.get("remained_figure_num") or 0) > 0 or int(note.get("remained_image_num") or 0) > 0
    return for_sale or cut_short


class Carula(Extractor):
    """Fetch episodes of コミックカルラ off the publisher's note.com account."""

    NAME = "carula"
    HOSTS = (_CATALOGUE_HOST, _NOTE_HOST)
    URL_FORMS = (
        f"https://{_NOTE_HOST}/{CREATOR}/n/<key>",
        f"https://{_NOTE_HOST}/{CREATOR}/m/<key>",
        f"https://{_CATALOGUE_HOST}/series/<id>",
    )
    PUBLISHER = "世界文化ブックス"

    def __init__(self, session: Client | None = None) -> None:
        """Build an extractor.

        Args:
            session: A session to reuse. A retrying one is made when omitted.
        """
        super().__init__(session)
        #: The catalogue's credits by work title, once read.
        self._catalogue: dict[str, str] | None = None

    @classmethod
    def suitable(cls, url: str) -> bool:
        """Take the account's note articles and magazines, and the catalogue's legacy work URLs.

        Args:
            url: The URL to check.

        Returns:
            True for one of `URL_FORMS`.
        """
        if not super().suitable(url):
            return False
        parsed = urlparse(url)
        if parsed.hostname == _CATALOGUE_HOST:
            return _LEGACY_SERIES_PATH.match(parsed.path) is not None
        return _NOTE_PATH.match(parsed.path) is not None or _MAGAZINE_PATH.match(parsed.path) is not None

    @classmethod
    def is_series(cls, url: str) -> bool:
        """Report whether `url` is a note magazine of the account.

        Args:
            url: The URL to check.

        Returns:
            True for `https://note.com/carula/m/<key>`.
        """
        parsed = urlparse(url)
        return parsed.hostname == _NOTE_HOST and _MAGAZINE_PATH.match(parsed.path) is not None

    def series_urls(self, url: str) -> list[str]:
        """List the episodes of a note magazine, in the magazine's order (first episode first).

        Args:
            url: A magazine URL.

        Returns:
            One article URL per listed episode, deduplicated.

        Raises:
            UnsupportedUrlError: `url` is not a magazine URL.
            NotAnEpisodePageError: The magazine lists no article.
        """
        parsed = urlparse(url)
        match = _MAGAZINE_PATH.match(parsed.path) if parsed.hostname == _NOTE_HOST else None
        if match is None:
            msg = f"{url} is not a magazine page."
            raise UnsupportedUrlError(msg)
        urls: list[str] = []
        page = 1
        while True:
            res = self._get(_MAGAZINE_API.format(key=match["key"]), headers=_API_HEADERS, params={"page": page})
            section = res.json()["data"]["section"]
            for content in section.get("contents") or ():
                key = content.get("key")
                if content.get("type") == "TextNote" and key and _NOTE_URL.format(key=key) not in urls:
                    urls.append(_NOTE_URL.format(key=key))
            if section.get("is_last_page", True) or not section.get("contents"):
                break
            page += 1
        if not urls:
            msg = f"the magazine at {url} lists no episode."
            raise NotAnEpisodePageError(msg)
        return urls

    def episode(self, url: str) -> Episode:
        """Read one episode article off note's API.

        Args:
            url: An article URL, or a legacy `carula.jp/series/<id>` URL that redirects to one.

        Returns:
            The episode, with no pages when it is for sale.

        Raises:
            NotAnEpisodePageError: The URL leads to no article, or the article carries no page.
        """
        key = self._article_key(url)
        res = self._session.get(_NOTE_API.format(key=key), headers=_API_HEADERS, timeout=self.TIMEOUT)
        if res.status_code == HTTPStatus.NOT_FOUND:
            msg = f"no note article for {url}."
            raise NotAnEpisodePageError(msg)
        res.raise_for_status()
        note = res.json()["data"]
        body = str(note.get("body") or "")
        nickname = str((note.get("user") or {}).get("nickname") or CREATOR)
        series_title, episode_title = split_title(str(note.get("name") or ""), nickname)
        listed = listed_keys(body, key)
        preceding, following = neighbours(listed, key)
        locked = is_locked(note)
        pages = () if locked else tuple(Page(url=src) for src in page_urls(body))
        if not pages and not locked:
            msg = f"no page image in the note article at {url}."
            raise NotAnEpisodePageError(msg)
        return Episode(
            url=_NOTE_URL.format(key=key),
            series_title=series_title,
            episode_title=episode_title,
            pages=pages,
            prev_url=_NOTE_URL.format(key=preceding) if preceding else None,
            next_url=_NOTE_URL.format(key=following) if following else None,
            metadata=note,
            writer=self.credits(series_title),
            publisher=self.PUBLISHER,
            published=published_on(note.get("publish_at")),
            number=ordinal(listed, key),
        )

    def credits(self, title: str) -> str:
        """Who the catalogue credits the work `title` to: `名前 (役割)` each, the roles as the CSV gives them.

        The catalogue is read once per extractor. A work the note account
        names differently from the catalogue gets no credits.
        """
        if self._catalogue is None:
            self._catalogue = parse_catalogue(self._get(CATALOGUE_URL).text)
        return self._catalogue.get(title, "")

    def _article_key(self, url: str) -> str:
        """The note article key `url` names, following a legacy catalogue redirect."""
        parsed = urlparse(url)
        if parsed.hostname == _CATALOGUE_HOST:
            landed = str(self._get(url).url)
            parsed = urlparse(landed)
            url = landed
        match = _NOTE_PATH.match(parsed.path) if parsed.hostname == _NOTE_HOST else None
        if match is None:
            msg = f"{url} is not a note article of {CREATOR}."
            raise NotAnEpisodePageError(msg)
        return match["key"]
