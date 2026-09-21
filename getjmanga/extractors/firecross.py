"""ファイアCROSS (Fire CROSS, Hobby Japan), and the CELSYS reader it embeds.

The site is a Laravel app. A work page `/ebook/series/<id>` lists the
episodes (`div.shop-item--episode[data-id]`, oldest first, twenty per
`?page=N`); each carries a form that POSTs `ebook_id` to `/api/reader` and
follows the `redirect` it answers with, a one-shot
`/reader/<id>?trial=0&token=<uuid>` URL. `/reader/<id>` without the token is
a 403, so that is the URL shape this extractor takes as an episode and
resolves itself. A locked episode (a rental the account has not paid for)
gets a 400 `不正なアクセスです` from the API instead of a redirect. The API
is CSRF-protected the Laravel way: the `XSRF-TOKEN` cookie set by any page,
URL-decoded, goes back as an `X-XSRF-TOKEN` header.

The titles, the series link and the next episode come off the colophon page
`/reader/colophon/<id>`, which needs no token and is a 404 for an id that is
no episode. The next episode is the `ebook_id` of the reader form there, or
the `/shop/rental/<id>` modal a paid one opens instead.

The reader is CLIP STUDIO READER's web viewer (`csr-web-*.js`, CELSYS), and
the page data comes from its CGI `/celsys/diazepam_hybrid.php` with the
`param` the reader page carries in `#meta input[name=param]`:

- `mode=7&file=face.xml` describes the book: `TotalPage` and the
  `Scramble` grid (4 x 4 here). An error is a `<Result><Code>` document.
- `mode=8&file=NNNN.xml` describes page `NNNN` (zero-based, four digits):
  its parts (`<Kind scramble='1' No='0000'>1</Kind>`, 1 being JPEG) and the
  tile permutation in `<Scramble>`.
- `mode=1&file=NNNN_PPPP.bin` is the JPEG of part `PPPP` of page `NNNN`.

Every one of those takes `reqtype=0&vm=4` as well and needs neither a cookie
nor a Referer, only the `param`. `descramble()` undoes what the viewer's
canvas does: the image is cut into a grid of tiles whose sides are the
largest multiples of 8 that fit, tile `i` of the grid is filled with tile
`table[i]` of the served image, and the strip left over on the right and at
the bottom stays where it is.

Signing in is a plain form POST to `/login` (`_token`, `email`, `password`);
a refusal comes back as the form again with a `p.form-message--error`.
"""

from __future__ import annotations

import re
from http import HTTPStatus
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urlencode, urljoin, urlparse

from bs4 import BeautifulSoup
from bs4.element import Tag

from getjmanga.errors import LoginError, NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractor import Episode, Extractor, Page, published_on

if TYPE_CHECKING:
    from collections.abc import Sequence

    from httpx import Client
    from PIL import Image

_READER_PATH = re.compile(r"^/reader/(?:colophon/)?(?P<id>\d+)/?$")
_SERIES_PATH = re.compile(r"^/ebook/series/(?P<id>\d+)/?$")
_SHOP_PATH = re.compile(r"/shop/(?:rental|buy)/(?P<id>\d+)")

#: The reader's view mode (`VIEW_TYPE_HYBRID`) every CGI request names.
_VIEW_MODE = "4"
_MODE_JPEG = "1"
_MODE_FACE_XML = "7"
_MODE_PAGE_XML = "8"
_REQUEST_TYPE_FILE = "0"
_KIND_JPEG = "1"
#: `奥付 - <episode> - <series>`: what a colophon's `<title>` holds before the site name.
_TITLE_PARTS = 3
#: Tile sides are multiples of this many pixels.
UNIT = 8


def descramble(image: Image.Image, table: Sequence[int], cols: int, rows: int) -> Image.Image:
    """Put a scrambled page back together.

    Args:
        image: The page as served.
        table: For every tile of the grid (row-major), the index of the tile
            in the served image that belongs there.
        cols: Tiles per row.
        rows: Tiles per column.

    Returns:
        A new image with the tiles in place. The image is returned as is when
        the table is short or the image too small to be tiled, as the viewer
        does.
    """
    width, height = image.size
    if cols < 1 or rows < 1 or len(table) < cols * rows or width < UNIT * cols or height < UNIT * rows:
        return image
    tile_width = width // cols // UNIT * UNIT
    tile_height = height // rows // UNIT * UNIT
    out = image.copy()
    for destination, source in enumerate(table[: cols * rows]):
        if not 0 <= source < cols * rows:
            continue
        sx, sy = source % cols * tile_width, source // cols * tile_height
        dx, dy = destination % cols * tile_width, destination // cols * tile_height
        out.paste(image.crop((sx, sy, sx + tile_width, sy + tile_height)), (dx, dy))
    return out


