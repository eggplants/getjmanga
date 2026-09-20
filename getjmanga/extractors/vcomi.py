"""Vコミ (Vスクロールコミックス): a SvelteKit app whose viewer decrypts every page with AES-CBC.

The site is a SvelteKit app talking to its own tRPC backend under `/trpc`,
with `devalue` as the wire format: every payload -- a page's server data,
a query input, a query result -- is a flattened array where the first entry
is the root and every object value or array element is an index into it.
`unflatten()` puts one back together.

An episode page's server data (`/episodes/<id>/__data.json`) names the
episode, its series and the episode after it; the viewer then asks
`episode.getPages` for the page list, which comes with a per-episode
`secret` (an AES-256 key) and a per-page `iv`, both base64url. The page
files sit on `images.vcomi.jp` unprotected but AES-CBC encrypted, which
`cipher.aes_cbc_decrypt()` puts back, the same as F comic's. A locked episode -- one past
the free run, or one the account does not own -- answers `getPages` with
no `episode`, only `readOptions` (the purchase offer). Sign-in is the
`user.login` mutation, which sets a session cookie; the site locks an
account after repeated failures, so `login()` is careful to post once.
"""

from __future__ import annotations

import base64
import json
import math
import re
from http import HTTPStatus
from io import BytesIO
from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import urlparse

from PIL import Image

from getjmanga.cipher import aes_cbc_decrypt
from getjmanga.errors import LoginError, NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractor import Episode, Extractor, Page

if TYPE_CHECKING:
    from httpx import Response

BASE_URL = "https://vcomi.jp"
#: `PUBLIC_S3_ENDPOINT`: where the page files are served from.
IMAGE_URL = "https://images.vcomi.jp"
TRPC_URL = f"{BASE_URL}/trpc"
#: `PUBLIC_PLATFORM`, sent as `x-platform` with every tRPC call.
PLATFORM = "web"

_EPISODE_PATH = re.compile(r"^/episodes/(?P<id>\d+)/?$")
_SERIES_PATH = re.compile(r"^/series/(?P<id>\d+)/?$")

# devalue's stand-ins for what JSON cannot say, as negative indices.
_UNDEFINED, _NAN, _POSITIVE_INFINITY, _NEGATIVE_INFINITY, _NEGATIVE_ZERO, _HOLE = -1, -2, -3, -4, -5, -6
_SPECIALS: dict[int, Any] = {
    _UNDEFINED: None,
    _NAN: math.nan,
    _POSITIVE_INFINITY: math.inf,
    _NEGATIVE_INFINITY: -math.inf,
    _NEGATIVE_ZERO: -0.0,
    _HOLE: None,
}


def unflatten(flat: Any) -> Any:  # noqa: ANN401 (whatever the site sent)
    """Put a `devalue` payload back together.

    `devalue.stringify()` writes a value as a flat array: entry 0 is the
    root, an object's values and an array's elements are indices into the
    array, and a few negative indices stand for what JSON has no word for
    (`undefined`, `NaN`, the infinities, `-0`, a hole). The site sends
    nothing but objects, arrays and scalars, so devalue's tagged entries
    (`["Date", ...]`, `["Set", ...]`) are not read: one comes back as the
    list it is.

    Args:
        flat: The parsed JSON payload, an array or a bare negative index.

    Returns:
        The value: dicts for objects, lists for arrays, None for `undefined`
        and holes.
    """
    if isinstance(flat, int) and not isinstance(flat, bool):
        return _SPECIALS.get(flat)
    if not isinstance(flat, list) or not flat:
        return None
    hydrated: dict[int, Any] = {}

    def hydrate(index: Any) -> Any:  # noqa: ANN401 (an index, or a raw value the site put in its place)
        if not isinstance(index, int) or isinstance(index, bool):
            return index
        if index < 0:
            return _SPECIALS.get(index)
        if index in hydrated:
            return hydrated[index]
        value = flat[index]
        # Containers are registered before their children, so a cycle resolves.
        if isinstance(value, dict):
            result: dict[str, Any] = {}
            hydrated[index] = result
            result.update({key: hydrate(child) for key, child in value.items()})
        elif isinstance(value, list):
            items: list[Any] = []
            hydrated[index] = items
            items.extend(hydrate(child) for child in value)
        else:
            hydrated[index] = value
        return hydrated[index]

    return hydrate(0)


