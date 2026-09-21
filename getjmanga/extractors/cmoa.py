"""コミックシーモア (NTTソルマーレ): a bookstore whose 立ち読み goes through Voyager's SpeedBinb reader.

Every volume in the store has a browser sample, and a volume on a free
campaign serves the whole book through the same reader; what an account has
bought is not reachable, since the store signs in through an OpenID provider
guarded by reCAPTCHA.

The way in is the store's `/reader/sample/` link of a volume, which the site
bounces via `/reader/browserviewer/content_id/<id>/sample_flg/1/` to the reader,
`/bib/speedreader/?cid=<bib id>&u0=1`. The reader is SpeedBinb in its `sbc`
flavour (`ServerType` 0): `bibGetCntntInfo.php` names the contents server and
hands over the scramble tables, `sbcGetCntnt.php` lists the pages and
`sbcGetImg.php` serves each of them; the `u0`..`u9` parameters of the reader
URL ride along on all three, as the viewer forwards them. The API also says
which shop page (`ShopURL`) the content belongs to, which is how a reader URL
gets its titles and its place in the series.

A series is the title page, `/title/<id>/`, whose lineup is read in its
"easy" display mode, twenty volumes a page, oldest first. The lineup links
the first volume as the title page itself, so volumes are named by the
`/vol/<n>/` URL derived from their content id (`1` + title id padded to ten
digits + volume number padded to four), which is also what `ShopURL` says.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from http import HTTPStatus
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup
from bs4.element import Tag

from getjmanga.errors import NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractor import Episode, Extractor, Page, neighbours, ordinal, published_on
from getjmanga.viewers import speedbinb

if TYPE_CHECKING:
    from datetime import date

    from httpx import Client
    from PIL import Image

HOST = "www.cmoa.jp"
BASE_URL = f"https://{HOST}"

#: The reader's query parameters the viewer forwards to every API call.
_FORWARDED_PARAM = re.compile(r"^u\d$")

#: How many lineup pages to read before giving up on a title that never ends.
_MAX_LISTING_PAGES = 500

# A title page: the series, and the first volume too.
_TITLE_PATH = re.compile(r"^/title/(?P<id>\d+)/?$")
# A volume page.
_VOLUME_PATH = re.compile(r"^/title/(?P<id>\d+)/vol/(?P<vol>\d+)/?$")
# The sample link of a volume, as the store writes it: with the ids in the
# path, or, without any, in the query (`?title_id=<id>&content_id=<id>`).
_SAMPLE_PATH = re.compile(r"^/reader/sample(?:/title_id/(?P<id>\d+)(?:/content_id/(?P<cid>\d+))?)?/?$")
# Where the sample link bounces to, and where the store sends a reader of a bought volume.
_VIEWER_PATH = re.compile(r"^/reader/browserviewer/content_id/(?P<cid>\d+)(?:/sample_flg/\d+)?/?$")
# The reader itself; `cid` in the query is the content's id in the reader's numbering.
_READER_PATH = re.compile(r"^/bib/speedreader/?$")

# The reader's element: `data-ptbinb` names the API endpoint.
_VIEWER_SELECTOR = "#content[data-ptbinb]"
# The API endpoint when the reader page does not say (it always does).
_INFO_PATH = "/bib/sws/bibGetCntntInfo.php"

# The lineup of a title page in its easy display mode, one `<li>` per volume.
_LINEUP_SELECTOR = "ul.title_vol_easy_box > li"
# The series title: the breadcrumb's link to the title page.
_BREADCRUMB_SELECTOR = ".brCramb a[href]"
# The volume's own heading on a title page.
_HEADING_SELECTOR = "h1.titleName"
# The work's authors under the heading, one link each, and the publisher's breadcrumb.
_AUTHOR_SELECTOR = "div.title_details_author_name a"
_PUBLISHER_SELECTOR = '.brCramb a[href^="/search/publisher/"]'

# The content id's shape: a kind digit, the title id and the volume number.
_CONTENT_ID = re.compile(r"^(?P<kind>\d)(?P<title>\d{10})(?P<vol>\d{4})$")


@dataclass(frozen=True)
class Volume:
    """One volume of a title's lineup."""

    url: str
    content_id: str
    title: str


