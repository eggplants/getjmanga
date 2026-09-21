"""Sites running the Comici+ viewer: publishers' own domains on Comici's SaaS."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from http import HTTPStatus
from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree as ET

from bs4 import BeautifulSoup
from bs4.element import Tag

from getjmanga.errors import GetjmangaError, LoginError, NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractor import Episode, Extractor, Page, published_on

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date

    from httpx2 import Client
    from PIL import Image

_DOCUMENT_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}
_API_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}
_IMAGE_HEADERS = {
    "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
    "Sec-Fetch-Dest": "image",
    "Sec-Fetch-Mode": "no-cors",
    "Sec-Fetch-Site": "cross-site",
}

#: The viewer slices every page into a COLUMNS x ROWS grid and shuffles the tiles.
COLUMNS = 4
ROWS = 4
TILES = COLUMNS * ROWS

_VIEWER_ID = "comici-viewer"

# Sites that serve several imprints off one domain (rimacomiplus.jp,
# comics.comici.jp) put the imprint in front: `<host>/<imprint>/series/<id>`.
_PREFIX = r"^(?P<prefix>(?:/[^/]+)*?)"

# A series feed, `<host>/series/<id>/rss`, listing the episodes newest first.
_RSS_PATH = re.compile(_PREFIX + r"/series/[^/]+/rss/?$")

# A series page: `<host>/series/<id>` on its own, its `/new` tab, or one `/<n>`
# page of the episode list. The list is paginated and the feed only carries the
# most recent episodes, so a whole series is read by walking `/1` upwards.
_SERIES_PATH = re.compile(_PREFIX + r"/series/(?P<id>[^/]+)(?:/(?:new|[0-9]+))?/?$")

# An episode link as a series page writes it, `<host>/episodes/<id>`.
_EPISODE_PATH = re.compile(_PREFIX + r"/episodes/[^/]+/?$")

# Sites that hand their images over unscrambled still go through `descramble`,
# with a permutation that puts every tile back where it already was.
_IDENTITY_SCRAMBLE = json.dumps(list(range(TILES)))


@dataclass(frozen=True)
class Viewer:
    """What an episode page says about its viewer."""

    url: str
    viewer_id: str
    api_base: str
    series_title: str
    episode_title: str
    next_url: str | None
    prev_url: str | None = None
    member_jwt: str = ""
    #: The series header's credits, `名前 (役割)` each.
    writer: str = ""
    #: The day the episode came out, off the page's own header.
    published: date | None = None
    # `data-content-id`, which sites that serve several imprints off one domain
    # (rimacomiplus.jp) set. `contentsInfo` answers `bad contentId` without it;
    # sites that leave the attribute off reject the parameter, so it is only
    # ever sent when the page carried one.
    content_id: str = ""
    # Pages the episode JSON carried itself, on sites that render the viewer
    # client-side. None when the page had a viewer and `contentsInfo` has to
    # be asked for them.
    inline_pages: tuple[dict[str, Any], ...] | None = None


def _credits(soup: BeautifulSoup) -> str:
    """The series header's `.g-author` entries: a name, and its `(役割)` when the site gives one."""
    credited = []
    for author in soup.select(".series-h-credit-user .g-author"):
        name = author.select_one(".g-author-name")
        role = author.select_one(".g-author-role")
        name_text = name.get_text(strip=True) if isinstance(name, Tag) else ""
        role_text = role.get_text(strip=True).strip("()") if isinstance(role, Tag) else ""
        if name_text:
            credited.append(f"{name_text} ({role_text})" if role_text else name_text)
    return ", ".join(credited)


def parse_scramble(scramble: str) -> list[int]:
    """Turn the API's `"[1, 5, 13, ...]"` into a list of tile indices.

    Args:
        scramble: The `scramble` field of a page, as the API returns it.

    Returns:
        One source tile index per destination tile, in column-major order.

    Raises:
        GetjmangaError: The string is not a permutation of `range(TILES)`.
    """
    indices = [int(part) for part in scramble.strip().strip("[]").split(",")]
    if sorted(indices) != list(range(TILES)):
        msg = f"{scramble!r} is not a permutation of 0..{TILES - 1}."
        raise GetjmangaError(msg)
    return indices