def flatten(value: Any) -> str:  # noqa: ANN401 (a query input)
    """Write a value the way `devalue.stringify()` does, for a tRPC input.

    Only what a query input needs: strings, numbers, booleans, None,
    dicts and lists. A repeated value is written once and referenced.

    Args:
        value: The input.

    Returns:
        The flattened JSON text.
    """
    entries: list[str] = []
    seen: dict[int, int] = {}

    def visit(item: Any) -> int:  # noqa: ANN401 (any JSON value)
        if item is None:
            return _UNDEFINED
        if isinstance(item, float) and math.isnan(item):
            return _NAN
        key = id(item)
        if key in seen:
            return seen[key]
        index = len(entries)
        seen[key] = index
        entries.append("")
        if isinstance(item, dict):
            entries[index] = "{" + ",".join(f"{json.dumps(str(k))}:{visit(v)}" for k, v in item.items()) + "}"
        elif isinstance(item, (list, tuple)):
            entries[index] = "[" + ",".join(str(visit(child)) for child in item) + "]"
        else:
            entries[index] = json.dumps(item, ensure_ascii=False)
        return index

    root = visit(value)
    return str(root) if root < 0 else "[" + ",".join(entries) + "]"


def urlsafe_b64decode(text: str) -> bytes:
    """Decode base64url without padding, the way the viewer's `atob` wrapper does.

    Args:
        text: The base64url text.

    Returns:
        The bytes.
    """
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def episode_url(episode_id: int | str) -> str:
    """The canonical URL of an episode.

    Args:
        episode_id: The episode's numeric id.

    Returns:
        The episode URL.
    """
    return f"{BASE_URL}/episodes/{episode_id}"


def episode_title(entry: dict[str, Any]) -> str:
    """Name an episode the way the site's heading does.

    Args:
        entry: An episode as the site describes it.

    Returns:
        `prefix` (`1話`) and `title`, joined by a space; whichever is set.
    """
    prefix = str(entry.get("prefix") or "")
    title = str(entry.get("title") or "")
    return " ".join(part for part in (prefix, title) if part) or str(entry.get("id") or "")


