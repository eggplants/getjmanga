"""BeLToon (レジンエンターテインメント), a Balcony-platform site whose viewer page carries its pages.

BeLToon is not Lezhin's own stack: it runs Balcony (`balcony.studio`), a
pages-router Next.js app. The viewer page is server-rendered with the whole
image list in `__NEXT_DATA__` (signed CloudFront URLs), and the work page's
episode list comes from the `/api/balcony-api-v2/contents/<alias>` API, which
wants the platform headers the browser bundle sends. An adult-flagged work is
readable without an account once the `not-login-adult=Y` cookie the site's own
age gate sets is on the session.

A scrambled page (`isScramble`) is a 4x4 tile shuffle. Its permutation comes
encrypted per page (`point`, AES-256-CBC, base64) under a key the site hands
over for the episode's `line`; the browser falls back to a constant key when
that call fails, and so does this module.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import parse_qs, unquote, urlencode, urlparse

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from PIL import Image

from getjmanga.errors import GetjmangaError, LoginError, NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractor import Episode, Extractor, Page

if TYPE_CHECKING:
    from collections.abc import Sequence

    from requests import Session

BASE_URL = "https://www.beltoon.jp"
API_URL = f"{BASE_URL}/api/balcony-api-v2"
#: The site is a next-auth app; the email provider is called `EmailLogin`.
AUTH_URL = f"{BASE_URL}/api/auth"

#: The key the browser falls back to when the site will not name one for an episode's `line`.
FALLBACK_SCRAMBLE_KEY = "thisisBalconyScrambledKey1234!@#"
#: What the sign-in form HMACs `email + password` with (`EMAIL_LOGIN_SECRET` in the bundle).
EMAIL_LOGIN_SECRET = "vMwG9w/4D9+kcwtmsGr79tAS3iQCzLeCPhCxNUEIYoY="  # noqa: S105 (a public constant of the site's bundle)
#: The viewer cuts a scrambled page into this many tiles per side.
GRID = 4

#: The headers the bundle's axios client sends every API call with.
_API_HEADERS = {
    "Accept": "application/json",
    "x-balcony-id": "BELTOON_JP",
    "x-balcony-timeZone": "Asia/Tokyo",
    "x-platform": "WEB",
}

# `/viewer/<alias>/<episode alias>`, optionally with `?isSample=true` for a sample.
_VIEWER_PATH = re.compile(r"^/viewer/(?P<alias>[\w-]+)/(?P<episode>[\w-]+)/?$")
# `/detail/<alias>`, the work page.
_DETAIL_PATH = re.compile(r"^/detail/(?P<alias>[\w-]+)/?$")
_NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__" type="application/json">(?P<json>.*?)</script>', re.DOTALL)
#: Query parameters the viewer reads off its own URL and the server honours.
_VIEWER_QUERY = ("isSample", "isCampaign")

#: Viewer errors that mean "no such episode" rather than "not for you".
_NOT_AN_EPISODE = frozenset({"NOT_FOUND_EPISODE", "NOT_EXIST", "INVALID_CONTENTS"})


def decrypt_index(point: str, key: str) -> list[int]:
    """Decrypt a page's `point` into its tile permutation.

    The browser imports the key's UTF-8 bytes as an AES-CBC key, uses the
    first 16 of them as the IV and JSON-parses the plaintext.

    Args:
        point: The encrypted permutation, base64.
        key: The key the site named for the episode, or the fallback.

    Returns:
        The permutation, one destination slot per source tile.

    Raises:
        GetjmangaError: The blob or the key is malformed, or the plaintext is no list.
    """
    key_bytes = key.encode()
    try:
        data = base64.b64decode(point, validate=True)
        decryptor = Cipher(algorithms.AES(key_bytes), modes.CBC(key_bytes[:16])).decryptor()
        plain = decryptor.update(data) + decryptor.finalize()
    except ValueError as error:
        msg = f"the page's scramble index cannot be decrypted: {error}"
        raise GetjmangaError(msg) from error
    padding = plain[-1] if plain else 0
    if not 1 <= padding <= 16 or plain[-padding:] != bytes([padding]) * padding:  # noqa: PLR2004 (PKCS#7 block)
        msg = "the decrypted scramble index carries no PKCS#7 padding; wrong key?"
        raise GetjmangaError(msg)
    try:
        index = json.loads(plain[:-padding].decode())
    except (UnicodeDecodeError, ValueError) as error:
        msg = "the decrypted scramble index is not JSON."
        raise GetjmangaError(msg) from error
    if not isinstance(index, list) or not all(isinstance(slot, int) for slot in index):
        msg = "the decrypted scramble index is not a list of integers."
        raise GetjmangaError(msg)
    return index


def descramble(image: Image.Image, index: Sequence[int]) -> Image.Image:
    """Put a scrambled page back together.

    The viewer draws tile `i` of the served image (row-major on a 4x4 grid)
    at slot `index[i]` of a canvas the same size.

    Args:
        image: The page as served.
        index: The permutation, one destination slot per source tile.

    Returns:
        The unscrambled page.

    Raises:
        GetjmangaError: The index is not a permutation of the 16 tiles.
    """
    if sorted(index) != list(range(GRID * GRID)):
        msg = f"the scramble index is not a permutation of {GRID * GRID} tiles: {list(index)}."
        raise GetjmangaError(msg)
    width, height = image.size
    tile_width, tile_height = width // GRID, height // GRID
    canvas = Image.new(image.mode, image.size)
    for source, target in enumerate(index):
        left, top = source % GRID * tile_width, source // GRID * tile_height
        tile = image.crop((left, top, left + tile_width, top + tile_height))
        canvas.paste(tile, (target % GRID * tile_width, target // GRID * tile_height))
    return canvas


def episode_url(alias: str, episode_alias: str, query: dict[str, str] | None = None) -> str:
    """The viewer URL of an episode.

    Args:
        alias: The work's alias, `12s1`.
        episode_alias: The episode's alias within the work, `1` or `p1`.
        query: Viewer query parameters to keep, `{"isSample": "true"}`.

    Returns:
        The viewer URL.
    """
    url = f"{BASE_URL}/viewer/{alias}/{episode_alias}"
    return f"{url}?{urlencode(query)}" if query else url


def _next_data(text: str) -> dict[str, Any]:
    """Read the `__NEXT_DATA__` blob off a page.

    Raises:
        NotAnEpisodePageError: The page carries none, or it is not JSON.
    """
    match = _NEXT_DATA.search(text)
    if match is None:
        msg = "no __NEXT_DATA__ on the page."
        raise NotAnEpisodePageError(msg)
    try:
        data = json.loads(match["json"])
    except ValueError as error:
        msg = "the __NEXT_DATA__ on the page is not JSON."
        raise NotAnEpisodePageError(msg) from error
    if not isinstance(data, dict):
        msg = "the __NEXT_DATA__ on the page is not an object."
        raise NotAnEpisodePageError(msg)
    return data


class BeLToon(Extractor):
    """Fetch episodes from BeLToon."""

    NAME = "beltoon"
    HOSTS = ("www.beltoon.jp",)
    URL_FORMS = (
        "https://www.beltoon.jp/viewer/<alias>/<episode>",
        "https://www.beltoon.jp/detail/<alias>",
    )
    CONFIG_KEY = "beltoon"
    HEADERS: ClassVar[dict[str, str]] = {**Extractor.HEADERS, "Referer": f"{BASE_URL}/"}

    def __init__(self, session: Session | None = None) -> None:
        """Build an extractor.

        Args:
            session: A session to reuse. A retrying one is made when omitted.
        """
        super().__init__(session)
        # The work API answers with every episode of a work; a bulk run asks
        # for the same work once per episode, so its answer is kept.
        self._works: dict[str, dict[str, Any] | None] = {}

    @classmethod
    def suitable(cls, url: str) -> bool:
        """Report whether the extractor takes `url`.

        Args:
            url: The URL to check.

        Returns:
            True for a viewer or a work page on www.beltoon.jp.
        """
        if not super().suitable(url):
            return False
        path = urlparse(url).path
        return _VIEWER_PATH.match(path) is not None or _DETAIL_PATH.match(path) is not None

    @classmethod
    def is_series(cls, url: str) -> bool:
        """Report whether `url` names a whole work rather than one episode.

        Args:
            url: The URL to check.

        Returns:
            True for `/detail/<alias>`.
        """
        return cls.suitable(url) and _DETAIL_PATH.match(urlparse(url).path) is not None

    def series_urls(self, url: str) -> list[str]:
        """List every episode of a work, first episode first.

        Args:
            url: A `/detail/<alias>` URL.

        Returns:
            The viewer URL of every episode, locked ones included.

        Raises:
            UnsupportedUrlError: The URL is no work page.
            NotAnEpisodePageError: The work does not exist or lists no episode.
        """
        match = _DETAIL_PATH.match(urlparse(url).path)
        if match is None:
            msg = f"{url} is not a work page."
            raise UnsupportedUrlError(msg)
        alias = match["alias"]
        work = self._work(alias)
        if work is None:
            msg = f"there is no work at {url}."
            raise NotAnEpisodePageError(msg)
        urls: list[str] = []
        for entry in _episodes(work):
            link = episode_url(alias, str(entry["alias"]))
            if link not in urls:
                urls.append(link)
        if not urls:
            msg = f"the work at {url} lists no episode."
            raise NotAnEpisodePageError(msg)
        return urls

    def episode(self, url: str) -> Episode:
        """Read one episode off its viewer page.

        Args:
            url: A `/viewer/<alias>/<episode>` URL.

        Returns:
            The episode. `pages` is empty when it wants a purchase or a login.

        Raises:
            UnsupportedUrlError: The URL is no viewer page.
            NotAnEpisodePageError: There is no such episode, or it is an e-book rather than pages.
        """
        parsed = urlparse(url)
        match = _VIEWER_PATH.match(parsed.path)
        if match is None:
            msg = f"{url} is not a viewer url."
            raise UnsupportedUrlError(msg)
        alias, episode_alias = match["alias"], match["episode"]
        query = {key: values[0] for key, values in parse_qs(parsed.query).items() if key in _VIEWER_QUERY}
        url = episode_url(alias, episode_alias, query)

        # The site's age gate sets this and the server reads it back; without
        # it an adult-flagged work answers `ADULT_ONLY_CONTENTS` to everyone.
        self._session.cookies.set("not-login-adult", "Y", domain=urlparse(BASE_URL).hostname, path="/")
        page_props = _next_data(self._get(url).text).get("props", {}).get("pageProps", {})
        answer = page_props.get("episodeData") or {}
        result, error = answer.get("result"), answer.get("error")

        work = self._work(alias)
        entries = _episodes(work) if work is not None else []
        entry = next((e for e in entries if str(e.get("alias")) == episode_alias), None)
        next_url = self._next_url(alias, entries, episode_alias)

        if not isinstance(result, dict):
            code = str((error or {}).get("code") or "")
            if not code or code in _NOT_AN_EPISODE:
                msg = f"no episode at {url} ({code or 'no viewer data'})."
                raise NotAnEpisodePageError(msg)
            # `NOT_LOGIN_USER`, `UNAUTHORIZED_CONTENTS`, `ADULT_ONLY_CONTENTS`: locked either way.
            return Episode(
                url=url,
                series_title=str((work or {}).get("title") or alias),
                episode_title=str((entry or {}).get("title") or episode_alias),
                next_url=next_url,
                metadata={"alias": alias, "episode_alias": episode_alias, "error": error, "episode": entry},
            )

        if result.get("contentType") != "IMAGE":
            msg = f"{url} is a {result.get('contentType') or 'non-image'} episode, not pages."
            raise NotAnEpisodePageError(msg)
        images = [image for image in result.get("images") or [] if image.get("imagePath")]
        images.sort(key=lambda image: image.get("order") or 0)
        indices = self._scramble_indices(result, images)
        pages = tuple(
            Page(
                url=str(image["imagePath"]),
                width=int(image.get("width") or 0),
                height=int(image.get("defaultHeight") or image.get("height") or 0),
                extra={"scramble": index},
            )
            for image, index in zip(images, indices, strict=True)
        )
        return Episode(
            url=url,
            series_title=str(result.get("contentsTitle") or (work or {}).get("title") or alias),
            episode_title=str(result.get("title") or (entry or {}).get("title") or episode_alias),
            pages=pages,
            next_url=next_url,
            metadata={key: value for key, value in result.items() if key != "images"},
        )

    def image(self, page: Page, episode: Episode) -> Image.Image:
        """Fetch one page and unscramble it when the site shuffled it.

        Args:
            page: The page to fetch.
            episode: The episode the page belongs to.

        Returns:
            The page image.
        """
        image = self._fetch_image(page.url, headers={**self.HEADERS, "Referer": episode.url})
        index = page.extra.get("scramble") or []
        return descramble(image, [int(slot) for slot in index]) if index else image

    def login(self, url: str, username: str, password: str) -> None:  # noqa: ARG002 (one sign-in endpoint)
        """Sign in with an email account, so episodes the account owns become readable.

        The site is a next-auth app: the credentials go to its `EmailLogin`
        provider with the CSRF token and an HMAC the sign-in form computes,
        and the session cookie lands on the shared session, which the viewer
        page is rendered against.

        Args:
            url: Ignored; BeLToon has one sign-in endpoint.
            username: The email address the account uses.
            password: The account's password.

        Raises:
            LoginError: BeLToon refused the credentials.
        """
        csrf = self._get(f"{AUTH_URL}/csrf", headers={**self.HEADERS, **_API_HEADERS}).json().get("csrfToken")
        if not csrf:
            msg = f"{AUTH_URL}/csrf handed out no CSRF token."
            raise LoginError(msg)
        signature = hmac.new(EMAIL_LOGIN_SECRET.encode(), (username + password).encode(), hashlib.sha1).hexdigest()
        res = self._session.post(
            f"{AUTH_URL}/callback/EmailLogin",
            data={
                "email": username,
                "password": password,
                "savedEmail": "false",
                "isAutoLogin": "false",
                "platform": "WEB",
                "loginPlatform": "WEB",
                "signInType": "email",
                "callbackUrl": f"{BASE_URL}/callback/login-success?callback=%2F",
                "qnwjdghldnjsrkdlq": signature,
                "csrfToken": str(csrf),
                "json": "true",
            },
            headers={**self.HEADERS, "Accept": "application/json", "Origin": BASE_URL},
            timeout=self.TIMEOUT,
        )
        try:
            answer = res.json()
        except ValueError:
            answer = None
        landing = str(answer.get("url") or "") if isinstance(answer, dict) else ""
        error = parse_qs(urlparse(landing).query).get("error")
        if not res.ok or not landing or error:
            reason = unquote(error[0]).split("&")[0] if error else f"HTTP {res.status_code}"
            msg = f"www.beltoon.jp refused the credentials for {username!r}: {reason}."
            raise LoginError(msg)

    def _work(self, alias: str) -> dict[str, Any] | None:
        """The work API's answer for `alias`, kept once fetched; None when there is no such work."""
        if alias not in self._works:
            res = self._get(
                f"{API_URL}/contents/{alias}",
                headers={**self.HEADERS, **_API_HEADERS},
                params={"isNotLoginAdult": "true", "isPorch": "false"},
            )
            answer = res.json()
            data = answer.get("data") if answer.get("result") == "SUCCESS" else None
            self._works[alias] = data if isinstance(data, dict) else None
        return self._works[alias]

    @staticmethod
    def _next_url(alias: str, entries: list[dict[str, Any]], episode_alias: str) -> str | None:
        aliases = [str(entry.get("alias")) for entry in entries]
        try:
            position = aliases.index(episode_alias)
        except ValueError:
            return None
        return episode_url(alias, aliases[position + 1]) if position + 1 < len(aliases) else None

    def _scramble_indices(self, result: dict[str, Any], images: list[dict[str, Any]]) -> list[list[int]]:
        """The tile permutation of every page, empty for a page served straight."""
        if not result.get("isScramble") or not images:
            return [[] for _ in images]
        line = images[0].get("line")
        key = self._scramble_key(result, str(line)) if line else None
        indices: list[list[int]] = []
        for image in images:
            point = image.get("point")
            if key is not None and point:
                indices.append(decrypt_index(str(point), key))
            else:
                indices.append([int(slot) for slot in image.get("scrambleIndex") or []])
        return indices

    def _scramble_key(self, result: dict[str, Any], line: str) -> str:
        """Ask the site for the key of an episode's `line`, falling back like the browser does."""
        res = self._session.post(
            f"{API_URL}/contents/images/{result.get('contentId')}/{result.get('episodeId')}",
            json={"line": line},
            headers={**self.HEADERS, **_API_HEADERS},
            timeout=self.TIMEOUT,
        )
        try:
            answer = res.json() if res.ok else {}
        except ValueError:
            answer = {}
        key = answer.get("data") if answer.get("result") == "SUCCESS" else None
        return str(key) if key else FALLBACK_SCRAMBLE_KEY


def _episodes(work: dict[str, Any]) -> list[dict[str, Any]]:
    """The episode rows of a work, in reading order."""
    entries = [entry for entry in work.get("episodes") or [] if isinstance(entry, dict) and entry.get("alias")]
    entries.sort(key=lambda entry: int(entry.get("orderNo") or 0))
    return entries
