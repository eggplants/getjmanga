"""What every site extractor builds on, and what it hands back.

A site is supported by subclassing `Extractor`, the way yt-dlp does it: name
it, list the hosts it takes, and implement `episode()`. Everything else --
listing a series, unscrambling pages, signing in -- has a default that a site
overrides only when it needs to.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from io import BytesIO
from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import urlparse

from PIL import Image

from .errors import LoginError, UnsupportedUrlError
from .session import HEADERS, make_session

if TYPE_CHECKING:
    from collections.abc import Mapping

    from httpx import Client, Response


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
    #: The episode as the site described it, written out by `--metadata`.
    metadata: Mapping[str, Any] = field(default_factory=dict)

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
