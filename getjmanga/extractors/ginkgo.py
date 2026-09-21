"""銀杏社 (Ginnansha): コミックいわてWEB and 漫画街, two static sites of one publisher.

Both are plain Apache directories of hand-written HTML: no viewer script, no
API, no scrambling, no account, and plain `http://` (コミックいわてWEB also
answers on https, 漫画街 does not). They lay their comics out differently:

- コミックいわてWEB (`comiciwate.jp`) publishes one-shots. A work page,
  `/comic/<slug>/` or `/foreign/<slug>_<lang>/` for the translations, carries
  every page as `images/NN.jpg` in `#contents`, the artist's profile in
  `#profile`. There is nothing to walk to and no work lists episodes, so a work
  page is an episode of the anthology.
- 漫画街 (`manga-gai.net`) runs serials. A work's index page,
  `/manga/<work>/<work>_index/<work>_index.html`, holds a `<select>` of its
  episodes; an episode is a directory `/manga/<work>/<episode>/` of pages
  `01.html`, `02.html`, ... each showing one image in `#originalmanga` and a
  NEXT link to the following page, until a page that carries the feedback
  form instead of an image. Nothing on those pages names the episode, so the
  title and the next episode come from the work's index, read once per work.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from http import HTTPStatus
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from bs4.element import Tag

from getjmanga.errors import NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractor import Episode, Extractor, Page, neighbours

if TYPE_CHECKING:
    from collections.abc import Iterator

    from httpx import Client

#: What the コミックいわてWEB work pages are episodes of.
IWATE_SERIES_TITLE = "コミックいわてWEB"

# A コミックいわてWEB work page: `/comic/<slug>/`, or `/foreign/<slug>_<lang>/`
# for a translation. `/comic/area/` and `/comic/artist/` are search pages.
_IWATE_PATH = re.compile(r"^/(?P<section>comic|foreign)/(?!area/?$|artist/?$)(?P<slug>[^/]+)/?$")
# A 漫画街 episode page: one page of an episode directory.
_GAI_PAGE_PATH = re.compile(r"^/manga/(?P<work>[^/]+)/(?P<episode>[^/]+)/(?P<page>\d+)\.html$")
# A 漫画街 work index: the directory name ends in `_index`, sometimes with a leading `_`.
_GAI_INDEX_PATH = re.compile(r"^/manga/(?P<work>[^/]+)/[^/]*_index/[^/]+\.html$")
# An episode directory name: an optional letter prefix, a number (`599.5`, `371.54`), a suffix.
_EPISODE_NAME = re.compile(r"^(?P<prefix>\D*)(?P<number>\d+(?:\.\d+)?)(?P<rest>.*)$")


@dataclass(frozen=True)
class IwateWork:
    """What a コミックいわてWEB work page says."""

    #: The work page URL, with its trailing slash.
    url: str
    #: The work's title: the `<title>` up to ` | `.
    title: str
    #: The artist, from `#profile`.
    author: str
    #: The page images in reading order, deduplicated.
    images: tuple[str, ...]


@dataclass(frozen=True)
class GaiPage:
    """What one page of a 漫画街 episode says."""

    #: The page URL.
    url: str
    #: The image on the page, or None on the feedback-form page that ends an episode.
    image: str | None
    #: The NEXT link, when the page carries one.
    next_url: str | None
    #: The work's title, from the title image in the side menu.
    series_title: str
    #: The work's index page, linked from the side menu.
    index_url: str | None


@dataclass(frozen=True)
class GaiListing:
    """The episodes a 漫画街 work index lists, oldest first."""

    #: Episode directory name -> the episode's first page.
    urls: dict[str, str]
    #: Episode directory name -> the title the index gives it.
    titles: dict[str, str]

    def neighbours_of(self, episode: str) -> tuple[str | None, str | None]:
        """The first pages of the episodes either side of `episode`, None at either end."""
        before, after = neighbours(list(self.urls), episode)
        return self.urls[before] if before else None, self.urls[after] if after else None


def parse_iwate_work(html: str | bytes, url: str) -> IwateWork:
    """Read a コミックいわてWEB work page.

    Args:
        html: The page.
        url: The URL it came from, to resolve the image paths against.

    Returns:
        What the page says.

    Raises:
        NotAnEpisodePageError: The page carries no comic.
    """
    soup = BeautifulSoup(html, "html.parser")
    contents = soup.find(id="contents")
    if not isinstance(contents, Tag):
        msg = f"no comic on {url}."
        raise NotAnEpisodePageError(msg)
    images: dict[str, None] = {}
    for img in contents.find_all("img", src=True):
        if not isinstance(img, Tag) or img.find_parent(id="profile") is not None:
            continue
        src = str(img["src"])
        # The pages are the work's own `images/NN.jpg`; the profile heading and
        # link buttons sit in `../images/` and start with a name, not a number.
        if re.match(r"^images/[^/]*\d+\.(?:jpe?g|png|gif)$", src, re.IGNORECASE):
            images[urljoin(url, src)] = None
    if not images:
        msg = f"no comic on {url}."
        raise NotAnEpisodePageError(msg)
    title = _text(soup.title).split(" | ", 1)[0].strip()
    author = ""
    profile = soup.find(id="profile")
    if isinstance(profile, Tag):
        name = profile.find("div", class_="name")
        author = _text(name) if isinstance(name, Tag) else ""
    return IwateWork(url=url, title=title, author=author, images=tuple(images))


def parse_gai_page(html: str | bytes, url: str) -> GaiPage:
    """Read one page of a 漫画街 episode.

    Args:
        html: The page.
        url: The URL it came from, to resolve the links against.

    Returns:
        What the page says.

    Raises:
        NotAnEpisodePageError: The page has no `#originalmanga` block at all.
    """
    soup = BeautifulSoup(html, "html.parser")
    manga = soup.find(id="originalmanga")
    if not isinstance(manga, Tag):
        msg = f"no comic on {url}."
        raise NotAnEpisodePageError(msg)
    img = manga.find("img", src=True)
    next_anchor = next(
        (a for a in manga.find_all("a", href=True) if isinstance(a, Tag) and "NEXT" in a.get_text().upper()),
        None,
    )
    series_title = ""
    index_url = None
    menu = soup.find(id="contents_menuWrap")
    if isinstance(menu, Tag):
        title_img = menu.find("img", title=True)
        if isinstance(title_img, Tag):
            series_title = str(title_img["title"]).strip()
            wrapper = title_img.find_parent("a", href=True)
            if isinstance(wrapper, Tag):
                index_url = urljoin(url, str(wrapper["href"]))
    if index_url is None:
        anchor = soup.select_one("div.nav-to-list a[href]")
        if isinstance(anchor, Tag):
            index_url = urljoin(url, str(anchor["href"]))
    return GaiPage(
        url=url,
        image=urljoin(url, str(img["src"])) if isinstance(img, Tag) else None,
        next_url=urljoin(url, str(next_anchor["href"])) if isinstance(next_anchor, Tag) else None,
        series_title=series_title,
        index_url=index_url,
    )


def parse_gai_listing(html: str | bytes, url: str) -> GaiListing:
    """Read the episodes a 漫画街 work index lists.

    The index is a `<select>` whose options link the first page of each
    episode. Some works list newest first, some oldest first, and specials
    such as `599.5` sit in a block of their own, so the episodes are ordered
    by their directory names -- a letter prefix, then the number -- rather
    than as listed.

    Args:
        html: The index page.
        url: The URL it came from, to resolve the links against.

    Returns:
        The listing, oldest first, deduplicated, restricted to the index's own work.
    """
    match = _GAI_INDEX_PATH.match(urlparse(url).path)
    work = match["work"] if match else None
    soup = BeautifulSoup(html, "html.parser")
    urls: dict[str, str] = {}
    titles: dict[str, str] = {}
    for option in soup.find_all("option", value=True):
        if not isinstance(option, Tag):
            continue
        href = urljoin(url, str(option["value"]).strip())
        page = _GAI_PAGE_PATH.match(urlparse(href).path)
        if page is None or (work is not None and page["work"] != work) or page["episode"] in urls:
            continue
        urls[page["episode"]] = href
        titles[page["episode"]] = option.get_text(strip=True)
    order = sorted(urls, key=episode_key)
    return GaiListing(urls={name: urls[name] for name in order}, titles={name: titles[name] for name in order})


def episode_key(name: str) -> tuple[str, float, str]:
    """Sort key for a 漫画街 episode directory name: `01` < `12.5` < `13` < `jk01`.

    Args:
        name: The directory name.

    Returns:
        The letter prefix, the number and whatever follows it.
    """
    match = _EPISODE_NAME.match(name)
    if match is None:
        return (name, float("inf"), "")
    return (match["prefix"], float(match["number"]), match["rest"])


def _text(tag: Tag | None) -> str:
    return tag.get_text(strip=True) if isinstance(tag, Tag) else ""


class Ginkgo(Extractor):
    """Fetch episodes from コミックいわてWEB and 漫画街.

    A コミックいわてWEB work page is one episode of the anthology. A 漫画街
    episode is walked page by page; its work index, which names the episodes
    and orders them, is read once per work and kept on the instance.
    """

    NAME = "ginkgo"
    HOSTS = ("comiciwate.jp", "manga-gai.net", "www.comiciwate.jp", "www.manga-gai.net")
    PUBLISHER = "銀杏社"
    URL_FORMS = (
        "http://comiciwate.jp/comic/<work>/",
        "http://comiciwate.jp/foreign/<work>_<lang>/",
        "http://www.manga-gai.net/manga/<work>/<episode>/01.html",
        "http://www.manga-gai.net/manga/<work>/<work>_index/<work>_index.html",
    )

    def __init__(self, session: Client | None = None) -> None:
        """Build an extractor.

        Args:
            session: A session to reuse. A retrying one is made when omitted.
        """
        super().__init__(session)
        # Index URL -> what it lists, so a work's index is read once.
        self._listings: dict[str, GaiListing] = {}

    @classmethod
    def suitable(cls, url: str) -> bool:
        """Report whether the extractor takes `url`.

        Both sites are served over plain http, so unlike the default this
        takes http as well as https.

        Args:
            url: The URL to check.

        Returns:
            True for a work, episode or index URL on a known host.
        """
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or parsed.hostname not in cls.HOSTS:
            return False
        return bool(
            _IWATE_PATH.match(parsed.path) or _GAI_PAGE_PATH.match(parsed.path) or _GAI_INDEX_PATH.match(parsed.path)
        )

    @classmethod
    def is_series(cls, url: str) -> bool:
        """Report whether `url` is a 漫画街 work index.

        Args:
            url: The URL to check.

        Returns:
            True for `/manga/<work>/<work>_index/<work>_index.html`.
        """
        return _GAI_INDEX_PATH.match(urlparse(url).path) is not None

    def series_urls(self, url: str) -> list[str]:
        """List every episode a 漫画街 work index links, oldest first.

        Args:
            url: The index URL.

        Returns:
            The first page of each listed episode.

        Raises:
            UnsupportedUrlError: The URL is not a work index.
            NotAnEpisodePageError: The index lists no episode.
        """
        if not self.is_series(url):
            msg = f"{url} is not a series page."
            raise UnsupportedUrlError(msg)
        listing = self._listing(url)
        if not listing.urls:
            msg = f"the series at {url} lists no episode."
            raise NotAnEpisodePageError(msg)
        return list(listing.urls.values())

    def episode(self, url: str) -> Episode:
        """Read one episode and list its pages.

        Args:
            url: A コミックいわてWEB work page, or any page of a 漫画街 episode
                (the episode is read from its `01.html` whichever page is given).

        Returns:
            The episode. Neither site locks anything, so `pages` is never empty.

        Raises:
            NotAnEpisodePageError: The page is gone or carries no comic.
        """
        path = urlparse(url).path
        if _IWATE_PATH.match(path):
            return self._iwate_episode(url)
        if _GAI_PAGE_PATH.match(path):
            return self._gai_episode(url)
        msg = f"{url} is not a work page nor an episode page."
        raise NotAnEpisodePageError(msg)

    def _iwate_episode(self, url: str) -> Episode:
        """Read a コミックいわてWEB work page as an episode of the anthology."""
        parsed = urlparse(url)
        key = f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}/"
        work = parse_iwate_work(self._fetch(key), key)
        match = _IWATE_PATH.match(parsed.path)
        section, slug = (match["section"], match["slug"]) if match else ("comic", "")
        title = work.title or slug
        # The translations keep the Japanese title now and then, so the
        # language suffix of the slug (`_en`, `_fr`, `_han`, `_kan`) tells them apart.
        if section == "foreign" and "_" in slug:
            title = f"{title} ({slug.rsplit('_', 1)[1]})"
        return Episode(
            url=key,
            series_title=IWATE_SERIES_TITLE,
            episode_title=title,
            pages=tuple(Page(url=src) for src in work.images),
            metadata={"site": "comiciwate", "title": work.title, "author": work.author, "images": list(work.images)},
            writer=work.author,
            publisher=self.PUBLISHER,
        )

    def _gai_episode(self, url: str) -> Episode:
        """Walk a 漫画街 episode from its first page to the feedback form."""
        parsed = urlparse(url)
        match = _GAI_PAGE_PATH.match(parsed.path)
        if match is None:  # pragma: no cover (episode() checked the path)
            msg = f"{url} is not an episode page."
            raise NotAnEpisodePageError(msg)
        directory = f"{parsed.scheme}://{parsed.netloc}/manga/{match['work']}/{match['episode']}/"
        pages = list(self._walk(urljoin(directory, "01.html")))
        if not pages:
            msg = f"no comic on {url}."
            raise NotAnEpisodePageError(msg)
        first = pages[0]
        listing = self._listing(first.index_url) if first.index_url else GaiListing({}, {})
        return Episode(
            url=urljoin(directory, "01.html"),
            series_title=first.series_title or match["work"],
            episode_title=listing.titles.get(match["episode"]) or match["episode"],
            pages=tuple(Page(url=page.image) for page in pages if page.image),
            prev_url=listing.neighbours_of(match["episode"])[0],
            next_url=listing.neighbours_of(match["episode"])[1],
            metadata={
                "site": "manga-gai",
                "work": match["work"],
                "episode": match["episode"],
                "index_url": first.index_url,
                "pages": [page.url for page in pages],
            },
            # 漫画街 names its authors on its front page only, not on the work or its pages.
            publisher=self.PUBLISHER,
        )

    def _walk(self, url: str) -> Iterator[GaiPage]:
        """Follow the NEXT links from `url`, yielding every page that shows an image.

        A page that is gone ends the walk; so does the feedback form, which
        carries no image. A first page that is gone is not an episode.
        """
        seen: set[str] = set()
        current: str | None = url
        while current and current not in seen:
            seen.add(current)
            res = self._session.get(current, headers=self.HEADERS, timeout=self.TIMEOUT)
            if res.status_code == HTTPStatus.NOT_FOUND:
                if current == url:
                    msg = f"{url} is gone (HTTP 404): not an episode."
                    raise NotAnEpisodePageError(msg)
                return
            res.raise_for_status()
            page = parse_gai_page(res.content, str(res.url or current))
            if page.image is None:
                return
            yield page
            current = page.next_url

    def _listing(self, url: str) -> GaiListing:
        """Read a 漫画街 work index, once per URL. An index that is gone lists nothing."""
        if url not in self._listings:
            res = self._session.get(url, headers=self.HEADERS, timeout=self.TIMEOUT)
            if res.status_code == HTTPStatus.NOT_FOUND:
                self._listings[url] = GaiListing({}, {})
            else:
                res.raise_for_status()
                self._listings[url] = parse_gai_listing(res.content, url)
        return self._listings[url]

    def _fetch(self, url: str) -> bytes:
        """GET a page, turning a 404 into `NotAnEpisodePageError`."""
        res = self._session.get(url, headers=self.HEADERS, timeout=self.TIMEOUT)
        if res.status_code == HTTPStatus.NOT_FOUND:
            msg = f"{url} is gone (HTTP 404): not a work page nor an episode."
            raise NotAnEpisodePageError(msg)
        res.raise_for_status()
        return res.content
