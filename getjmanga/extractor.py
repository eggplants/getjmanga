"""What every site extractor builds on, and what it hands back.

A site is supported by subclassing `Extractor`, the way yt-dlp does it: name
it, list the hosts it takes, and implement `episode()`. Everything else --
listing a series, unscrambling pages, signing in -- has a default that a site
overrides only when it needs to.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from io import BytesIO
from typing import TYPE_CHECKING, Any, ClassVar, TypeVar
from urllib.parse import urlparse

from PIL import Image

from .errors import GetjmangaError, LoginError, UnsupportedUrlError
from .session import HEADERS, make_session

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from httpx import Client, Response

T = TypeVar("T")

#: The sites' own clock: a date is the day it was in Japan.
JST = timezone(timedelta(hours=9), "JST")
#: `2026年9月21日`, `2026/09/21`, `2026-09-21`, `2026.9.21`, with or without the time after.
_DATE = re.compile(r"(?P<year>\d{4})[年/.\-](?P<month>\d{1,2})[月/.\-](?P<day>\d{1,2})")
#: An epoch in milliseconds rather than seconds, going by its size.
_EPOCH_MS = 10**11


def published_on(value: object) -> date | None:
    """The day, in Japan, a site's timestamp falls on.

    Args:
        value: What the site said: an ISO 8601 string (`2026-09-20T15:00:00Z`,
            an offset, or none -- then taken as JST), an HTTP date
            (`Thu, 21 Aug 2025 08:16:41 GMT`), a date written `2026/09/21`,
            `2026-09-21`, `2026.9.21` or `2026年9月21日` (anything after the
            day is ignored), or an epoch in seconds or milliseconds.

    Returns:
        The date, or None when `value` is empty or says no date.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        return _epoch_day(value)
    text = str(value).strip()
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return _http_day(text) or _written_day(text)
    return (moment if moment.tzinfo is None else moment.astimezone(JST)).date()


def _http_day(text: str) -> date | None:
    try:
        moment = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    return (moment if moment.tzinfo is None else moment.astimezone(JST)).date()


def _epoch_day(value: float) -> date | None:
    if value <= 0:
        return None
    seconds = value / 1000 if value >= _EPOCH_MS else value
    return datetime.fromtimestamp(seconds, tz=JST).date()


def _written_day(text: str) -> date | None:
    match = _DATE.search(text)
    if match is None:
        return None
    try:
        return date(int(match["year"]), int(match["month"]), int(match["day"]))
    except ValueError:
        return None


def neighbours(items: Sequence[T], current: T) -> tuple[T | None, T | None]:
    """The items before and after `current` in `items`, for `prev_url` and `next_url`.

    Args:
        items: A listing in reading order.
        current: The item to look either side of.

    Returns:
        `(before, after)`; None at either end, or both when `current` is not listed.
    """
    try:
        position = items.index(current)
    except ValueError:
        return None, None
    before = items[position - 1] if position else None
    after = items[position + 1] if position + 1 < len(items) else None
    return before, after


@dataclass(frozen=True)
class Page:
    """One page image of an episode."""

    url: str
    width: int = 0
    height: int = 0
    #: Whatever the extractor needs to put the page back together -- a tile
    #: permutation, a shuffle seed -- kept opaque here and read back by its
    #: own `Extractor.image()`. Must be JSON-serialisable, for `--metadata`.
    extra: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Episode:
    """What an extractor says about one episode."""

    url: str
    series_title: str
    episode_title: str
    #: The pages in reading order. Empty when the episode is not readable
    #: -- it wants a purchase, a wait or a login.
    pages: tuple[Page, ...] = ()
    #: The episode that follows this one, for a bulk run to walk to.
    next_url: str | None = None
    #: The episode before this one, for `-B` to walk back to.
    prev_url: str | None = None
    #: The episode as the site described it, written out by `--metadata`.
    metadata: Mapping[str, Any] = field(default_factory=dict)
    #: Who the work is by, as the site credits them -- several names joined
    #: the way the site joins them. Empty when the site does not say.
    writer: str = ""
    #: Who publishes the work: what the site says, else the site's own publisher.
    publisher: str = ""
    #: The day the episode came out, in Japan -- or, on a site that never says,
    #: the day its first page was last uploaded. None when neither is known.
    published: date | None = None

    @property
    def readable(self) -> bool:
        """Whether the episode has pages to download."""
        return bool(self.pages)


