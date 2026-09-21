"""comicブースト (幻冬舎コミックス), read through ACCESS's PUBLUS Reader for Browser.

The storefront is plain server-rendered HTML: a work page, `/content/<id>`,
lists its episodes ten per page, and an episode page, `/product/<id>`,
answers with a 302 to `/viewer/viewer.html?cid=<token>` when the session may
read it -- and with an HTML page carrying an `alert()` when it may not. The
redirect also sets an `access_id` cookie that the viewer's license call,
`/pageapi/viewer/c.php?cid=`, insists on; that call names the episode's
content directory on the CDN.

The content directory holds a `configuration_pack.json`, which
`viewers/publus.py` unwraps, walks for the page files and unshuffles.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup
from bs4.element import Tag

from getjmanga.errors import LoginError, NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractor import Episode, Extractor, Page, published_on
from getjmanga.viewers.publus import decode_pack, descramble, pages

if TYPE_CHECKING:
    from httpx import Client
    from PIL import Image

BASE_URL = "https://comic-boost.com"
LOGIN_URL = f"{BASE_URL}/login"
#: The viewer's license call; the `cid` of the viewer URL goes in as is.
LICENSE_URL = f"{BASE_URL}/pageapi/viewer/c.php"
VIEWER_PATH = "/viewer/viewer.html"
#: What the site says on a product page it will not open.
_NOT_FOUND = "作品情報が見つかりませんでした"

_PRODUCT_PATH = re.compile(r"^/product/(?P<id>\d{8})/?$")
_CONTENT_PATH = re.compile(r"^/content/(?P<id>\d{8})/?$")


@dataclass(frozen=True)
class Colophon:
    """What the end-of-episode page, `/colophon/<id>`, says about an episode."""

    series_title: str
    episode_title: str
    prev_url: str | None = None
    next_url: str | None = None
    content_url: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


def _prev_link(soup: BeautifulSoup, next_link: Tag | None) -> str | None:
    """The previous episode's link: the other `/product/` button of the colophon, which has no class of its own."""
    for anchor in soup.select('a[href^="/product/"]'):
        if anchor is not next_link:
            return urljoin(BASE_URL, str(anchor.attrs["href"]))
    return None


def _update_date(item: Tag) -> str:
    """A listed episode's `p.update-date`, `2026/02/03`."""
    dated = item.select_one("p.update-date")
    return dated.get_text(strip=True) if isinstance(dated, Tag) else ""


def _credit(text: str) -> str:
    """`役割：名前` as `名前 (役割)`; a bare name as it is."""
    role, sep, name = text.partition("：")
    return f"{name.strip()} ({role.strip()})" if sep else text.strip()


def product_url(product_id: str) -> str:
    """The canonical URL of an episode."""
    return f"{BASE_URL}/product/{product_id}"


def parse_colophon(html: str | bytes) -> Colophon | None:
    """Read the titles and the next episode off a colophon page.

    Args:
        html: The page.

    Returns:
        None when the page names no episode, which is what an unknown id gets.
    """
    soup = BeautifulSoup(html, "html.parser")
    share = soup.find(class_="js-share-btn-twitter")
    if not isinstance(share, Tag) or not share.attrs.get("data-title"):
        return None
    next_link = soup.select_one("a.next[href]")
    back = soup.select_one('a[href^="/content/"]')
    return Colophon(
        series_title=str(share.attrs["data-title"]),
        episode_title=str(share.attrs.get("data-title-sub") or ""),
        prev_url=_prev_link(soup, next_link),
        next_url=urljoin(BASE_URL, str(next_link.attrs["href"])) if isinstance(next_link, Tag) else None,
        content_url=urljoin(BASE_URL, str(back.attrs["href"])) if isinstance(back, Tag) else None,
        raw={key: str(value) for key, value in share.attrs.items() if key.startswith("data-")},
    )


def viewer_cid(url: str) -> str | None:
    """The `cid` of a viewer URL, or None for any other page."""
    parsed = urlparse(url)
    if parsed.path != VIEWER_PATH:
        return None
    cids = parse_qs(parsed.query).get("cid")
    return cids[0] if cids else None


