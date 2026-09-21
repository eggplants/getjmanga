"""マンガノ (Hatena, for Shueisha): a Next.js app over a GraphQL API that wants a Firebase token.

A work page at `/works/<id>` and an episode page at `/episodes/<id>` are
both built client-side: the HTML carries only the titles, and the viewer
asks `https://manga-no.com/query` (GraphQL, `node(id:)` on a global id)
for the rest. Every call needs `Authorization: Bearer <Firebase id token>`,
which the site gets by signing every visitor in anonymously through
Firebase Auth (`accounts:signUp` with no credentials), so the extractor
does the same and keeps the token on the instance, refreshing it through
`securetoken.googleapis.com` once it expires.

An episode's `pages(first:, after:)` connection lists one image per page in
reading order, as a "scissors" template URL (Hatena's image proxy) with
`{width}`/`{height}` placeholders and the source
`https://img.manga-no.com/EPISODE_PAGE/<uuid>` URL-encoded at its end. The
source is a plain public file, so that is what gets downloaded, the proxy
rendering at the page's own size standing in when the source answers an
error. No scrambling, no Referer or cookie check.

A paid episode lists only its free preview pages and says
`canViewerSkipPaywall: false`; the extractor treats it as locked. A fan
wall (`canViewerSkipFanwall`) is an interstitial the viewer lets anyone
click through, so it gates nothing. Sign-in is Firebase's email + password
flow (`accounts:signInWithPassword`); X (Twitter) accounts cannot sign in
this way.
"""

from __future__ import annotations

import re
import time
from http import HTTPStatus
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urlparse

from httpx import HTTPStatusError

from getjmanga.errors import GetjmangaError, LoginError, NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractor import Episode, Extractor, Page, numbered, published_on

if TYPE_CHECKING:
    from collections.abc import Mapping

    from httpx import Client, Response
    from PIL import Image

BASE_URL = "https://manga-no.com"
#: The GraphQL endpoint every page talks to.
API_URL = f"{BASE_URL}/query"
#: The site's Firebase web API key, as its bundle carries it.
FIREBASE_API_KEY = "AIzaSyASnOvvLWrECQKNRI0R_82droxO1QMd4O8"  # a public client key, not a secret
_SIGN_UP_URL = "https://identitytoolkit.googleapis.com/v1/accounts:signUp"
_SIGN_IN_URL = "https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword"
_REFRESH_URL = "https://securetoken.googleapis.com/v1/token"
#: Seconds taken off a token's lifetime, so it is renewed before it runs out.
_TOKEN_MARGIN = 60
#: Where the page images really are; the proxy template has this URL-encoded at its end.
_IMAGE_ORIGIN = "https://img.manga-no.com/"

_EPISODE_PATH = re.compile(r"^/episodes/(?P<id>[0-9a-f]+)/?$")
_WORK_PATH = re.compile(r"^/works/(?P<id>[0-9a-f]+)/?$")

#: How many pages / episodes one connection page asks for, as the viewer does.
PAGE_SIZE = 200
EPISODES_PAGE_SIZE = 100