@dataclass(frozen=True)
class Listing:
    """What a title page says: the series title, who it is by and its volumes, oldest first."""

    title: str
    volumes: tuple[Volume, ...]
    writer: str = ""
    publisher: str = ""


@dataclass(frozen=True)
class ListingPage:
    """One page of a title's lineup."""

    #: The series title: the breadcrumb's, else the heading's, else the id.
    title: str
    #: The volumes listed on the page, in order.
    volumes: tuple[Volume, ...]
    #: The last page number the pagination links name; this page's when there is none.
    last: int
    #: The work's authors, comma-separated, and the publisher the breadcrumb names.
    writer: str = ""
    publisher: str = ""


@dataclass(frozen=True)
class Target:
    """Where an accepted URL leads: the URL to fetch to land on the reader, and the ids it names."""

    entry: str
    title_id: str | None = None
    content_id: str | None = None


def volume_url(title_id: str, vol: int | str) -> str:
    """The canonical volume page URL."""
    return f"{BASE_URL}/title/{title_id}/vol/{int(vol)}/"


def content_id_of(title_id: str, vol: int | str) -> str:
    """The store's content id of a volume: `1`, the title id on ten digits, the volume on four."""
    return f"1{int(title_id):010d}{int(vol):04d}"


def title_and_volume_of(content_id: str) -> tuple[str, int] | None:
    """The title id and volume number a content id encodes, or None when it is not shaped so."""
    match = _CONTENT_ID.match(content_id)
    if match is None:
        return None
    return str(int(match["title"])), int(match["vol"])


def parse_listing_page(html: str | bytes, title_id: str) -> ListingPage:
    """Read one page of a title's lineup.

    Args:
        html: The title page, in its easy display mode.
        title_id: The title's id, to tell its own links from the recommendations'.

    Returns:
        The page: the series title, the volumes on it, the last page number,
        and who the work is by and published by.
    """
    soup = BeautifulSoup(html, "html.parser")
    title = ""
    for anchor in soup.select(_BREADCRUMB_SELECTOR):
        match = _TITLE_PATH.match(urlparse(str(anchor["href"])).path)
        if match and match["id"] == title_id:
            title = anchor.get_text(strip=True)
    if not title:
        heading = soup.select_one(_HEADING_SELECTOR)
        title = heading.get_text(strip=True) if isinstance(heading, Tag) else ""

    volumes: list[Volume] = []
    for item in soup.select(_LINEUP_SELECTOR):
        volume = _parse_volume(item, title_id)
        if volume is not None and all(volume.content_id != known.content_id for known in volumes):
            volumes.append(volume)

    # The pagination links; the switch to the other display mode, which
    # pages differently, is not one of them.
    last = 1
    for anchor in soup.select("a[href]"):
        href = urlparse(str(anchor["href"]))
        query = parse_qs(href.query)
        page = query.get("page")
        if page and _TITLE_PATH.match(href.path) and query.get("disp_mode", ["easy"]) == ["easy"]:
            last = max(last, int(page[0]))
    publisher = soup.select_one(_PUBLISHER_SELECTOR)
    return ListingPage(
        title=title or title_id,
        volumes=tuple(volumes),
        last=last,
        writer=", ".join(anchor.get_text(strip=True) for anchor in soup.select(_AUTHOR_SELECTOR)),
        publisher=publisher.get_text(strip=True) if isinstance(publisher, Tag) else "",
    )


