"""マガポケ (Magazine Pocket, 講談社), and the K MANGA viewer it runs.

The site is a Nuxt app whose data comes from Kodansha's K MANGA API -- the
same one Comic NORA's white-label viewer talks to (`viewers/kmanga.py`), with the
signing header renamed to `x-manga-hash`. An episode lives at
`/title/<title id, 5 digits>/episode/<episode id>`; `/title/<title id>` is the
work page, which the site redirects to the first episode and whose episode
list is what the title API answers.

- `GET /web/episode?episode_id=<id>` on `api.<host>` names the episode and
  its title, for a locked episode too; an unknown id is a `400` with
  `response_code` 3100.
- `GET /web/episode/viewer?episode_id=<id>` on `se-api.<host>` answers the
  page list, the next episode and `scramble_seed`. An episode that wants a
  ticket, points or a subscription is a `400` with `response_code` 3105
  (`episode unpurchased`), an unreleased one 3104.
- `GET /web/title/detail?title_id=<id>` lists the episode ids, oldest first.

Every call needs `x-manga-platform: 3` and `x-manga-hash`, which is
`kmanga.service_hash()` of the query parameters -- and, unlike Nora, the site
checks it (`invalid hash` without one).

Pages are signed CloudFront JPEGs on `mgpk-cdn.magazinepocket.com`, served
without a Referer or a cookie, and scrambled the way Nora's are
(`kmanga.descramble()`: a 4 x 4 tile shuffle keyed by an xorshift32 seed). The
seed is hidden a little better here: `scramble_seed` is a string the
viewer's WebAssembly turns into the number by mapping each character to a
digit through one of two ten-letter alphabets -- `svdk0m7acl` for an even
title id, `q6jtf2xnog` for an odd one -- and XORing the result with
`title_id + episode_id`. A seed that does not parse leaves the page as is.

Signing in wants a reCAPTCHA v3 token with the credentials, so `login()` is
not supported.
"""

from __future__ import annotations

import re
from http import HTTPStatus
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from getjmanga.errors import NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractor import Episode, Extractor, Page, neighbours, ordinal, published_on
from getjmanga.viewers.kmanga import descramble, service_hash

if TYPE_CHECKING:
    from collections.abc import Mapping

    from httpx2 import Client
    from PIL import Image

_TITLE_PATH = re.compile(r"^/title/(?P<title>\d{5})/?$")
_EPISODE_PATH = re.compile(r"^/title/(?P<title>\d{5})/episode/(?P<episode>\d+)/?$")

#: The web platform id the API is told, and the headers it wants with it.
_PLATFORM = "3"
_HASH_HEADER = "x-manga-hash"
_CRAWLER_HEADER = "x-manga-is-crawler"
_PLATFORM_HEADER = "x-manga-platform"
#: The API hosts: the viewer and the login go to the `se-` one, everything else to the plain one.
_API_ORIGIN = "https://api.pocket.shonenmagazine.com"
_SECURE_API_ORIGIN = "https://se-api.pocket.shonenmagazine.com"
_SECURE_PATHS = frozenset({"/web/episode/viewer", "/web/user/login"})
#: The API's `response_code` for an id that names nothing.
_EPISODE_NOT_FOUND = 3100

#: The digit alphabets `scramble_seed` is written in, picked by the title id's parity.
SEED_ALPHABETS = ("svdk0m7acl", "q6jtf2xnog")
_SEED_MIN = 1
_SEED_MAX = 2**32 - 1
_MASK = 0xFFFFFFFF


def scramble_seed(title_id: int, episode_id: int, seed: str) -> int | None:
    """Turn the API's `scramble_seed` string into the xorshift32 seed the viewer uses.

    Args:
        title_id: The `title_id` of the episode.
        episode_id: The `episode_id` of the episode.
        seed: The `scramble_seed` the viewer API answered.

    Returns:
        The seed for `kmanga.descramble()`, or None when the string does not
        parse (a character outside the alphabet, or a number past 32 bits),
        in which case the viewer draws the page as served.
    """
    alphabet = SEED_ALPHABETS[title_id & 1]
    digits: list[str] = []
    for char in seed:
        index = alphabet.find(char)
        if index < 0:
            return None
        digits.append(str(index))
    if not digits:
        return None
    number = int("".join(digits))
    if number > _MASK:
        return None
    return (number ^ (title_id + episode_id)) & _MASK


