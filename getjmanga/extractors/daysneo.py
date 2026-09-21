"""DAYS NEO (講談社), Kodansha's manga submission site, and its two server-rendered viewers.

Every page is plain server-side HTML around jQuery. A work page
`/works/<id>.html` lists its episodes under `もくじ`, oldest first, and an
episode page `/works/<id>/episode/<id>.html` carries its viewer inline:

- A horizontal (right-to-left, fullPage.js) episode has one empty
  `<img id="page_N">` per page and fills the sources in from an inline
  script, `$('#page_' + N).attr("src", "...")`, in reading order.
- A vertical (`タテ読み`, WEBTOON) episode has the sources right on its
  `<img id="view_N">` elements, top to bottom.

The pages are unscrambled JPEG/PNG files on `img.daysneo.com`, served
without a Referer or a cookie. `次の話へ` at the end of the viewer names the
next episode; the last one has no such link.

Works are either public or `編集者のみ閲覧可能` (visible to Kodansha's editors
only): the latter list their episodes without links, and an episode URL that
cannot be shown is redirected to its work page (a missing work goes to
`/404error.html`). A reader account unlocks nothing, so there is no login.
Mobile browsers are redirected to the same pages under `/sp/`, which is
accepted and folded back into the desktop URL.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from bs4.element import Tag

from getjmanga.errors import NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractor import Episode, Extractor, Page, published_on

_WORK_PATH = re.compile(r"^/(?:sp/)?works/(?P<work>[0-9a-f]+)\.html$")
_EPISODE_PATH = re.compile(r"^/(?:sp/)?works/(?P<work>[0-9a-f]+)/episode/(?P<episode>[0-9a-f]+)\.html$")

#: How the horizontal viewer hands its `<img id="page_N">` elements their sources.
_PAGE_SCRIPT = re.compile(r"""\$\(\s*'#page_'\s*\+\s*(?P<index>\d+)\s*\)\.attr\(\s*"src"\s*,\s*"(?P<src>[^"]+)"\s*\)""")
#: The `<img>` ids of the two viewers, and the number that orders them.
_IMAGE_ID = re.compile(r"^(?:page|view)_(?P<index>\d+)$")

_NEXT_LABEL = "次の話へ"
_EDITORS_ONLY = "編集者のみ閲覧可能"


class DaysNeo(Extractor):
    """Fetch episodes from DAYS NEO."""

    NAME = "daysneo"
    HOSTS = ("daysneo.com",)
    PUBLISHER = "講談社"
    URL_FORMS = (
        "https://daysneo.com/works/<id>/episode/<id>.html",
        "https://daysneo.com/works/<id>.html",
    )

    @classmethod
    def suitable(cls, url: str) -> bool:
        """Report whether the extractor takes `url`.

        Args:
            url: The URL to check.

        Returns:
            True for an https work or episode page on a known host, `/sp/` included.
        """
        if not super().suitable(url):
            return False
        path = urlparse(url).path
        return _EPISODE_PATH.match(path) is not None or _WORK_PATH.match(path) is not None

    @classmethod
    def is_series(cls, url: str) -> bool:
        """Report whether `url` is a work page.

        Args:
            url: The URL to check.

        Returns:
            True for `/works/<id>.html`.
        """
        return _WORK_PATH.match(urlparse(url).path) is not None

    def series_urls(self, url: str) -> list[str]:
        """List every readable episode a work page links to, oldest first.

        Args:
            url: A work page URL.

        Returns:
            One episode URL per linked episode, in the order listed, deduplicated.

        Raises:
            UnsupportedUrlError: The URL is not a work page.
            NotAnEpisodePageError: No work page there, or it links no episode
                (an editors-only work lists its episodes without links).
        """
        match = _WORK_PATH.match(urlparse(url).path)
        if match is None:
            msg = f"{url} is not a work page."
            raise UnsupportedUrlError(msg)
        work_url = _work_url(url, match["work"])
        res = self._get(work_url)
        soup = BeautifulSoup(res.text, "html.parser")
        if _WORK_PATH.match(urlparse(str(res.url)).path) is None or soup.select_one("ul.ul01") is None:
            msg = f"no work page at {url}."
            raise NotAnEpisodePageError(msg)
        urls: list[str] = []
        for anchor in soup.select("ul.ul01 a[href]"):
            href = urljoin(work_url, str(anchor["href"]))
            episode_match = _EPISODE_PATH.match(urlparse(href).path)
            if episode_match is None:
                continue
            episode_url = _episode_url(href, episode_match["work"], episode_match["episode"])
            if episode_url not in urls:
                urls.append(episode_url)
        if not urls:
            reason = "is visible to editors only" if _editors_only(soup) else "lists no episode"
            msg = f"the work at {url} {reason}."
            raise NotAnEpisodePageError(msg)
        return urls

    def episode(self, url: str) -> Episode:
        """Read one episode and list its pages.

        Args:
            url: An episode page URL.

        Returns:
            The episode. `pages` is empty when the site redirects the episode
            to an editors-only work page; `next_url` is what `次の話へ` links.

        Raises:
            NotAnEpisodePageError: The URL is not an episode page, or the site
                shows no viewer there (a missing episode, a missing work).
        """
        match = _EPISODE_PATH.match(urlparse(url).path)
        if match is None:
            msg = f"{url} is not an episode page."
            raise NotAnEpisodePageError(msg)
        episode_url = _episode_url(url, match["work"], match["episode"])
        res = self._get(episode_url)
        soup = BeautifulSoup(res.text, "html.parser")
        landed = urlparse(str(res.url)).path

        if _EPISODE_PATH.match(landed) is None or soup.find("body", id="viewer") is None:
            if _WORK_PATH.match(landed) is not None and _editors_only(soup):
                # The episode exists but is shown to editors only: locked, not gone.
                return Episode(
                    url=episode_url,
                    series_title=_text(soup.select_one("p.f150.b")) or match["work"],
                    episode_title=match["episode"],
                    metadata={"work_id": match["work"], "episode_id": match["episode"], "editors_only": True},
                    writer=_text(soup.select_one("p.author")),
                    publisher=self.PUBLISHER,
                )
            msg = f"no viewer on {url}."
            raise NotAnEpisodePageError(msg)

        series_title = _text(soup.select_one("h1.f160 a")) or _text(soup.select_one("#header .title h1"))
        episode_title = _text(soup.select_one("p.b.f140")) or _header_episode_title(soup)
        sources = _page_sources(res.text, soup)
        # The page only links the next episode; the work page lists the one before.
        prev_url = self._listed_neighbours(_work_url(url, match["work"]), episode_url)[0]
        next_url = _next_url(episode_url, soup)
        metadata: dict[str, Any] = {
            "work_id": match["work"],
            "episode_id": match["episode"],
            "editors_only": False,
            "author": _text(soup.select_one("p.b.mt40.f120")),
            "published": _text(soup.select_one("p.color3")),
            "direction": "vertical" if soup.find("img", id="view_1") else "horizontal",
            "page_count": len(sources),
        }
        return Episode(
            url=episode_url,
            series_title=series_title,
            episode_title=episode_title or match["episode"],
            pages=tuple(Page(url=src) for src in sources),
            prev_url=prev_url,
            next_url=next_url,
            metadata=metadata,
            writer=str(metadata["author"]),
            publisher=self.PUBLISHER,
            published=published_on(str(metadata["published"])),
            number=self._listed_number(_work_url(url, match["work"]), episode_url),
        )