class FireCross(Extractor):
    """Fetch episodes from ファイアCROSS."""

    NAME = "firecross"
    HOSTS = ("firecross.jp",)
    URL_FORMS = (
        "https://firecross.jp/reader/<id>",
        "https://firecross.jp/ebook/series/<id>",
    )
    CONFIG_KEY = "firecross"
    PUBLISHER = "ホビージャパン"

    def __init__(self, session: Client | None = None) -> None:
        """Build an extractor.

        Args:
            session: A session to reuse. A retrying one is made when omitted.
        """
        super().__init__(session)
        #: The credits of each work page read, by URL.
        self._credits: dict[str, str] = {}
        #: The `公開：` line of every listed episode seen so far, by reader URL.
        self._releases: dict[str, str] = {}

    @classmethod
    def suitable(cls, url: str) -> bool:
        """Report whether the extractor takes `url`.

        Args:
            url: The URL to check.

        Returns:
            True for an https reader or series page on a known host.
        """
        if not super().suitable(url):
            return False
        path = urlparse(url).path
        return _READER_PATH.match(path) is not None or _SERIES_PATH.match(path) is not None

    @classmethod
    def is_series(cls, url: str) -> bool:
        """Report whether `url` is a work page.

        Args:
            url: The URL to check.

        Returns:
            True for `/ebook/series/<id>`.
        """
        return _SERIES_PATH.match(urlparse(url).path) is not None

    def series_urls(self, url: str) -> list[str]:
        """List every episode a work page lists, oldest first.

        Args:
            url: A work page URL.

        Returns:
            One `/reader/<id>` URL per listed episode, oldest first, deduplicated.

        Raises:
            UnsupportedUrlError: The URL is not a work page.
            NotAnEpisodePageError: The page lists no episode.
        """
        match = _SERIES_PATH.match(urlparse(url).path)
        if match is None:
            msg = f"{url} is not a work page."
            raise UnsupportedUrlError(msg)
        origin = self._origin(url)
        series_url = f"{origin}/ebook/series/{match['id']}"
        urls: list[str] = []
        page = 1
        while True:
            soup = BeautifulSoup(self._get(series_url, params={"page": page}).content, "html.parser")
            self._credits.setdefault(series_url, _credits(soup))
            found = []
            for item in soup.select("div.shop-item--episode[data-id]"):
                if not str(item["data-id"]).isdigit():
                    continue
                found.append(_reader_url(origin, str(item["data-id"])))
                release = item.select_one(".shop-item-info-release")
                self._releases.setdefault(found[-1], _text(release).removeprefix("公開：").strip())
            fresh = [episode_url for episode_url in dict.fromkeys(found) if episode_url not in urls]
            if not fresh:
                break
            urls.extend(fresh)
            page += 1
        if not urls:
            msg = f"the work at {url} lists no episode."
            raise NotAnEpisodePageError(msg)
        return urls

    def episode(self, url: str) -> Episode:
        """Read one episode and list its pages.

        Args:
            url: A `/reader/<id>` URL, with or without the site's token query.

        Returns:
            The episode. `pages` is empty when the site refuses to open the
            reader (a rental not paid for, or not signed in for); `next_url`
            is the episode the colophon names as the next one.

        Raises:
            NotAnEpisodePageError: The id is no episode (the colophon is a 404).
        """
        match = _READER_PATH.match(urlparse(url).path)
        if match is None:
            msg = f"{url} is not a reader page."
            raise NotAnEpisodePageError(msg)
        origin = self._origin(url)
        episode_id = match["id"]
        episode_url = _reader_url(origin, episode_id)

        res = self._session.get(f"{origin}/reader/colophon/{episode_id}", headers=self.HEADERS, timeout=self.TIMEOUT)
        if res.status_code == HTTPStatus.NOT_FOUND:
            msg = f"no episode at {url}."
            raise NotAnEpisodePageError(msg)
        res.raise_for_status()
        colophon = BeautifulSoup(res.content, "html.parser")
        series_title, episode_title = _titles(colophon, episode_id)
        next_id = _next_id(colophon)
        home = colophon.select_one("a.colophonBtn__home[href]")
        series_url = urljoin(origin, str(home["href"])) if isinstance(home, Tag) else None
        # The colophon points forward only; the work page lists the episode before
        # and credits the work, which the listing keeps for here.
        prev_url = self._listed_neighbours(series_url, episode_url)[0] if series_url else None
        writer = self._credits.get(series_url or "", "")
        published = published_on(self._releases.get(episode_url, ""))
        listed_at = self._listed_number(series_url, episode_url) if series_url else None
        metadata: dict[str, Any] = {
            "ebook_id": int(episode_id),
            "series_url": str(home["href"]) if isinstance(home, Tag) else None,
            "next_ebook_id": int(next_id) if next_id is not None else None,
        }

        reader_url = self._open_reader(origin, episode_id)
        if reader_url is None:
            return Episode(
                url=episode_url,
                series_title=series_title,
                episode_title=episode_title,
                prev_url=prev_url,
                next_url=_reader_url(origin, next_id) if next_id is not None else None,
                metadata={**metadata, "locked": True},
                writer=writer,
                publisher=self.PUBLISHER,
                published=published,
                number=listed_at,
            )
        reader = BeautifulSoup(self._get(reader_url, headers=self.HEADERS).content, "html.parser")
        cgi, param = _reader_meta(reader)
        if not cgi or not param:
            msg = f"no reader on {reader_url}."
            raise NotAnEpisodePageError(msg)
        cgi_url = cgi if cgi.startswith("http") else f"{origin}{cgi}"

        face = self._cgi_xml(cgi_url, _MODE_FACE_XML, "face.xml", param)
        total = _number(face, "totalpage")
        cols, rows = _number(face, "width", "scramble"), _number(face, "height", "scramble")
        metadata.update({"locked": total <= 0, "total_pages": total, "scramble": [cols, rows]})
        pages: list[Page] = []
        for number in range(total):
            page_xml = self._cgi_xml(cgi_url, _MODE_PAGE_XML, f"{number:04d}.xml", param)
            table = [int(value) for value in re.findall(r"\d+", _text(page_xml.find("scramble")))]
            for part in page_xml.select("part kind"):
                if _text(part) != _KIND_JPEG:
                    continue
                scrambled = str(part.get("scramble", "0")) == "1"
                part_no = str(part.get("no", "0"))
                file = f"{number:04d}_{int(part_no) if part_no.isdigit() else 0:04d}.bin"
                extra = {"scramble": table if scrambled else [], "cols": cols, "rows": rows}
                pages.append(Page(url=_cgi_url(cgi_url, _MODE_JPEG, file, param), extra=extra))
        return Episode(
            url=episode_url,
            series_title=series_title,
            episode_title=episode_title,
            pages=tuple(pages),
            prev_url=prev_url,
            next_url=_reader_url(origin, next_id) if next_id is not None else None,
            metadata=metadata,
            writer=writer,
            publisher=self.PUBLISHER,
            published=published,
            number=listed_at,
        )

    def image(self, page: Page, episode: Episode) -> Image.Image:
        """Fetch one page and unscramble it when its part is scrambled.

        Args:
            page: The page to fetch.
            episode: The episode the page belongs to.

        Returns:
            The page image.
        """
        image = self._fetch_image(page.url, headers={**self.HEADERS, "Referer": episode.url})
        table = [int(value) for value in page.extra.get("scramble") or []]
        if not table:
            return image
        return descramble(image, table, int(page.extra.get("cols", 0)), int(page.extra.get("rows", 0)))

    def login(self, url: str, username: str, password: str) -> None:
        """Sign in, so rentals the account has paid for become readable.

        Args:
            url: Any URL on the site to sign in to.
            username: The email address the account uses.
            password: The account's password.

        Raises:
            LoginError: The site refused the credentials.
        """
        origin = self._origin(url)
        form = BeautifulSoup(self._get(f"{origin}/login", headers=self.HEADERS).content, "html.parser")
        token = form.select_one("form[action$='/login'] input[name=_token]")
        res = self._session.post(
            f"{origin}/login",
            data={
                "_token": str(token["value"]) if isinstance(token, Tag) else "",
                "email": username,
                "password": password,
                "remember_me": "1",
            },
            headers={**self.HEADERS, "Referer": f"{origin}/login"},
            timeout=self.TIMEOUT,
        )
        res.raise_for_status()
        answer = BeautifulSoup(res.content, "html.parser")
        if answer.select_one("p.form-message--error") is not None or answer.select_one("input[name=password]"):
            msg = f"{origin} refused the credentials for {username!r}."
            raise LoginError(msg)

    def _open_reader(self, origin: str, episode_id: str) -> str | None:
        """Ask `/api/reader` for the tokened reader URL of an episode.

        Returns None when the site refuses to open it (a rental not paid for).
        """
        headers = {
            **self.HEADERS,
            "Accept": "application/json",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{origin}/",
        }
        xsrf = self._cookie("XSRF-TOKEN", self.HOSTS[0])
        if xsrf:
            headers["X-XSRF-TOKEN"] = unquote(xsrf)
        res = self._session.post(
            f"{origin}/api/reader",
            data={"ebook_id": episode_id, "vertical": "0"},
            headers=headers,
            timeout=self.TIMEOUT,
        )
        if res.status_code == HTTPStatus.BAD_REQUEST:
            return None
        res.raise_for_status()
        data = res.json()
        redirect = data.get("redirect") if isinstance(data, dict) else None
        return str(redirect) if redirect else None

    def _cgi_xml(self, cgi_url: str, mode: str, file: str, param: str) -> BeautifulSoup:
        """Fetch one XML document off the reader's CGI."""
        return BeautifulSoup(
            self._get(_cgi_url(cgi_url, mode, file, param), headers=self.HEADERS).content, "html.parser"
        )