EPISODE_QUERY = """
query GetEpisode($id: ID!, $first: Int!, $after: String) {
  node(id: $id) {
    __typename
    id
    ... on Episode {
      title
      number
      publicNumber
      publishedAt
      status
      startPosition
      afterword
      canViewerSkipPaywall
      canViewerSkipFanwall
      purchasedByViewer
      salesInfo { price pagesChargedFrom salesAppeal }
      work {
        id
        title
        scrollDirection
        user { id screenName displayName }
      }
      previousEpisode { id }
      nextEpisode { id }
      pages(first: $first, after: $after) {
        totalCount
        viewableCount
        edges { node { id width height image { id url } } }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""

WORK_QUERY = """
query GetWorkDetailAndEpisodes($id: ID!, $first: Int!, $after: String) {
  node(id: $id) {
    __typename
    id
    ... on Work {
      title
      episodes(first: $first, after: $after) {
        totalCount
        edges { node { id title number status } }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""


def episode_url(episode_id: str) -> str:
    """The episode page for a global id.

    Args:
        episode_id: The episode's id, as the API names it.

    Returns:
        `https://manga-no.com/episodes/<id>`.
    """
    return f"{BASE_URL}/episodes/{episode_id}"


def render_template(template: str, width: int, height: int) -> str:
    """Fill a scissors template URL in, the way the viewer does.

    Args:
        template: An image URL with `{width}` and `{height}` placeholders.
        width: The width to ask the proxy for.
        height: The height to ask the proxy for.

    Returns:
        The URL the proxy serves the image at.
    """
    return template.replace("{width}", str(width)).replace("{height}", str(height))


def source_url(template: str) -> str | None:
    """The plain image file a scissors template URL wraps.

    Args:
        template: An image URL from the API.

    Returns:
        The `https://img.manga-no.com/...` URL encoded at its end, or None
        when the template does not end in one.
    """
    source = unquote(template.rsplit("/", 1)[-1])
    return source if source.startswith(_IMAGE_ORIGIN) else None


class MangaNo(Extractor):
    """Fetch episodes from マンガノ."""

    NAME = "mangano"
    HOSTS = ("manga-no.com",)
    PUBLISHER = "はてな"
    URL_FORMS = (
        "https://manga-no.com/episodes/<id>",
        "https://manga-no.com/works/<id>",
    )
    CONFIG_KEY = "mangano"

    def __init__(self, session: Client | None = None) -> None:
        """Build an extractor.

        Args:
            session: A session to reuse. A retrying one is made when omitted.
        """
        super().__init__(session)
        #: The Firebase id token the API wants, and when to stop trusting it.
        self._id_token: str | None = None
        self._refresh_token: str | None = None
        self._token_expires_at = 0.0

    @classmethod
    def suitable(cls, url: str) -> bool:
        """Report whether the extractor takes `url`.

        Args:
            url: The URL to check.

        Returns:
            True for an https `/episodes/<id>` or `/works/<id>` URL on the site.
        """
        if not super().suitable(url):
            return False
        path = urlparse(url).path
        return bool(_EPISODE_PATH.match(path) or _WORK_PATH.match(path))

    @classmethod
    def is_series(cls, url: str) -> bool:
        """Report whether `url` is a work page rather than an episode.

        Args:
            url: The URL to check.

        Returns:
            True for `/works/<id>`.
        """
        return _WORK_PATH.match(urlparse(url).path) is not None

    def series_urls(self, url: str) -> list[str]:
        """List every episode of a work, oldest first.

        Args:
            url: A work page URL.

        Returns:
            One episode URL per listed episode, in the work's own order.

        Raises:
            UnsupportedUrlError: The URL is not a work page.
            NotAnEpisodePageError: The work does not exist, or lists no episode.
        """
        match = _WORK_PATH.match(urlparse(url).path)
        if match is None:
            msg = f"{url} is not a work page."
            raise UnsupportedUrlError(msg)
        work_id = match.group("id")
        urls: list[str] = []
        seen: set[str] = set()
        after: str | None = None
        while True:
            node = self._node(
                "GetWorkDetailAndEpisodes",
                WORK_QUERY,
                {"id": work_id, "first": EPISODES_PAGE_SIZE, "after": after},
                url,
            )
            if node.get("__typename") != "Work":
                msg = f"{url} is not a work page (the API calls it {node.get('__typename')!r})."
                raise NotAnEpisodePageError(msg)
            connection = node.get("episodes") or {}
            for edge in connection.get("edges") or []:
                episode_id = str((edge.get("node") or {}).get("id") or "")
                if episode_id and episode_id not in seen:
                    seen.add(episode_id)
                    urls.append(episode_url(episode_id))
            page_info = connection.get("pageInfo") or {}
            after = page_info.get("endCursor")
            if not page_info.get("hasNextPage") or not after:
                break
        if not urls:
            msg = f"the work at {url} lists no episode."
            raise NotAnEpisodePageError(msg)
        return urls

    def episode(self, url: str) -> Episode:
        """Read one episode: its titles, its pages and what follows it.

        Args:
            url: The episode URL.

        Returns:
            The episode. `pages` is empty for a paid episode the account has
            not bought, which the API lists only the free preview of; the
            next episode is still named.

        Raises:
            NotAnEpisodePageError: The URL is not an episode page, or the
                episode does not exist.
        """
        match = _EPISODE_PATH.match(urlparse(url).path)
        if match is None:
            msg = f"{url} is not an episode page."
            raise NotAnEpisodePageError(msg)
        episode_id = match.group("id")

        node: dict[str, Any] = {}
        edges: list[dict[str, Any]] = []
        after: str | None = None
        while True:
            node = self._node("GetEpisode", EPISODE_QUERY, {"id": episode_id, "first": PAGE_SIZE, "after": after}, url)
            if node.get("__typename") != "Episode":
                msg = f"{url} is not an episode (the API calls it {node.get('__typename')!r})."
                raise NotAnEpisodePageError(msg)
            connection = node.get("pages") or {}
            edges.extend(edge.get("node") or {} for edge in connection.get("edges") or [])
            page_info = connection.get("pageInfo") or {}
            after = page_info.get("endCursor")
            if not page_info.get("hasNextPage") or not after:
                break

        work = node.get("work") or {}
        locked = not node.get("canViewerSkipPaywall", True)
        pages = () if locked else tuple(_page(page_node) for page_node in edges if _template(page_node))
        next_episode = node.get("nextEpisode") or {}
        previous_episode = node.get("previousEpisode") or {}
        number = node.get("publicNumber") or node.get("number")
        return Episode(
            url=episode_url(episode_id),
            series_title=str(work.get("title") or ""),
            episode_title=str(node.get("title") or (f"第{number}話" if number else episode_id)),
            pages=pages,
            prev_url=episode_url(str(previous_episode["id"])) if previous_episode.get("id") else None,
            next_url=episode_url(str(next_episode["id"])) if next_episode.get("id") else None,
            metadata={
                "id": episode_id,
                "title": node.get("title"),
                "number": node.get("number"),
                "public_number": node.get("publicNumber"),
                "status": node.get("status"),
                "start_position": node.get("startPosition"),
                "scroll_direction": work.get("scrollDirection"),
                "afterword": node.get("afterword"),
                "author": (work.get("user") or {}).get("displayName"),
                "author_screen_name": (work.get("user") or {}).get("screenName"),
                "work_id": work.get("id"),
                "work_url": f"{BASE_URL}/works/{work['id']}" if work.get("id") else None,
                "prev_url": episode_url(str(previous_episode["id"])) if previous_episode.get("id") else None,
                "locked": locked,
                "purchased": node.get("purchasedByViewer"),
                "sales_info": node.get("salesInfo"),
                "page_count": (node.get("pages") or {}).get("totalCount"),
                "viewable_count": (node.get("pages") or {}).get("viewableCount"),
                "images": [_template(page_node) for page_node in edges],
            },
            writer=str((work.get("user") or {}).get("displayName") or ""),
            publisher=self.PUBLISHER,
            published=published_on(node.get("publishedAt")),
            number=numbered(node.get("publicNumber") or node.get("number")),
        )

    def image(self, page: Page, episode: Episode) -> Image.Image:
        """Fetch one page: the plain source file, or the proxy's rendering of it.

        Args:
            page: The page to fetch.
            episode: The episode the page belongs to.

        Returns:
            The page image.
        """
        headers = {**self.HEADERS, "Referer": episode.url}
        template = str(page.extra.get("template") or "")
        fallback = render_template(template, page.width, page.height) if template else ""
        if not fallback or fallback == page.url:
            return self._fetch_image(page.url, headers=headers)
        try:
            return self._fetch_image(page.url, headers=headers)
        except HTTPStatusError:
            return self._fetch_image(fallback, headers=headers)

    def login(self, url: str, username: str, password: str) -> None:  # noqa: ARG002 (one Firebase project for the site)
        """Sign in with an email address and password through Firebase Auth.

        Args:
            url: Any URL on the site.
            username: The account's email address.
            password: The account's password.

        Raises:
            LoginError: Firebase refused the credentials.
        """
        res = self._session.post(
            _SIGN_IN_URL,
            params={"key": FIREBASE_API_KEY},
            json={"email": username, "password": password, "returnSecureToken": True},
            headers={**self.HEADERS, "Referer": f"{BASE_URL}/"},
            timeout=self.TIMEOUT,
        )
        body = _json_or_none(res)
        if res.status_code != HTTPStatus.OK or not isinstance(body, dict) or not body.get("idToken"):
            reason = ""
            if isinstance(body, dict):
                reason = str((body.get("error") or {}).get("message") or "")
            msg = f"{BASE_URL} refused the credentials for {username!r}" + (f": {reason}" if reason else ".")
            raise LoginError(msg)
        self._store_token(body)

    def _node(self, operation: str, query: str, variables: Mapping[str, Any], url: str) -> dict[str, Any]:
        """Run one `node(id:)` query and hand its node back.

        Args:
            operation: The operation name, sent as `?opname=` the way the viewer does.
            query: The GraphQL document.
            variables: Its variables.
            url: The page being read, for the Referer and for error messages.

        Returns:
            `data.node` of the answer.

        Raises:
            NotAnEpisodePageError: The API knows no such id.
            GetjmangaError: The API answered with another error.
        """
        body = self._query(operation, query, variables, url)
        errors = body.get("errors") or []
        node = (body.get("data") or {}).get("node")
        if not isinstance(node, dict):
            codes = {
                str((error.get("extensions") or {}).get("code") or "") for error in errors if isinstance(error, dict)
            }
            if "NOT_FOUND" in codes or not errors:
                msg = f"{url}: the API knows no such page."
                raise NotAnEpisodePageError(msg)
            messages = "; ".join(str(error.get("message") or "") for error in errors if isinstance(error, dict))
            msg = f"{url}: the API answered {messages or codes}."
            raise GetjmangaError(msg)
        return node

    def _query(self, operation: str, query: str, variables: Mapping[str, Any], url: str) -> dict[str, Any]:
        """POST a GraphQL operation with a valid token, renewing it once on a 401."""
        payload = {"operationName": operation, "variables": dict(variables), "query": query}
        for attempt in range(2):
            res = self._session.post(
                API_URL,
                params={"opname": operation},
                json=payload,
                headers={
                    **self.HEADERS,
                    "Authorization": f"Bearer {self._token()}",
                    "Origin": BASE_URL,
                    "Referer": url,
                },
                timeout=self.TIMEOUT,
            )
            if res.status_code == HTTPStatus.UNAUTHORIZED and attempt == 0:
                self._token_expires_at = 0.0
                continue
            res.raise_for_status()
            body = _json_or_none(res)
            if not isinstance(body, dict):
                msg = f"{API_URL} answered {operation} with something other than JSON."
                raise GetjmangaError(msg)
            return body
        msg = f"{API_URL} kept refusing the token for {operation}."  # pragma: no cover (the loop returns or raises)
        raise GetjmangaError(msg)

    def _token(self) -> str:
        """A Firebase id token: the account's, an anonymous one, or a renewal of either."""
        if self._id_token and time.monotonic() < self._token_expires_at:
            return self._id_token
        if self._refresh_token:
            res = self._session.post(
                _REFRESH_URL,
                params={"key": FIREBASE_API_KEY},
                json={"grant_type": "refresh_token", "refresh_token": self._refresh_token},
                headers=self.HEADERS,
                timeout=self.TIMEOUT,
            )
            body = _json_or_none(res)
            if res.status_code == HTTPStatus.OK and isinstance(body, dict) and body.get("id_token"):
                self._store_token(
                    {
                        "idToken": body["id_token"],
                        "refreshToken": body.get("refresh_token"),
                        "expiresIn": body.get("expires_in"),
                    }
                )
                return str(self._id_token)
        res = self._session.post(
            _SIGN_UP_URL,
            params={"key": FIREBASE_API_KEY},
            json={"returnSecureToken": True},
            headers={**self.HEADERS, "Referer": f"{BASE_URL}/"},
            timeout=self.TIMEOUT,
        )
        res.raise_for_status()
        body = _json_or_none(res)
        if not isinstance(body, dict) or not body.get("idToken"):
            msg = f"{_SIGN_UP_URL} handed no anonymous token over."
            raise GetjmangaError(msg)
        self._store_token(body)
        return str(self._id_token)

    def _store_token(self, body: Mapping[str, Any]) -> None:
        """Keep a token answer's id token, refresh token and lifetime."""
        self._id_token = str(body["idToken"])
        refresh = body.get("refreshToken")
        self._refresh_token = str(refresh) if refresh else self._refresh_token
        try:
            lifetime = int(body.get("expiresIn") or 3600)
        except ValueError:
            lifetime = 3600
        self._token_expires_at = time.monotonic() + max(lifetime - _TOKEN_MARGIN, 0)


def _template(page_node: Mapping[str, Any]) -> str:
    """The scissors template URL of a page node, or '' when it carries none."""
    return str((page_node.get("image") or {}).get("url") or "")


def _page(page_node: Mapping[str, Any]) -> Page:
    """A `Page` for one node of the pages connection: the source file, the template kept for `image()`."""
    template = _template(page_node)
    width = int(page_node.get("width") or 0)
    height = int(page_node.get("height") or 0)
    source = source_url(template)
    return Page(
        url=source or render_template(template, width, height),
        width=width,
        height=height,
        extra={"template": template, "id": page_node.get("id")},
    )


def _json_or_none(res: Response) -> Any:  # noqa: ANN401 (whatever the body decodes to)
    """`res.json()`, or None when the body is not JSON."""
    try:
        return res.json()
    except ValueError:
        return None
