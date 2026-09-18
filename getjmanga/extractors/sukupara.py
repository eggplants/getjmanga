"""すくパラぷらす, 竹書房's free parenting-comic web magazine: one plain JPEG per page, one HTML page per image."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup
from bs4.element import Tag

from .common import Episode, Extractor, NotAnEpisodePageError, Page, UnsupportedUrlError

if TYPE_CHECKING:
    from requests import Response

BASE_URL = "https://sukupara.jp"

#: The path of a reading page: `?manga_id=<work>&story_id=<episode>[&page_no=<n>]`.
EPISODE_PATH = "/plus/mag_detail.php"
#: The path of a work page: `?manga_id=<work>`.
SERIES_PATH = "/plus/mag_top.php"

#: The site name the `og:title` of every page ends with.
_SITE_SUFFIX = "｜すくパラぷらす"

#: The heading of a reading page: `「<series>」<episode title>-<page number>`.
_STORY_TITLE = re.compile(r"^(?:「(?P<series>.*?)」)?(?P<title>.*?)(?:-(?P<page>\d+))?$", re.DOTALL)

#: How many reading pages an episode may have before the walk gives up on a
#: looping `次のページへ` link.
MAX_PAGES = 500


def episode_url(manga_id: str, story_id: str) -> str:
    """The canonical URL of one episode, without a page number."""
    return f"{BASE_URL}{EPISODE_PATH}?manga_id={manga_id}&story_id={story_id}"


def series_url(manga_id: str) -> str:
    """The canonical URL of one work page."""
    return f"{BASE_URL}{SERIES_PATH}?manga_id={manga_id}"


def _ids(url: str) -> dict[str, str]:
    """The numeric query parameters of `url` the site keys its pages by."""
    query = parse_qs(urlparse(url).query)
    return {
        key: values[0]
        for key, values in query.items()
        if key in {"manga_id", "story_id", "page_no"} and values and values[0].isdigit()
    }


class Sukupara(Extractor):
    """Fetch episodes from すくパラぷらす (sukupara.jp/plus/).

    Every episode is free and needs no account. The site is server-rendered
    PHP: a reading page shows exactly one image and links to the next page,
    so an episode is read by walking those links; the last page then links to
    the next episode. The work page lists the first episode, the newest one and
    a short back-number list -- the episodes in between are taken down after a
    while and answer with an error page.
    """

    NAME = "sukupara"
    HOSTS = ("sukupara.jp",)
    URL_FORMS = (
        "https://sukupara.jp/plus/mag_detail.php?manga_id=<id>&story_id=<id>",
        "https://sukupara.jp/plus/mag_top.php?manga_id=<id>",
    )

    @classmethod
    def suitable(cls, url: str) -> bool:
        """Report whether the extractor takes `url`.

        Args:
            url: The URL to check.

        Returns:
            True for a reading page (any page number) or a work page under `/plus/`.
        """
        if not super().suitable(url):
            return False
        path = urlparse(url).path
        ids = _ids(url)
        if path == EPISODE_PATH:
            return "manga_id" in ids and "story_id" in ids
        return path == SERIES_PATH and "manga_id" in ids

    @classmethod
    def is_series(cls, url: str) -> bool:
        """Report whether `url` names a whole work rather than one episode.

        Args:
            url: The URL to check.

        Returns:
            True for `mag_top.php?manga_id=<id>`, the work page.
        """
        return urlparse(url).path == SERIES_PATH and "manga_id" in _ids(url)

    def series_urls(self, url: str) -> list[str]:
        """List every episode a work page links to, oldest first.

        Args:
            url: A work URL.

        Returns:
            One canonical episode URL per listed story, by ascending story id.

        Raises:
            UnsupportedUrlError: The URL is not a work page.
            NotAnEpisodePageError: The work is unknown or lists no episode.
        """
        if not self.is_series(url):
            msg = f"{url} is not a work page."
            raise UnsupportedUrlError(msg)
        manga_id = _ids(url)["manga_id"]

        res = self._get(series_url(manga_id))
        soup = BeautifulSoup(res.content, "html.parser")
        story_ids: set[int] = set()
        for anchor in soup.find_all("a", href=True):
            if not isinstance(anchor, Tag):
                continue
            href = urljoin(res.url or url, str(anchor["href"]))
            ids = _ids(href)
            if urlparse(href).path == EPISODE_PATH and ids.get("manga_id") == manga_id and "story_id" in ids:
                story_ids.add(int(ids["story_id"]))
        if not story_ids:
            msg = f"the work at {url} lists no episode."
            raise NotAnEpisodePageError(msg)
        return [episode_url(manga_id, str(story_id)) for story_id in sorted(story_ids)]

    def episode(self, url: str) -> Episode:
        """Read one episode: its titles, its page images and what follows it.

        Args:
            url: A reading page URL; a page number in it is ignored.

        Returns:
            The episode, with one page per reading page walked.

        Raises:
            NotAnEpisodePageError: The story was taken down or the URL names none.
        """
        ids = _ids(url)
        if urlparse(url).path != EPISODE_PATH or "manga_id" not in ids or "story_id" not in ids:
            msg = f"{url} is not a reading page."
            raise NotAnEpisodePageError(msg)
        manga_id, story_id = ids["manga_id"], ids["story_id"]
        first_url = episode_url(manga_id, story_id)

        first = _ReadingPage(self._get(first_url), first_url)
        if first.heading is None:
            msg = f"no story at {url}; it was taken down or never existed."
            raise NotAnEpisodePageError(msg)

        image_urls: list[str] = []
        seen = {first_url}
        page = first
        while True:
            if page.image_url:
                image_urls.append(page.image_url)
            if page.next_page is None or page.next_page in seen or len(seen) >= MAX_PAGES:
                break
            seen.add(page.next_page)
            page = _ReadingPage(self._get(page.next_page), page.next_page)

        match = _STORY_TITLE.match(first.heading)
        series_title = first.og_title or (match["series"] if match and match["series"] else "") or manga_id
        episode_title = (match["title"] if match else "") or first.heading or story_id

        return Episode(
            url=first_url,
            series_title=series_title,
            episode_title=episode_title,
            pages=tuple(Page(url=src) for src in image_urls),
            next_url=page.next_story,
            metadata={
                "manga_id": manga_id,
                "story_id": story_id,
                "heading": first.heading,
                "series_url": series_url(manga_id),
                "prev_url": first.prev_story,
                "page_count": len(seen),
                "images": image_urls,
            },
        )


class _ReadingPage:
    """What one `mag_detail.php` page says: its image, its heading and where its buttons go."""

    def __init__(self, res: Response, url: str) -> None:
        base = res.url or url
        soup = BeautifulSoup(res.content, "html.parser")

        heading = soup.find("h3", id="story_title")
        self.heading = heading.get_text(strip=True) if isinstance(heading, Tag) else None

        og_title = soup.find("meta", property="og:title")
        content = str(og_title["content"]) if isinstance(og_title, Tag) and og_title.get("content") else ""
        self.og_title = content.removesuffix(_SITE_SUFFIX).strip()

        self.image_url: str | None = None
        area = soup.find("div", class_="magarea")
        img = area.find("img", src=True) if isinstance(area, Tag) else None
        if isinstance(img, Tag):
            self.image_url = urljoin(base, str(img["src"]))

        self.next_page = _button(soup, "next-page-btn", base)
        self.next_story = _button(soup, "after-story-btn", base)
        self.prev_story = _button(soup, "before-story-btn", base)


def _button(soup: BeautifulSoup, element_id: str, base: str) -> str | None:
    """Where the `<li id=...>` button links to, or None when it is absent or greyed out."""
    item = soup.find("li", id=element_id)
    anchor = item.find("a", href=True) if isinstance(item, Tag) else None
    if not isinstance(anchor, Tag):
        return None
    href = urljoin(base, str(anchor["href"]))
    ids = _ids(href)
    if "manga_id" not in ids or "story_id" not in ids:
        return href
    canonical = episode_url(ids["manga_id"], ids["story_id"])
    # Page 1 is the story URL itself, so a link back to it is seen as such.
    if ids.get("page_no", "1") == "1":
        return canonical
    return f"{canonical}&page_no={ids['page_no']}"