def _reader_url(origin: str, episode_id: str) -> str:
    return f"{origin}/reader/{episode_id}"


def _cgi_url(cgi_url: str, mode: str, file: str, param: str) -> str:
    query = {"mode": mode, "file": file, "reqtype": _REQUEST_TYPE_FILE, "vm": _VIEW_MODE, "param": param}
    return f"{cgi_url}?{urlencode(query)}"


def _credits(series: BeautifulSoup) -> str:
    """A work page's `ul.ebook-series-author`: `名前 (役割)` per entry, the role its `-type` span."""
    credited = []
    for item in series.select("ul.ebook-series-author li.ebook-series-author-item"):
        kind = item.select_one("span.ebook-series-author-type")
        role = _text(kind)
        link = item.find("a")
        name = _text(link) if isinstance(link, Tag) else ""
        if name:
            credited.append(f"{name} ({role})" if role else name)
    return ", ".join(credited)


def _reader_meta(reader: BeautifulSoup) -> tuple[str, str]:
    """The CGI path and the `param` of a reader page's `#meta` inputs."""
    values = {}
    for name in ("cgi", "param"):
        tag = reader.select_one(f"#meta input[name={name}]")
        values[name] = str(tag.get("value", "")) if isinstance(tag, Tag) else ""
    return values["cgi"], values["param"]