def _parse_volume(item: Tag, title_id: str) -> Volume | None:
    """One lineup entry: its content id (off the cart button), its page and its title."""
    cart = item.select_one("a[_content_id]")
    content_id = str(cart.get("_content_id") or "") if isinstance(cart, Tag) else ""
    heading = item.select_one("h3 a[href]")
    href = urlparse(str(heading["href"])) if isinstance(heading, Tag) else None
    vol_match = _VOLUME_PATH.match(href.path) if href else None
    if vol_match and vol_match["id"] == title_id:
        vol: int | None = int(vol_match["vol"])
    elif href and _TITLE_PATH.match(href.path) and content_id:
        # The first volume is linked as the title page itself.
        encoded = title_and_volume_of(content_id)
        vol = encoded[1] if encoded else None
    else:
        vol = None
    if vol is None:
        return None
    if not content_id:
        content_id = content_id_of(title_id, vol)
    return Volume(
        url=volume_url(title_id, vol),
        content_id=content_id,
        title=heading.get_text(strip=True) if isinstance(heading, Tag) else "",
    )


class Cmoa(Extractor):
    """Fetch the samples and free volumes of コミックシーモア.

    A volume page, a sample link, the browser viewer's URL and the reader's
    own URL all name one volume; a title page names the series. A title's
    lineup is read once per run, for the series title and the volume after
    each one.
    """

    NAME = "cmoa"
    PUBLISHER = "NTTソルマーレ"
    HOSTS = (HOST,)
    URL_FORMS = (
        "https://www.cmoa.jp/title/<title-id>/vol/<n>/",
        "https://www.cmoa.jp/reader/sample/title_id/<title-id>/content_id/<content-id>/",
        "https://www.cmoa.jp/reader/browserviewer/content_id/<content-id>/",
        "https://www.cmoa.jp/title/<title-id>/",
    )

    def __init__(self, session: Client | None = None) -> None:
        """Build an extractor.

        Args:
            session: A session to reuse. A retrying one is made when omitted.
        """
        super().__init__(session)
        # Title id -> its lineup, so a title page is read once however many volumes are fetched.
        self._listings: dict[str, Listing] = {}

    @classmethod
    def suitable(cls, url: str) -> bool:
        """Report whether the extractor takes `url`.

        Args:
            url: The URL to check.

        Returns:
            True for an https title page, volume page, sample link, browser
            viewer URL or reader URL on `www.cmoa.jp`.
        """
        if not super().suitable(url):
            return False
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        if _TITLE_PATH.match(parsed.path) or _VOLUME_PATH.match(parsed.path) or _VIEWER_PATH.match(parsed.path):
            return True
        sample = _SAMPLE_PATH.match(parsed.path)
        if sample:
            return bool(sample["id"] or query.get("title_id") or query.get("content_id"))
        return _READER_PATH.match(parsed.path) is not None and bool(query.get("cid"))

    @classmethod
    def is_series(cls, url: str) -> bool:
        """Report whether `url` is a title page.

        Args:
            url: The URL to check.

        Returns:
            True for `https://www.cmoa.jp/title/<title-id>/`.
        """
        return cls.suitable(url) and _TITLE_PATH.match(urlparse(url).path) is not None

    def series_urls(self, url: str) -> list[str]:
        """List every volume of a title, oldest first.

        Args:
            url: A title page URL.

        Returns:
            One volume page URL per listed volume, oldest first.

        Raises:
            UnsupportedUrlError: The URL is not a title page.
            NotAnEpisodePageError: There is no such title, or it lists no volume.
        """
        match = _TITLE_PATH.match(urlparse(url).path)
        if match is None:
            msg = f"{url} is not a title page."
            raise UnsupportedUrlError(msg)
        listing = self._listing(match["id"])
        if not listing.volumes:
            msg = f"the title at {url} lists no volume."
            raise NotAnEpisodePageError(msg)
        return [volume.url for volume in listing.volumes]

    def episode(self, url: str) -> Episode:
        """Read one volume: its titles, its pages and the volume after it.

        Args:
            url: A volume page, sample link, browser viewer or reader URL.

        Returns:
            The volume, at its canonical volume page URL. `pages` is empty
            when the reader's API refuses the content.

        Raises:
            UnsupportedUrlError: The URL is a title page, or not one the extractor takes.
            NotAnEpisodePageError: The URL leads to no reader: the store
                answers an error page, or the reader's API does not describe the content.
        """
        target = self._target(url)
        res = self._session.get(target.entry, headers=self.HEADERS, timeout=self.TIMEOUT)
        if res.status_code == HTTPStatus.NOT_FOUND:
            msg = f"{url} is gone (HTTP 404)."
            raise NotAnEpisodePageError(msg)
        res.raise_for_status()
        reader_url = str(res.url or target.entry)
        parsed = urlparse(reader_url)
        query = parse_qs(parsed.query)
        if _READER_PATH.match(parsed.path) is None or not query.get("cid"):
            msg = f"{url} leads to no reader, but to {reader_url}."
            raise NotAnEpisodePageError(msg)
        bib_id = query["cid"][0]
        forwarded = {name: values[0] for name, values in query.items() if _FORWARDED_PARAM.match(name)}
        reader = BeautifulSoup(res.content, "html.parser")
        viewer = reader.select_one(_VIEWER_SELECTOR)
        info_url = urljoin(reader_url, str(viewer["data-ptbinb"]) if isinstance(viewer, Tag) else _INFO_PATH)

        content = speedbinb.content_info(
            self,
            info_url,
            bib_id,
            referer=reader_url,
            params=forwarded,
            server_types=frozenset({speedbinb.SERVER_TYPE_SBC}),
        )
        item = content.item if content else {}

        content_id = str(item.get("ContentID") or target.content_id or "")
        shop_url = str(item.get("ShopURL") or "")
        shop = _VOLUME_PATH.match(urlparse(shop_url).path) if shop_url else None
        title_id = shop["id"] if shop else target.title_id
        if title_id is None and content_id:
            encoded = title_and_volume_of(content_id)
            title_id = encoded[0] if encoded else None

        listing = self._listing(title_id) if title_id else None
        volume, prev_url, next_url = _place(listing, content_id)
        number = ordinal(listing.volumes, volume) if listing and volume else None
        canonical = volume.url if volume else (urljoin(BASE_URL, shop_url) if shop else url)
        series_title = listing.title if listing else content_id or bib_id
        episode_title = str(item.get("SubTitle") or (volume.title if volume else "") or content_id or bib_id)
        metadata: dict[str, Any] = {
            "title_id": title_id,
            "content_id": content_id,
            "bib_id": bib_id,
            "reader_url": reader_url,
            "shop_url": urljoin(BASE_URL, shop_url) if shop_url else None,
        }
        published = self._released(canonical) if volume else None
        if content is None:
            return Episode(
                url=canonical,
                series_title=series_title,
                episode_title=episode_title,
                prev_url=prev_url,
                next_url=next_url,
                metadata={**metadata, "locked": True},
                writer=listing.writer if listing else "",
                publisher=(listing.publisher if listing else "") or self.PUBLISHER,
                published=published,
                number=number,
            )

        book = speedbinb.page_list(self, content, referer=reader_url, params=forwarded)

        return Episode(
            url=canonical,
            series_title=series_title,
            episode_title=episode_title,
            pages=book.pages,
            prev_url=prev_url,
            next_url=next_url,
            metadata={
                **metadata,
                "locked": False,
                "contents_server": content.server,
                "info": content.info,
            },
            writer=listing.writer if listing else "",
            publisher=(listing.publisher if listing else "") or self.PUBLISHER,
            published=published,
            number=number,
        )

    def _released(self, volume_url: str) -> date | None:
        """The `配信開始日` a volume page shows, which nothing the reader answers carries."""
        res = self._session.get(volume_url, headers=self.HEADERS, timeout=self.TIMEOUT)
        if not res.is_success:
            return None
        soup = BeautifulSoup(res.content, "html.parser")
        for label in soup.select("div.category_line_f_l_l"):
            if label.get_text(strip=True) == "配信開始日":
                value = label.find_next_sibling("div")
                return published_on(value.get_text(" ", strip=True)) if isinstance(value, Tag) else None
        return None

    def image(self, page: Page, episode: Episode) -> Image.Image:
        """Fetch one page and put its tiles back where they belong.

        Args:
            page: The page to fetch.
            episode: The episode the page belongs to.

        Returns:
            The page in reading order, padding gone.
        """
        return speedbinb.fetch_page(self, page, referer=str(episode.metadata.get("reader_url") or episode.url))

    def _target(self, url: str) -> Target:
        """Where `url` leads: what to fetch to land on the reader, and the ids it names.

        A volume page is turned into the sample link of the volume, looked up
        in the title's lineup, or built from the ids when the lineup does not
        have it (the store then answers an error page).
        """
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        volume = _VOLUME_PATH.match(parsed.path)
        if volume:
            listing = self._listing(volume["id"])
            wanted = volume_url(volume["id"], volume["vol"])
            listed = next((entry for entry in listing.volumes if entry.url == wanted), None)
            content_id = listed.content_id if listed else content_id_of(volume["id"], volume["vol"])
            return Target(_sample_url(volume["id"], content_id), volume["id"], content_id)
        sample = _SAMPLE_PATH.match(parsed.path)
        if sample:
            title_id = sample["id"] or (query.get("title_id") or [None])[0]
            content_id = sample["cid"] or (query.get("content_id") or [None])[0]
            if title_id or content_id:
                return Target(url, title_id, content_id)
        viewer = _VIEWER_PATH.match(parsed.path)
        if viewer:
            return Target(url, None, viewer["cid"])
        if _READER_PATH.match(parsed.path) and query.get("cid"):
            return Target(url)
        what = "a volume page" if _TITLE_PATH.match(parsed.path) else f"a page {self.NAME} reads"
        msg = f"{url} is not {what}."
        raise UnsupportedUrlError(msg)

    def _listing(self, title_id: str) -> Listing:
        """Read a title's lineup, once per title."""
        if title_id not in self._listings:
            title = writer = publisher = ""
            volumes: list[Volume] = []
            page, last = 1, 1
            while page <= last and page <= _MAX_LISTING_PAGES:
                res = self._session.get(
                    f"{BASE_URL}/title/{title_id}/",
                    params={"page": page, "order": "up", "disp_mode": "easy"},
                    headers=self.HEADERS,
                    timeout=self.TIMEOUT,
                )
                if res.status_code == HTTPStatus.NOT_FOUND:
                    msg = f"no title {title_id} on {HOST} (HTTP 404)."
                    raise NotAnEpisodePageError(msg)
                res.raise_for_status()
                found = parse_listing_page(res.content, title_id)
                title, writer, publisher = title or found.title, writer or found.writer, publisher or found.publisher
                last = found.last
                new = [v for v in found.volumes if all(v.content_id != known.content_id for known in volumes)]
                if not new:
                    break
                volumes.extend(new)
                page += 1
            self._listings[title_id] = Listing(
                title=title or title_id, volumes=tuple(volumes), writer=writer, publisher=publisher
            )
        return self._listings[title_id]


def _sample_url(title_id: str, content_id: str) -> str:
    """The store's sample link of a volume."""
    return f"{BASE_URL}/reader/sample/title_id/{title_id}/content_id/{content_id}/"


def _place(listing: Listing | None, content_id: str) -> tuple[Volume | None, str | None, str | None]:
    """The lineup entry of a content id and the URLs of the volumes either side, when the lineup has it."""
    if listing is None or not content_id:
        return None, None, None
    volume = next((entry for entry in listing.volumes if entry.content_id == content_id), None)
    if volume is None:
        return None, None, None
    before, after = neighbours(listing.volumes, volume)
    return volume, before.url if before else None, after.url if after else None