class Vcomi(Extractor):
    """Fetch episodes from Vコミ."""

    NAME = "vcomi"
    HOSTS = ("vcomi.jp",)
    URL_FORMS = (
        "https://vcomi.jp/episodes/<id>",
        "https://vcomi.jp/series/<id>",
    )
    CONFIG_KEY = "vcomi"
    HEADERS: ClassVar[dict[str, str]] = {
        **Extractor.HEADERS,
        "sec-ch-ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Linux"',
    }

    @classmethod
    def suitable(cls, url: str) -> bool:
        """Report whether the extractor takes `url`.

        Args:
            url: The URL to check.

        Returns:
            True for an episode or a series page on the known host.
        """
        if not super().suitable(url):
            return False
        path = urlparse(url).path
        return _EPISODE_PATH.match(path) is not None or _SERIES_PATH.match(path) is not None

    @classmethod
    def is_series(cls, url: str) -> bool:
        """Report whether `url` names a whole series rather than one episode.

        Args:
            url: The URL to check.

        Returns:
            True for `/series/<id>`.
        """
        return _SERIES_PATH.match(urlparse(url).path) is not None

    def series_urls(self, url: str) -> list[str]:
        """List every episode of a series, first episode first.

        Args:
            url: A series URL.

        Returns:
            One episode URL per listed episode, in the order the site lists them.

        Raises:
            UnsupportedUrlError: The URL is not a series page.
            NotAnEpisodePageError: The series lists no episode, or does not exist.
        """
        match = _SERIES_PATH.match(urlparse(url).path)
        if match is None:
            msg = f"{url} is not a series page."
            raise UnsupportedUrlError(msg)
        data = self._page_data(f"{BASE_URL}/series/{match['id']}", "series")
        series = _dict(data.get("series"))
        urls: list[str] = []
        for entry in series.get("episodes") or []:
            if isinstance(entry, dict) and entry.get("id") is not None:
                candidate = episode_url(entry["id"])
                if candidate not in urls:
                    urls.append(candidate)
        if not urls:
            msg = f"the series at {url} lists no episode."
            raise NotAnEpisodePageError(msg)
        return urls

    def episode(self, url: str) -> Episode:
        """Read one episode and list its pages.

        The page's server data names the episode and the one after it; the
        `episode.getPages` query hands the page files over with their
        decryption key, or only `readOptions` when the episode is locked.

        Args:
            url: The episode URL.

        Returns:
            The episode. `pages` is empty when it is not readable.

        Raises:
            UnsupportedUrlError: The URL is not an episode URL.
            NotAnEpisodePageError: The site knows no such episode.
        """
        match = _EPISODE_PATH.match(urlparse(url).path)
        if match is None:
            msg = f"{url} is not an episode page."
            raise UnsupportedUrlError(msg)
        episode_id = int(match["id"])
        canonical = episode_url(episode_id)

        data = self._page_data(canonical, "episode")
        entry = _dict(data.get("episode"))
        if not entry:
            msg = f"no episode on {canonical}."
            raise NotAnEpisodePageError(msg)
        series = _dict(entry.get("series"))

        viewer = self._query("episode.getPages", episode_id, referer=canonical)
        readable = _dict(viewer.get("episode"))
        secret = str(readable.get("secret") or "")
        pages: list[Page] = []
        for item in readable.get("pages") or []:
            image = _dict(_dict(item).get("image"))
            if not image.get("path"):
                continue
            pages.append(
                Page(
                    url=f"{IMAGE_URL}/{image['path']}",
                    extra={"iv": str(image.get("iv") or ""), "secret": secret},
                )
            )

        following = _dict(data.get("nextEpisode"))
        next_url = episode_url(following["id"]) if following.get("id") is not None else None

        return Episode(
            url=canonical,
            series_title=str(series.get("title") or ""),
            episode_title=episode_title(entry),
            pages=tuple(pages),
            next_url=next_url,
            metadata={
                "episode": entry,
                "nextEpisode": following or None,
                "prevEpisode": data.get("prevEpisode"),
                "viewer": viewer,
            },
        )

    def image(self, page: Page, episode: Episode) -> Image.Image:
        """Fetch one page file and decrypt it.

        Args:
            page: The page to fetch.
            episode: The episode the page belongs to.

        Returns:
            The page image.
        """
        res = self._get(page.url, headers={**self.HEADERS, "Referer": episode.url}, timeout=self.IMAGE_TIMEOUT)
        data = res.content
        iv, secret = str(page.extra.get("iv") or ""), str(page.extra.get("secret") or "")
        if iv and secret:
            data = aes_cbc_decrypt(data, urlsafe_b64decode(secret).hex(), urlsafe_b64decode(iv).hex())
        return Image.open(BytesIO(data))

    def login(self, url: str, username: str, password: str) -> None:
        """Sign in with the `user.login` mutation, which sets the session cookie.

        The site locks an account for a while after several failed attempts
        in a row, so the credentials are posted exactly once.

        Args:
            url: Any URL on the site to sign in to.
            username: The account's email address.
            password: The account's password.

        Raises:
            LoginError: The site refused the credentials, or locked the account.
        """
        origin = self._origin(url) if urlparse(url).hostname in self.HOSTS else BASE_URL
        res = self._session.post(
            f"{origin}/trpc/user.login",
            params={"batch": "1"},
            json={"0": flatten({"email": username, "password": password})},
            headers={**self.HEADERS, **self._trpc_headers(f"{origin}/login"), "Origin": origin},
            timeout=self.TIMEOUT,
        )
        if res.status_code in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN):
            msg = f"{origin} refused the sign-in request for {username!r} (HTTP {res.status_code})."
            raise LoginError(msg)
        answer = self._trpc_result(res)
        if not isinstance(answer, dict):
            msg = f"{origin} answered the sign-in with something unexpected: {answer!r}."
            raise LoginError(msg)
        error = answer.get("error")
        if error:
            duration = answer.get("duration")
            detail = f" for {duration} more seconds" if duration is not None else ""
            msg = f"{origin} refused the credentials for {username!r}: {error}{detail}."
            raise LoginError(msg)

    def _page_data(self, page_url: str, key: str) -> dict[str, Any]:
        """The server data of a page, as its `__data.json` says it.

        Args:
            page_url: The canonical page URL, without a trailing slash.
            key: A key the page's own data node carries (`episode`, `series`).

        Returns:
            The route's data node, unflattened.

        Raises:
            NotAnEpisodePageError: The site answers with an error node (its 404).
        """
        res = self._session.get(
            f"{page_url}/__data.json",
            headers={**self.HEADERS, "Accept": "application/json", "Referer": page_url},
            timeout=self.TIMEOUT,
        )
        if res.status_code == HTTPStatus.NOT_FOUND:
            msg = f"nothing at {page_url}."
            raise NotAnEpisodePageError(msg)
        res.raise_for_status()
        body = _json_or_none(res)
        nodes = body.get("nodes") if isinstance(body, dict) else None
        for node in nodes or []:
            if not isinstance(node, dict):
                continue
            if node.get("type") == "error":
                msg = f"nothing at {page_url}: {(node.get('error') or {}).get('message', node.get('status'))}."
                raise NotAnEpisodePageError(msg)
            data = unflatten(node.get("data")) if node.get("type") == "data" else None
            if isinstance(data, dict) and key in data:
                return data
        msg = f"no {key} in the data of {page_url}."
        raise NotAnEpisodePageError(msg)

    def _query(self, procedure: str, value: Any, *, referer: str) -> dict[str, Any]:  # noqa: ANN401 (the input)
        """Call a tRPC query.

        Args:
            procedure: The procedure, `episode.getPages`.
            value: Its input.
            referer: The page the viewer would call it from.

        Returns:
            The result, unflattened.

        Raises:
            NotAnEpisodePageError: The procedure said the episode does not exist.
        """
        res = self._session.get(
            f"{TRPC_URL}/{procedure}",
            params={"batch": "1", "input": json.dumps({"0": flatten(value)})},
            headers={**self.HEADERS, **self._trpc_headers(referer)},
            timeout=self.TIMEOUT,
        )
        if res.status_code == HTTPStatus.NOT_FOUND:
            msg = f"{procedure} knows no {value!r} ({(self._trpc_error(res) or {}).get('message', 'not found')})."
            raise NotAnEpisodePageError(msg)
        res.raise_for_status()
        result = self._trpc_result(res)
        return result if isinstance(result, dict) else {}

    @staticmethod
    def _trpc_headers(referer: str) -> dict[str, str]:
        """What the tRPC client sends with every call."""
        return {
            "Accept": "*/*",
            "Content-Type": "application/json",
            "Referer": referer,
            "x-platform": PLATFORM,
        }

    @staticmethod
    def _trpc_result(res: Response) -> Any:  # noqa: ANN401 (whatever the procedure returned)
        """The first result of a batched tRPC answer, unflattened."""
        body = _json_or_none(res)
        first = body[0] if isinstance(body, list) and body else body
        if not isinstance(first, dict) or not isinstance(first.get("result"), dict):
            return None
        return unflatten(_loads(first["result"].get("data")))

    @staticmethod
    def _trpc_error(res: Response) -> dict[str, Any] | None:
        """The first error of a batched tRPC answer, unflattened."""
        body = _json_or_none(res)
        first = body[0] if isinstance(body, list) and body else body
        if not isinstance(first, dict) or "error" not in first:
            return None
        error = unflatten(_loads(first["error"]))
        return error if isinstance(error, dict) else None


def _dict(value: Any) -> dict[str, Any]:  # noqa: ANN401 (whatever the site put there)
    """`value` when it is an object, else an empty dict, so lookups chain without checks."""
    return value if isinstance(value, dict) else {}


def _loads(payload: Any) -> Any:  # noqa: ANN401 (a devalue payload, as text or already parsed)
    """A tRPC payload: the transformer writes it as JSON text inside the JSON envelope."""
    if isinstance(payload, str):
        try:
            return json.loads(payload)
        except ValueError:
            return None
    return payload


def _json_or_none(res: Response) -> Any:  # noqa: ANN401 (whatever JSON the site sent)
    """`res.json()`, or None when the body is not JSON."""
    try:
        return res.json()
    except ValueError:
        return None