def _titles(colophon: BeautifulSoup, episode_id: str) -> tuple[str, str]:
    """The series and episode titles of a colophon page."""
    series_title = _text(colophon.select_one("p.colophonContent__seriesTitle"))
    episode_title = _text(colophon.select_one("div.colophonContent__body > p.fw-bold"))
    if not series_title or not episode_title:
        # `奥付 - <episode> - <series> | ファイアCROSS`
        parts = _text(colophon.title).split(" | ", 1)[0].split(" - ")
        if len(parts) >= _TITLE_PARTS:
            episode_title = episode_title or parts[1]
            series_title = series_title or " - ".join(parts[2:])
    return series_title, episode_title or episode_id


def _next_id(colophon: BeautifulSoup) -> str | None:
    """The `ebook_id` the colophon's next-episode area opens, or None at the end of the series."""
    area = colophon.select_one("div.colophonNextArea")
    if not isinstance(area, Tag):
        return None
    form_input = area.select_one("form input[name=ebook_id][value]")
    if isinstance(form_input, Tag) and str(form_input["value"]).isdigit():
        return str(form_input["value"])
    for button in area.select("[data-modal-source]"):
        match = _SHOP_PATH.search(str(button["data-modal-source"]))
        if match:
            return match["id"]
    return None


def _number(soup: BeautifulSoup, name: str, parent: str | None = None) -> int:
    """The integer content of the first `<name>` (under `<parent>` when given), 0 without one."""
    scope = soup.find(parent) if parent else soup
    tag = scope.find(name) if isinstance(scope, Tag) else None
    text = _text(tag) if isinstance(tag, Tag) else ""
    return int(text) if text.isdigit() else 0


def _text(tag: Tag | None) -> str:
    return " ".join(tag.get_text(" ", strip=True).split()) if isinstance(tag, Tag) else ""