class MagaPoke(Extractor):
    """Fetch episodes from マガポケ."""

    NAME = "magapoke"
    HOSTS = ("pocket.shonenmagazine.com",)
    URL_FORMS = (
        "https://pocket.shonenmagazine.com/title/<title id>/episode/<episode id>",
        "https://pocket.shonenmagazine.com/title/<title id>",
    )
    PUBLISHER = "講談社"

    def __init__(self, session: Client | None = None) -> None:
        """Build an extractor.

        Args:
            session: A session to reuse. A retrying one is made when omitted.
        """
        super().__init__(session)
        #: `author_text` of each work asked about, by title id.
        #: The `web_title` of every work asked about, by title id (None for one the API knows no such title).
        self._titles: dict[int, dict[str, Any] | None] = {}

    @classmethod
    def suitable(cls, url: str) -> bool:
        """Report whether the extractor takes `url`.

        Args:
            url: The URL to check.

        Returns:
            True for an https episode or work page on a known host.
        """
        if not super().suitable(url):
            return False
        path = urlparse(url).path
        return _EPISODE_PATH.match(path) is not None or _TITLE_PATH.match(path) is not None

    @classmethod
    def is_series(cls, url: str) -> bool:
        """Report whether `url` is a work page.

        Args:
            url: The URL to check.

        Returns:
            True for `/title/<title id>`.
        """
        return _TITLE_PATH.match(urlparse(url).path) is not None

    def series_urls(self, url: str) -> list[str]:
        """List every episode of a work, oldest first.

        Args:
            url: A work page URL.

        Returns:
            One episode URL per listed episode, locked ones included,
            deduplicated.

        Raises:
            UnsupportedUrlError: The URL is not a work page.
            NotAnEpisodePageError: There is no such work, or it lists no episode.
        """
        match = _TITLE_PATH.match(urlparse(url).path)
        if match is None:
            msg = f"{url} is not a work page."
            raise UnsupportedUrlError(msg)
        title_id = int(match["title"])
        detail = self._title(title_id)
        if detail is None:
            msg = f"no work at {url}."
            raise NotAnEpisodePageError(msg)
        urls: list[str] = []
        for episode_id in detail.get("episode_id_list") or []:
            episode_url = _episode_url(title_id, int(episode_id))
            if episode_url not in urls:
                urls.append(episode_url)
        if not urls:
            msg = f"the work at {url} lists no episode."
            raise NotAnEpisodePageError(msg)
        return urls

    def episode(self, url: str) -> Episode:
        """Read one episode and list its pages.

        Args:
            url: An episode URL.

        Returns:
            The episode. `pages` is empty when the viewer API refuses it (a
            ticket, points or a subscription wanted, or not released yet);
            `next_url` is what the API names as the next one, or the next id
            the work lists when the episode is locked.

        Raises:
            NotAnEpisodePageError: The URL is not an episode URL, or the API
                knows no such episode.
        """
        match = _EPISODE_PATH.match(urlparse(url).path)
        if match is None:
            msg = f"{url} is not an episode page."
            raise NotAnEpisodePageError(msg)
        episode_id = int(match["episode"])
        status, detail = self._api("/web/episode", {"episode_id": episode_id})
        if status == HTTPStatus.BAD_REQUEST and detail.get("response_code") == _EPISODE_NOT_FOUND:
            msg = f"no episode at {url}."
            raise NotAnEpisodePageError(msg)
        info = _mapping(detail.get("episode"))
        share = _mapping(detail.get("share"))
        title_id = int(info.get("title_id") or match["title"])
        series_title = str(share.get("title_name") or "")
        episode_title = str(info.get("episode_name") or episode_id)
        episode_url = _episode_url(title_id, episode_id)

        status, viewer = self._api("/web/episode/viewer", {"episode_id": episode_id})
        metadata: dict[str, Any] = {"title_id": title_id, "episode_id": episode_id, "episode": detail}
        if status != HTTPStatus.OK or viewer.get("status") != "success":
            # Wants a ticket, points, a subscription, or is not released yet.
            prev_id, next_id = self._listed_ids(title_id, episode_id)
            return Episode(
                url=episode_url,
                series_title=series_title,
                episode_title=episode_title,
                prev_url=_episode_url(title_id, prev_id) if prev_id is not None else None,
                next_url=_episode_url(title_id, next_id) if next_id is not None else None,
                metadata={**metadata, "viewer": viewer},
                writer=self._writer(title_id),
                publisher=self.PUBLISHER,
                published=published_on(info.get("start_time")),
                number=self._listed_position(title_id, episode_id),
            )

        raw_seed = viewer.get("scramble_seed")
        seed = scramble_seed(title_id, episode_id, str(raw_seed)) if raw_seed is not None else None
        extra = {"seed": seed} if seed is not None and _SEED_MIN <= seed <= _SEED_MAX else {}
        preceding, following = viewer.get("previous_episode"), viewer.get("next_episode")
        prev_id = preceding.get("episode_id") if isinstance(preceding, dict) else None
        prev_title = preceding.get("title_id") if isinstance(preceding, dict) else None
        next_id = following.get("episode_id") if isinstance(following, dict) else None
        next_title = following.get("title_id") if isinstance(following, dict) else None
        return Episode(
            url=episode_url,
            series_title=series_title,
            episode_title=episode_title,
            pages=tuple(Page(url=str(src), extra=extra) for src in viewer.get("page_list") or []),
            prev_url=_episode_url(int(prev_title or title_id), int(prev_id)) if prev_id is not None else None,
            next_url=_episode_url(int(next_title or title_id), int(next_id)) if next_id is not None else None,
            metadata={**metadata, "viewer": viewer},
            writer=self._writer(title_id),
            publisher=self.PUBLISHER,
            published=published_on(info.get("start_time")),
            number=self._listed_position(title_id, episode_id),
        )

    def _writer(self, title_id: int) -> str:
        """The work's `author_text`; the episode API names no author."""
        return str((self._title(title_id) or {}).get("author_text") or "")

    def image(self, page: Page, episode: Episode) -> Image.Image:
        """Fetch one page and unscramble it when the episode has a seed.

        Args:
            page: The page to fetch.
            episode: The episode the page belongs to.

        Returns:
            The page image.
        """
        image = self._fetch_image(page.url, headers={**self.HEADERS, "Referer": episode.url})
        seed = page.extra.get("seed")
        return descramble(image, int(seed)) if seed is not None else image

    def _title(self, title_id: int) -> dict[str, Any] | None:
        """The `web_title` of a work, asked for once; None when the API knows no such title."""
        if title_id not in self._titles:
            _, data = self._api("/web/title/detail", {"title_id": title_id})
            # An unknown title is a 400 with response code 3000 and no `web_title`.
            title = data.get("web_title")
            self._titles[title_id] = title if isinstance(title, dict) else None
        return self._titles[title_id]

    def _listed_ids(self, title_id: int, episode_id: int) -> tuple[int | None, int | None]:
        """The ids the work lists either side of `episode_id`, None at either end (or both when unlisted)."""
        return neighbours(self._episode_ids(title_id), episode_id)

    def _listed_position(self, title_id: int, episode_id: int) -> int | None:
        """Where the work lists `episode_id`, counted from 1; None when unlisted."""
        return ordinal(self._episode_ids(title_id), episode_id)

    def _episode_ids(self, title_id: int) -> list[int]:
        """The work's `episode_id_list`, oldest first."""
        title = self._title(title_id)
        return [int(value) for value in (title or {}).get("episode_id_list") or []]

    def _api(self, path: str, params: Mapping[str, str | int]) -> tuple[int, dict[str, Any]]:
        """GET one API path, signed the viewer's way.

        Returns the status and the JSON body. A `400` is returned, not raised:
        it is how the API says "locked" or "no such thing"; anything else
        that fails raises.
        """
        query = {key: str(value) for key, value in params.items()}
        headers = {
            **self.HEADERS,
            _HASH_HEADER: service_hash(query),
            _CRAWLER_HEADER: "false",
            _PLATFORM_HEADER: _PLATFORM,
            "Origin": f"https://{self.HOSTS[0]}",
            "Referer": f"https://{self.HOSTS[0]}/",
        }
        origin = _SECURE_API_ORIGIN if path in _SECURE_PATHS else _API_ORIGIN
        res = self._session.get(f"{origin}{path}", params=query, headers=headers, timeout=self.TIMEOUT)
        if res.status_code != HTTPStatus.BAD_REQUEST:
            res.raise_for_status()
        data = res.json()
        return res.status_code, data if isinstance(data, dict) else {}


def _mapping(value: object) -> dict[str, Any]:
    """`value` when it is a dict, else an empty one."""
    return value if isinstance(value, dict) else {}


def _episode_url(title_id: int, episode_id: int) -> str:
    return f"https://pocket.shonenmagazine.com/title/{title_id:05d}/episode/{episode_id}"