class Extractor(ABC):
    """Reads episodes off one site, or one family of sites sharing a viewer."""

    #: The name `--extractor` selects the class by.
    NAME: ClassVar[str] = ""
    #: The hostnames the extractor takes. `suitable()` checks against them.
    HOSTS: ClassVar[tuple[str, ...]] = ()
    #: The URL shapes the extractor reads, for `--list-extractors`.
    URL_FORMS: ClassVar[tuple[str, ...]] = ()
    #: The `[site.<key>]` section of the config file whose credentials work
    #: on every host of the extractor. Empty when accounts are per site, in
    #: which case only a `[site."<host>"]` section applies.
    CONFIG_KEY: ClassVar[str] = ""
    #: The publisher behind the site, for `Episode.publisher` when the page
    #: itself does not name one. Empty when the site is not a publisher's own.
    PUBLISHER: ClassVar[str] = ""
    #: The same per host, for an extractor whose hosts belong to different publishers.
    PUBLISHERS: ClassVar[Mapping[str, str]] = {}
    #: Headers sent with every request.
    HEADERS: ClassVar[dict[str, str]] = HEADERS
    #: Seconds to wait for a response.
    TIMEOUT: ClassVar[int] = 30
    #: Seconds to wait for a page image.
    IMAGE_TIMEOUT: ClassVar[int] = 60

    def __init__(self, session: Client | None = None) -> None:
        """Build an extractor.

        Args:
            session: A session to reuse. A retrying one is made when omitted.
        """
        self._session = session if session is not None else make_session()
        #: `series_urls()` answers, by series URL, for `_listed_neighbours()`.
        self._series_listings: dict[str, list[str]] = {}

    @property
    def session(self) -> Client:
        """The session requests go through."""
        return self._session

    @classmethod
    def suitable(cls, url: str) -> bool:
        """Report whether the extractor takes `url`.

        Args:
            url: The URL to check.

        Returns:
            True when the URL is https and its host is one of `HOSTS`.
        """
        parsed = urlparse(url)
        return parsed.scheme == "https" and parsed.hostname in cls.HOSTS

    @classmethod
    def publisher(cls, url: str) -> str:
        """The publisher behind the site `url` is on.

        Args:
            url: A URL on one of `HOSTS`.

        Returns:
            The host's entry in `PUBLISHERS`, else `PUBLISHER`.
        """
        return cls.PUBLISHERS.get(urlparse(url).hostname or "", cls.PUBLISHER)

    def is_series(self, url: str) -> bool:  # noqa: ARG002
        """Report whether `url` names a whole series rather than one episode.

        Most sites tell the two apart by the URL alone, so overrides are
        usually classmethods; a site whose series and episode pages share a
        URL shape may fetch the page here and keep it for `episode()`.

        Args:
            url: The URL to check.

        Returns:
            True when `series_urls()` can list the episodes of the URL.
        """
        return False

    def series_urls(self, url: str) -> list[str]:
        """List every episode a series URL covers.

        Args:
            url: A URL `is_series()` accepted.

        Returns:
            One episode URL per listed episode, in the order to download them.

        Raises:
            UnsupportedUrlError: The extractor lists no series.
        """
        msg = f"{self.NAME} cannot list a series from {url}."
        raise UnsupportedUrlError(msg)

    def _listed_neighbours(self, series_url: str, episode_url: str) -> tuple[str | None, str | None]:
        """The episodes `series_urls(series_url)` lists either side of `episode_url`.

        For a site whose episode page names the next episode but not the
        previous one: the listing is fetched once per series and kept.

        Args:
            series_url: The series `episode_url` belongs to.
            episode_url: The episode, spelled the way `series_urls()` spells it.

        Returns:
            `(before, after)`; None at either end, or both when the series
            cannot be listed or does not list the episode.
        """
        if series_url not in self._series_listings:
            try:
                self._series_listings[series_url] = self.series_urls(series_url)
            except GetjmangaError:
                self._series_listings[series_url] = []
        return neighbours(self._series_listings[series_url], episode_url)

    @abstractmethod
    def episode(self, url: str) -> Episode:
        """Read one episode: its titles, its pages and what follows it.

        Args:
            url: The episode URL.

        Returns:
            The episode, with no pages when it is not readable.

        Raises:
            NotAnEpisodePageError: The page describes no episode.
        """

    def image(self, page: Page, episode: Episode) -> Image.Image:
        """Fetch one page image, put back together if the site scrambles it.

        The default sends the episode URL as the Referer, which is what a site
        that checks one expects, and hands the image over as served.

        Args:
            page: The page to fetch.
            episode: The episode the page belongs to.

        Returns:
            The page image.
        """
        return self._fetch_image(page.url, headers={**self.HEADERS, "Referer": episode.url})

    def login(self, url: str, username: str, password: str) -> None:  # noqa: ARG002 (a stub sites fill in)
        """Sign in, so episodes the account may read become readable.

        Args:
            url: A URL on the site to sign in to.
            username: The account's id or email address.
            password: The account's password.

        Raises:
            LoginError: The extractor cannot sign in, or the site said no.
        """
        msg = f"{self.NAME} does not support logging in ({url})."
        raise LoginError(msg)

    def _get(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, str | int] | None = None,
        timeout: int | None = None,
    ) -> Response:
        """GET `url` and raise on a failing status.

        Args:
            url: The URL to fetch.
            headers: Headers to send instead of `HEADERS`.
            params: Query parameters.
            timeout: Seconds to wait instead of `TIMEOUT`.

        Returns:
            The response, after any redirects.
        """
        res = self._session.get(
            url,
            headers=dict(headers) if headers is not None else self.HEADERS,
            params=dict(params) if params is not None else None,
            timeout=timeout if timeout is not None else self.TIMEOUT,
        )
        res.raise_for_status()
        return res

    def _fetch_image(self, url: str, *, headers: Mapping[str, str] | None = None) -> Image.Image:
        """GET an image and decode it.

        Args:
            url: The image URL.
            headers: Headers to send instead of `HEADERS`.

        Returns:
            The decoded image.
        """
        res = self._get(url, headers=headers, timeout=self.IMAGE_TIMEOUT)
        return Image.open(BytesIO(res.content))

    def _dated_by_upload(self, episode: Episode) -> Episode:
        """The episode, dated by when its first page was uploaded.

        For a site that never says when an episode came out: the page image's
        `Last-Modified` header, read with one HEAD request that carries the
        episode as its Referer the way `image()` does. The upload usually
        precedes the release by days, so the day is a floor, not the date.

        Args:
            episode: The episode as read off the site, undated.

        Returns:
            The episode with `published` set; unchanged when it has no pages
            or the server sends no such header.
        """
        if not episode.pages:
            return episode
        res = self._session.head(
            episode.pages[0].url,
            headers={**self.HEADERS, "Referer": episode.url},
            follow_redirects=True,
            timeout=self.TIMEOUT,
        )
        uploaded = published_on(res.headers.get("last-modified")) if res.is_success else None
        return replace(episode, published=uploaded) if uploaded else episode

    def _cookie(self, name: str, host: str) -> str | None:
        """The value of the session's cookie `name` that applies to `host`.

        Every extractor shares one session, so a cookie name like Laravel's
        `XSRF-TOKEN` can be set by several sites at once; asking the jar by
        name alone then raises. This picks the one whose domain covers `host`.

        Args:
            name: The cookie name.
            host: The hostname the cookie is for.

        Returns:
            The value, or None when no such cookie applies to the host.
        """
        for cookie in self._session.cookies.jar:
            domain = cookie.domain.lstrip(".")
            if cookie.name == name and (host == domain or host.endswith("." + domain)):
                return cookie.value
        return None

    @staticmethod
    def _origin(url: str) -> str:
        """The `scheme://host` part of `url`."""
        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}"