def _page_sources(html: str, soup: BeautifulSoup) -> list[str]:
    """The page image URLs of either viewer, in reading order.

    Args:
        html: The episode page.
        soup: The same page, parsed.

    Returns:
        One URL per page, ordered by the number in the `<img>` id.
    """
    numbered: dict[int, str] = {}
    for tag in soup.find_all("img", id=_IMAGE_ID):
        if not isinstance(tag, Tag):
            continue
        id_match = _IMAGE_ID.match(str(tag.get("id", "")))
        src = str(tag.get("src") or "").strip()
        if id_match is not None and src:
            numbered.setdefault(int(id_match["index"]), src)
    for script_match in _PAGE_SCRIPT.finditer(html):
        numbered.setdefault(int(script_match["index"]), script_match["src"])
    return [numbered[index] for index in sorted(numbered)]


def _next_url(episode_url: str, soup: BeautifulSoup) -> str | None:
    """The episode `次の話へ` links, or None on the last episode."""
    for anchor in soup.select("a[href]"):
        if _text(anchor) != _NEXT_LABEL:
            continue
        href = urljoin(episode_url, str(anchor["href"]))
        match = _EPISODE_PATH.match(urlparse(href).path)
        if match is not None:
            return _episode_url(href, match["work"], match["episode"])
    return None


def _header_episode_title(soup: BeautifulSoup) -> str:
    """`第N話` out of the viewer header, `<h1>series</h1>　｜　第N話 [1p ／ 12p]`."""
    title = soup.select_one("#header .title")
    if title is None:
        return ""
    for tag in title.find_all(["h1", "span"]):
        tag.decompose()
    return _text(title).strip("｜| ")


def _editors_only(soup: BeautifulSoup) -> bool:
    """Whether a work page flags its work as `編集者のみ閲覧可能`."""
    return any(_EDITORS_ONLY in _text(tag) for tag in soup.select("div.mt5"))


def _work_url(url: str, work_id: str) -> str:
    """The desktop work page URL of `work_id` on the host of `url`."""
    return f"{_origin(url)}/works/{work_id}.html"


def _episode_url(url: str, work_id: str, episode_id: str) -> str:
    """The desktop episode page URL of `episode_id` on the host of `url`."""
    return f"{_origin(url)}/works/{work_id}/episode/{episode_id}.html"


def _origin(url: str) -> str:
    parsed = urlparse(url)
    return f"https://{parsed.hostname or DaysNeo.HOSTS[0]}"


def _text(tag: Tag | None) -> str:
    return " ".join(tag.get_text(" ", strip=True).split()) if isinstance(tag, Tag) else ""