class Boost(Extractor):
    """Fetch episodes from comicブースト."""

    NAME = "boost"
    HOSTS = ("comic-boost.com", "www.comic-boost.com")
    URL_FORMS = (
        "https://comic-boost.com/product/<id>",
        "https://comic-boost.com/content/<id>",
    )
    PUBLISHER = "幻冬舎コミックス"
    #: How many listing pages a work is allowed to have before the walk gives up.
    MAX_LISTING_PAGES: ClassVar[int] = 500

    def __init__(self, session: Client | None = None) -> None:
        """Build an extractor.

        Args:
            session: A session to reuse. A retrying one is made when omitted.
        """
        super().__init__(session)
        #: The credits of each work page read, by URL.
        self._credits: dict[str, str] = {}
        #: The `update-date` of every listed episode seen so far, by product id.
        self._dates: dict[str, str] = {}

    @classmethod
    def suitable(cls, url: str) -> bool:
        """Report whether the extractor takes `url`.

        Args:
            url: The URL to check.

        Returns:
            True for an episode or a work page on a known host.
        """
        if not super().suitable(url):
            return False
        path = urlparse(url).path
        return _PRODUCT_PATH.match(path) is not None or _CONTENT_PATH.match(path) is not None

    @classmethod
    def is_series(cls, url: str) -> bool:
        """Report whether `url` is a work page rather than an episode.

        Args:
            url: The URL to check.

        Returns:
            True for `/content/<id>`.
        """
        return _CONTENT_PATH.match(urlparse(url).path) is not None

    def series_urls(self, url: str) -> list[str]:
        """List every episode of a work, first episode first.

        The work page lists ten episodes per page; `?order=asc&p=<n>` walks
        them oldest first until the pager's "next" is disabled.

        Args:
            url: A work URL.

        Returns:
            One episode URL per listed episode, locked ones included.

        Raises:
            UnsupportedUrlError: The URL is not a work page.
            NotAnEpisodePageError: The work lists no episode.
        """
        match = _CONTENT_PATH.match(urlparse(url).path)
        if match is None:
            msg = f"{url} is not a work page."
            raise UnsupportedUrlError(msg)
        listing = f"{BASE_URL}/content/{match['id']}"
        urls: list[str] = []
        for page in range(1, self.MAX_LISTING_PAGES + 1):
            res = self._get(listing, params={"order": "asc", "p": page})
            soup = BeautifulSoup(res.content, "html.parser")
            fresh = False
            for item in soup.select("a.book-product-list-item[data-id]"):
                candidate = product_url(str(item.attrs["data-id"]))
                self._dates.setdefault(str(item.attrs["data-id"]), _update_date(item))
                if candidate not in urls:
                    urls.append(candidate)
                    fresh = True
            pager_next = soup.select_one("li.to-next")
            if not fresh or pager_next is None or "disabled" in (pager_next.get("class") or []):
                break
        if not urls:
            msg = f"the work at {url} lists no episode."
            raise NotAnEpisodePageError(msg)
        return urls

    def episode(self, url: str) -> Episode:
        """Read one episode and list its pages.

        Args:
            url: The episode URL.

        Returns:
            The episode. `pages` is empty when the product page does not
            hand the session over to the viewer.

        Raises:
            UnsupportedUrlError: The URL is not an episode URL.
            NotAnEpisodePageError: The site knows no such episode.
        """
        match = _PRODUCT_PATH.match(urlparse(url).path)
        if match is None:
            msg = f"{url} is not an episode page."
            raise UnsupportedUrlError(msg)
        product_id = match["id"]
        canonical = product_url(product_id)

        colophon = parse_colophon(self._get(f"{BASE_URL}/colophon/{product_id}").content)
        res = self._get(canonical)
        landed = str(res.url)
        cid = viewer_cid(landed)
        if cid is None:
            if colophon is None or _NOT_FOUND in res.text:
                msg = f"no episode {product_id} on {BASE_URL}."
                raise NotAnEpisodePageError(msg)
            return Episode(
                url=canonical,
                series_title=colophon.series_title,
                episode_title=colophon.episode_title,
                prev_url=colophon.prev_url,
                next_url=colophon.next_url,
                metadata={"colophon": colophon.raw},
                writer=self._writer(colophon.content_url),
                publisher=self.PUBLISHER,
                published=published_on(self._update_date(colophon.content_url, product_id)),
            )

        license_ = self._get(LICENSE_URL, params={"cid": cid}, headers={**self.HEADERS, "Referer": landed}).json()
        if not isinstance(license_, dict):
            license_ = {}
        if colophon is None:
            # No colophon, but the license call names the episode as `<episode> - <series>`.
            title, _, series = str(license_.get("cti") or "").partition(" - ")
            colophon = Colophon(series_title=series or product_id, episode_title=title or product_id)
        content_url = license_.get("url")
        if str(license_.get("status")) != "200" or not content_url:
            # The license call refuses a cid its cookie does not match; nothing to read.
            return Episode(
                url=canonical,
                series_title=colophon.series_title,
                episode_title=colophon.episode_title,
                prev_url=colophon.prev_url,
                next_url=colophon.next_url,
                metadata={"colophon": colophon.raw, "license": license_},
                writer=self._writer(colophon.content_url),
                publisher=self.PUBLISHER,
                published=published_on(self._update_date(colophon.content_url, product_id)),
            )
        pack = decode_pack(self._get(urljoin(content_url, "configuration_pack.json")).text)
        return Episode(
            url=canonical,
            series_title=colophon.series_title,
            episode_title=colophon.episode_title,
            pages=tuple(pages(pack, content_url)),
            prev_url=colophon.prev_url,
            next_url=colophon.next_url,
            metadata={"colophon": colophon.raw, "license": license_, "configuration": pack.content["configuration"]},
            writer=self._writer(colophon.content_url),
            publisher=self.PUBLISHER,
            published=published_on(self._update_date(colophon.content_url, product_id)),
        )

    def _update_date(self, content_url: str | None, product_id: str) -> str:
        """The `update-date` the work page lists for `product_id`.

        The listing is walked oldest first until the episode shows up, and
        every episode passed on the way is remembered too.
        """
        if content_url is None:
            return ""
        if product_id not in self._dates:
            for page in range(1, self.MAX_LISTING_PAGES + 1):
                soup = BeautifulSoup(self._get(content_url, params={"order": "asc", "p": page}).content, "html.parser")
                items = soup.select("a.book-product-list-item[data-id]")
                for item in items:
                    self._dates.setdefault(str(item.attrs["data-id"]), _update_date(item))
                pager_next = soup.select_one("li.to-next")
                if (
                    product_id in self._dates
                    or not items
                    or pager_next is None
                    or "disabled" in (pager_next.get("class") or [])
                ):
                    break
        return self._dates.get(product_id, "")

    def _writer(self, content_url: str | None) -> str:
        """The credits off the work page the colophon links back to, read once per work.

        The work page's own `ul.author-list` comes first, one `役割：名前` per
        entry (or a bare name); the lists further down belong to other works.
        """
        if content_url is None:
            return ""
        if content_url not in self._credits:
            soup = BeautifulSoup(self._get(content_url).content, "html.parser")
            authors = soup.select_one("ul.author-list")
            items = authors.select("li.author") if isinstance(authors, Tag) else []
            self._credits[content_url] = ", ".join(_credit(item.get_text("", strip=True)) for item in items)
        return self._credits[content_url]

    def image(self, page: Page, episode: Episode) -> Image.Image:
        """Fetch one page and unshuffle its tiles.

        Args:
            page: The page to fetch.
            episode: The episode the page belongs to.

        Returns:
            The page image.
        """
        image = self._fetch_image(page.url, headers={**self.HEADERS, "Referer": episode.url})
        if "pattern" not in page.extra:
            return image
        seeds = page.extra["seeds"]
        block = page.extra["block"]
        return descramble(image, int(page.extra["pattern"]), (seeds[0], seeds[1], seeds[2]), (block[0], block[1]))

    def login(self, url: str, username: str, password: str) -> None:  # noqa: ARG002 (one site, one login page)
        """Sign in with an email address, so bought episodes become readable.

        The form is a plain POST to `/login`; a refusal bounces back to the
        login page with a `msgid` and its reason in a `p.text-warning`.
        Accounts made through Google, LINE or Yahoo! cannot sign in this way.

        Args:
            url: Any URL on the site.
            username: The account's email address.
            password: The account's password.

        Raises:
            LoginError: The site refused the credentials.
        """
        res = self._session.post(
            LOGIN_URL,
            params={"rl": "/"},
            data={"account[email]": username, "account[password]": password},
            headers={**self.HEADERS, "Origin": BASE_URL, "Referer": LOGIN_URL},
            timeout=self.TIMEOUT,
        )
        res.raise_for_status()
        if urlparse(str(res.url)).path.rstrip("/") != "/login":
            return
        warning = BeautifulSoup(res.content, "html.parser").find(class_="text-warning")
        reason = " ".join(warning.get_text().split()) if isinstance(warning, Tag) else "no reason given"
        msg = f"{BASE_URL} refused the credentials for {username!r}: {reason}"
        raise LoginError(msg)