def descramble(image: Image.Image, scramble: Sequence[int]) -> Image.Image:
    """Put a scrambled page back together.

    The viewer walks the destination grid column by column and copies
    `scramble[n]`-th source tile into the n-th destination slot. Tile size is
    floored, so any leftover strip on the right and bottom edge is never
    shuffled and is kept as-is.

    Args:
        image: The page exactly as the CDN serves it.
        scramble: Source tile index per destination tile, from `parse_scramble`.

    Returns:
        A new image with the tiles back in reading order.
    """
    width, height = image.size
    tile_width, tile_height = width // COLUMNS, height // ROWS
    out = image.copy()
    for dest, src in enumerate(scramble):
        dest_col, dest_row = divmod(dest, ROWS)
        src_col, src_row = divmod(src, ROWS)
        tile = image.crop(
            (
                tile_width * src_col,
                tile_height * src_row,
                tile_width * (src_col + 1),
                tile_height * (src_row + 1),
            ),
        )
        out.paste(tile, (tile_width * dest_col, tile_height * dest_row))
    return out


class Comici(Extractor):
    """Fetch episodes from a site running the Comici+ viewer.

    `HOSTS` is a list of the sites known to run it, not a gate: any page with
    a Comici+ viewer on it works, so an unlisted site can be read by forcing
    the extractor with `--extractor comici`.
    """

    NAME = "comici"
    # https://comici.jp/cooperation-site
    HOSTS = (
        "asacomi.jp",
        "bibibi-comic.com",
        "bigcomics.jp",
        "championcross.jp",
        "comic-growl.com",
        "comic-room-base.com",
        "comic-ryu.jp",
        "comic.j-nbooks.jp",
        "comicpash.jp",
        "comicride.jp",
        "comics.comici.jp",
        "comics.manga-bang.com",
        "comirela.com",
        "ebookstore.corkagency.com",
        "g-comi.jp",
        "hanayume.com",
        "hayacomic.jp",
        "heros-web.com",
        "kansai.mag-garden.co.jp",
        "kimicomi.com",
        "manga-zegra.com",
        "mangabu.jp",
        "mangalt.jp",
        "mangaspa.nikkan-spa.jp",
        "namicomic.jp",
        "piacomic.jp",
        "rimacomiplus.jp",
        "studio.booklista.co.jp",
        "takecomic.jp",
        "younganimal.com",
        "youngchampion.jp",
    )
    URL_FORMS = (
        "https://<host>/episodes/<id>",
        "https://<host>/series/<id>",
        "https://<host>/series/<id>/new",
        "https://<host>/series/<id>/<page>",
        "https://<host>/series/<id>/rss",
        "https://<host>/<imprint>/episodes/<id>",
        "https://<host>/<imprint>/series/<id>",
    )
    CONFIG_KEY = "comici-plus"
    PUBLISHERS: ClassVar[dict[str, str]] = {
        "asacomi.jp": "朝日新聞出版",
        "bibibi-comic.com": "ツインエンジン",
        "bigcomics.jp": "小学館",
        "championcross.jp": "秋田書店",
        "comic-growl.com": "ブシロードワークス",
        "comic-room-base.com": "コミックルーム",
        "comic-ryu.jp": "徳間書店",
        "comic.j-nbooks.jp": "実業之日本社",
        "comicpash.jp": "主婦と生活社",
        "comicride.jp": "マイクロマガジン社",
        "comics.comici.jp": "コミチ",
        "comics.manga-bang.com": "Amazia",
        "comirela.com": "スターツ出版",
        "ebookstore.corkagency.com": "コルク",
        "g-comi.jp": "ジーオーティー",
        "hanayume.com": "白泉社",
        "hayacomic.jp": "早川書房",
        "heros-web.com": "ヒーローズ",
        "kansai.mag-garden.co.jp": "マッグガーデン",
        "kimicomi.com": "キルタイムコミュニケーション",
        "manga-zegra.com": "スターツ出版",
        "mangabu.jp": "ファムエンタテイメント",
        "mangalt.jp": "リイド社",
        "mangaspa.nikkan-spa.jp": "扶桑社",
        "namicomic.jp": "ウェイブ",
        "piacomic.jp": "ぴあ",
        "rimacomiplus.jp": "集英社",
        "studio.booklista.co.jp": "ブックリスタ",
        "takecomic.jp": "竹書房",
        "younganimal.com": "白泉社",
        "youngchampion.jp": "秋田書店",
    }
    HEADERS: ClassVar[dict[str, str]] = {
        **Extractor.HEADERS,
        "sec-ch-ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Linux"',
    }

    def __init__(self, session: Client | None = None) -> None:
        """Build an extractor.

        Args:
            session: A session to reuse. A retrying one is made when omitted.
        """
        super().__init__(session)
        self._id_tokens: dict[str, str] = {}

    @classmethod
    def suitable(cls, url: str) -> bool:
        """Report whether `url` is an episode or series URL on a known host.

        The sites hang plenty else off the same domain -- author pages, the
        catalogue, news -- and `-s` asks about every link, so only the shapes
        `episode()` and `series_urls()` read are taken.

        Args:
            url: The URL to check.

        Returns:
            True for `/episodes/<id>` and the `/series/<id>` shapes `is_series`
            takes on `HOSTS`, with or without an imprint in front.
        """
        if not super().suitable(url):
            return False
        return _EPISODE_PATH.match(urlparse(url).path) is not None or cls.is_series(url)

    @classmethod
    def is_series(cls, url: str) -> bool:
        """Report whether `url` names a whole series rather than a single episode.

        Args:
            url: The URL to check.

        Returns:
            True for a `<host>/series/<id>` URL -- the feed, the series page,
            its `/new` tab or a numbered page of it -- which `series_urls` reads.
        """
        path = urlparse(url).path
        return _RSS_PATH.match(path) is not None or _SERIES_PATH.match(path) is not None

    def series_urls(self, url: str) -> list[str]:
        """List every episode a series URL covers.

        A `/rss` URL is read as the feed, every other series URL as the
        paginated episode list; the two disagree on both order and coverage, so
        which one was asked for decides what comes back.

        Args:
            url: A `/series/<id>` URL, with or without `/rss`, `/new` or a page
                number on it.

        Returns:
            One episode URL per listed episode.
        """
        if _RSS_PATH.match(urlparse(url).path):
            return self._feed_urls(url)
        return self._listing_urls(url)

    def episode(self, url: str) -> Episode:
        """Read one episode and list its pages.

        Args:
            url: The episode URL.

        Returns:
            The episode. `pages` is empty when it is not readable.

        Raises:
            NotAnEpisodePageError: The page carries no Comici+ viewer.
        """
        viewer = self.viewer(url)
        raw = self.pages(viewer)
        return Episode(
            url=viewer.url,
            series_title=viewer.series_title,
            episode_title=viewer.episode_title,
            pages=tuple(
                Page(
                    url=str(page["imageUrl"]),
                    width=int(page.get("width") or 0),
                    height=int(page.get("height") or 0),
                    extra={"scramble": str(page.get("scramble") or _IDENTITY_SCRAMBLE)},
                )
                for page in raw
            ),
            prev_url=viewer.prev_url,
            next_url=viewer.next_url,
            metadata={"viewer_id": viewer.viewer_id, "api_base": viewer.api_base, "pages": raw},
            writer=viewer.writer,
            publisher=self.publisher(viewer.url),
            published=viewer.published,
        )

    def image(self, page: Page, episode: Episode) -> Image.Image:
        """Fetch one page and put its tiles back in order.

        Page images are only served with a site Referer.

        Args:
            page: The page to fetch.
            episode: The episode the page belongs to.

        Returns:
            The page in reading order.
        """
        image = self._fetch_image(
            page.url,
            headers={**self._headers(episode.url, _IMAGE_HEADERS), "Referer": episode.url},
        )
        return descramble(image, parse_scramble(str(page.extra.get("scramble") or _IDENTITY_SCRAMBLE)))

    def login(self, url: str, username: str, password: str) -> None:
        """Sign in, so episodes the account may read become readable.

        Sites run NextAuth behind `/api/auth`, with a credentials provider that
        takes a Comici ID or an email address. The session cookie lands on the
        shared session and the returned id token is sent with later API calls.
        This grants nothing the account does not already own.

        Args:
            url: Any URL on the site to sign in to.
            username: A Comici ID or the email address the account uses.
            password: The account's password.

        Raises:
            LoginError: The site refused the credentials.
        """
        origin = self._origin(url)
        auth = f"{origin}/api/auth"
        csrf = self._get(f"{auth}/csrf", headers=self._headers(url, _API_HEADERS))

        res = self._session.post(
            f"{auth}/callback/credentials",
            data={
                "id": username,
                "password": password,
                "csrfToken": csrf.json()["csrfToken"],
                "callbackUrl": f"{origin}/",
                "json": "true",
            },
            headers={**self._headers(url, _API_HEADERS), "Origin": origin, "Referer": f"{origin}/"},
            timeout=self.TIMEOUT,
        )
        # NextAuth answers a bad password with a 401 rather than a JSON error.
        if res.status_code in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN):
            msg = f"{origin} refused the credentials for {username!r}."
            raise LoginError(msg)
        res.raise_for_status()

        session = self._get(f"{auth}/session", headers=self._headers(url, _API_HEADERS))
        token = (session.json() or {}).get("idToken")
        if not token:
            msg = f"{origin} refused the credentials for {username!r}."
            raise LoginError(msg)
        self._id_tokens[origin] = str(token)

    def viewer(self, url: str) -> Viewer:
        """Read the viewer parameters off an episode page.

        Args:
            url: The episode URL.

        Returns:
            The parsed viewer.

        Raises:
            NotAnEpisodePageError: The page carries no Comici+ viewer.
        """
        res = self._get(url, headers=self._headers(url, _DOCUMENT_HEADERS))
        soup = BeautifulSoup(res.content, "html.parser")

        # The series header credits the work, and the episode header dates it, on every site, hydrated or not.
        writer = _credits(soup)
        dated = soup.select_one("p.ep-main-h-date")
        published = published_on(dated.get_text(strip=True)) if isinstance(dated, Tag) else None
        element = soup.find(id=_VIEWER_ID)
        if not isinstance(element, Tag):
            return replace(self._viewer_from_api(url), writer=writer, published=published)

        viewer_id = str(element.attrs.get("data-comici-viewer-id", "")) or None
        if viewer_id is None:
            msg = f"'#{_VIEWER_ID}' on {url} carries no data-comici-viewer-id."
            raise NotAnEpisodePageError(msg)

        api_domain = str(element.attrs.get("data-api-domain", "/api"))
        parsed = urlparse(url)
        api_base = (
            f"{parsed.scheme}://{parsed.netloc}{api_domain}" if api_domain.startswith("/") else f"https://{api_domain}"
        )

        series_title, episode_title = self._titles(soup, element, viewer_id)

        prev_id = str(element.attrs.get("data-prev-episode-id", ""))
        next_id = str(element.attrs.get("data-next-episode-id", ""))

        return Viewer(
            url=url,
            viewer_id=viewer_id,
            member_jwt=str(element.attrs.get("data-member-jwt", "")),
            content_id=str(element.attrs.get("data-content-id", "")),
            api_base=api_base,
            series_title=series_title,
            episode_title=episode_title,
            prev_url=urljoin(url, prev_id) if prev_id else None,
            next_url=urljoin(url, next_id) if next_id else None,
            writer=writer,
            published=published,
        )

    def pages(self, viewer: Viewer, member_jwt: str | None = None) -> list[dict[str, Any]]:
        """List every page of an episode, as `book/contentsInfo` describes them.

        `contentsInfo` refuses a range wider than the episode, so the total is
        asked for first and the real range fetched second.

        Args:
            viewer: The viewer to list.
            member_jwt: Overrides the token the episode page carried, if any.

        Returns:
            The pages, in reading order. Empty when the episode is not readable.
        """
        if viewer.inline_pages is not None:
            return list(viewer.inline_pages)
        if member_jwt is None:
            member_jwt = viewer.member_jwt
        total = int(self._contents_info(viewer, 0, 0, member_jwt).get("totalPages") or 0)
        if total <= 0:
            return []
        body = self._contents_info(viewer, 0, total - 1, member_jwt)
        pages: list[dict[str, Any]] = list(body.get("result") or [])
        return sorted(pages, key=lambda page: int(page["sort"]))

    def _headers(self, url: str, kind: dict[str, str] | None = None) -> dict[str, str]:
        """Headers for `url`, carrying the id token once its site is signed in."""
        headers = {**self.HEADERS, **(kind or {})}
        token = self._id_tokens.get(self._origin(url))
        if token:
            headers["Authorization"] = token
        return headers

    def _feed_urls(self, url: str) -> list[str]:
        """List the episodes a series feed carries.

        `<host>/series/<id>/rss` is a plain RSS 2.0 feed, newest episode first,
        which is the order kept here. It only holds the most recent episodes,
        where `_listing_urls` reaches the whole series. Its item links carry a
        `utm_*` query the episode page has no use for.
        """
        res = self._get(url, headers=self._headers(url, _DOCUMENT_HEADERS))
        root = ET.fromstring(res.content)  # noqa: S314 (a trusted host, see `HOSTS`)
        urls = [
            urljoin(url, href.split("?", 1)[0].split("#", 1)[0])
            for href in ((link.text or "").strip() for link in root.iterfind("./channel/item/link"))
            if href
        ]
        if not urls:
            msg = f"the feed at {url} lists no episode."
            raise NotAnEpisodePageError(msg)
        return urls

    def _listing_urls(self, url: str) -> list[str]:
        """List every episode of a series by walking its numbered pages.

        `/series/<id>/1` is the first page of the episode list, oldest episode
        first, and the page after the last one answers 404. Whatever the given
        URL pointed at -- the series page itself, its `/new` tab or a page in
        the middle -- the walk starts at page 1, so the whole series comes back.
        """
        parsed = urlparse(url)
        match = _SERIES_PATH.match(parsed.path)
        if match is None:
            msg = f"{url} is not a series page."
            raise UnsupportedUrlError(msg)

        base = f"{parsed.scheme}://{parsed.netloc}{match['prefix']}/series/{match['id']}"
        urls: list[str] = []
        seen: set[str] = set()
        number = 1
        while True:
            page = f"{base}/{number}"
            res = self._session.get(page, headers=self._headers(page, _DOCUMENT_HEADERS), timeout=self.TIMEOUT)
            if res.status_code != HTTPStatus.OK:
                break
            # A site that answers an out-of-range page number with the last page
            # instead of a 404 would loop forever, so a page holding nothing new
            # ends the walk as well.
            fresh = [href for href in self._episode_links(res.content, page) if href not in seen]
            if not fresh:
                break
            seen.update(fresh)
            urls += fresh
            number += 1

        if not urls:
            msg = f"the series at {base} lists no episode."
            raise NotAnEpisodePageError(msg)
        return urls

    @staticmethod
    def _episode_links(html: bytes, url: str) -> list[str]:
        """The episode URLs a series page links to, in document order, deduplicated."""
        soup = BeautifulSoup(html, "html.parser")
        links: list[str] = []
        for anchor in soup.find_all("a", href=True):
            href = str(anchor["href"]).split("?", 1)[0].split("#", 1)[0]
            absolute = urljoin(url, href)
            if _EPISODE_PATH.match(urlparse(absolute).path) and absolute not in links:
                links.append(absolute)
        return links

    def _viewer_from_api(self, url: str) -> Viewer:
        """Read an episode that renders its viewer only after hydration.

        Newer sites ship an episode page with no `#comici-viewer` element on
        it and describe the episode through `/api/episodes/{id}` instead. Its
        `content` blocks either hand the page images over directly, already
        unscrambled (ebookstore.corkagency.com), or carry one `viewer` block
        naming the viewer id to look up with `contentsInfo` (championcross.jp).
        An episode the account may not read has no `content` at all.
        """
        parsed = urlparse(url)
        episode_id = parsed.path.rstrip("/").rsplit("/", 1)[-1]
        api_base = f"{parsed.scheme}://{parsed.netloc}/api"
        res = self._session.get(
            f"{api_base}/episodes/{episode_id}",
            headers={**self._headers(url, _API_HEADERS), "Referer": url},
            timeout=self.TIMEOUT,
        )
        body = res.json() if res.is_success else None
        episode = body.get("episode") if isinstance(body, dict) else None
        if not isinstance(episode, dict):
            msg = f"no '#{_VIEWER_ID}' element on {url}, and its episode API describes none either."
            raise NotAnEpisodePageError(msg)

        series = episode.get("series") or {}
        summary = episode.get("summary") or {}
        prev_id = str(episode.get("previousEpisodeId") or "")
        next_id = str(episode.get("nextEpisodeId") or "")
        viewer_id = self._viewer_block_id(episode)
        return Viewer(
            url=url,
            viewer_id=viewer_id or str(episode.get("id") or episode_id),
            api_base=api_base,
            series_title=str(series.get("name") or "").strip() or episode_id,
            episode_title=str(summary.get("title") or "").strip() or episode_id,
            prev_url=urljoin(url, prev_id) if prev_id else None,
            next_url=urljoin(url, next_id) if next_id else None,
            content_id=str(episode.get("contentId") or "") if viewer_id else "",
            inline_pages=None if viewer_id else self._inline_pages(episode),
        )

    @staticmethod
    def _viewer_block_id(episode: dict[str, Any]) -> str:
        """The viewer id a `viewer` content block names, or "" when the JSON has none."""
        for node in episode.get("content") or []:
            if isinstance(node, dict) and node.get("type") == "viewer" and node.get("viewerId"):
                return str(node["viewerId"])
        return ""

    @staticmethod
    def _inline_pages(episode: dict[str, Any]) -> tuple[dict[str, Any], ...]:
        """Turn the `content` blocks of an episode JSON into pages."""
        return tuple(
            {
                "imageUrl": str(node["url"]),
                "scramble": _IDENTITY_SCRAMBLE,
                "sort": index,
                "width": int(node.get("width") or 0),
                "height": int(node.get("height") or 0),
                "expiresOn": 0,
            }
            for index, node in enumerate(
                node
                for node in episode.get("content") or []
                if isinstance(node, dict) and node.get("type") == "image" and node.get("url")
            )
        )

    def _contents_info(self, viewer: Viewer, page_from: int, page_to: int, member_jwt: str = "") -> dict[str, Any]:
        params: dict[str, str | int] = {
            "user-id": member_jwt,
            "comici-viewer-id": viewer.viewer_id,
            "page-from": page_from,
            "page-to": page_to,
        }
        if viewer.content_id:
            params["contentId"] = viewer.content_id
        res = self._get(
            f"{viewer.api_base}/book/contentsInfo",
            params=params,
            # Some sites (studio.booklista.co.jp) answer 403 without a site Referer.
            headers={**self._headers(viewer.url, _API_HEADERS), "Referer": viewer.url},
        )
        body = res.json()
        if not isinstance(body, dict) or "result" not in body:
            msg = f"contentsInfo refused the request: {body}"
            raise GetjmangaError(msg)
        return body

    @staticmethod
    def _titles(soup: BeautifulSoup, element: Tag, viewer_id: str) -> tuple[str, str]:
        """Split `og:title` -- `"<series>・<episode> | <site>"` -- into its parts."""
        og = soup.find("meta", property="og:title")
        heading = str(og.attrs.get("content", "")) if isinstance(og, Tag) else ""
        if not heading and soup.title:
            heading = soup.title.get_text()
        heading = heading.rsplit(" | ", 1)[0].strip()

        series, _, episode = heading.partition("・")
        if not episode:
            series, episode = str(element.attrs.get("data-share-text", "")), heading
        return (series.lstrip("#").strip() or viewer_id, episode.strip() or viewer_id)
