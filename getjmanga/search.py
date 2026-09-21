"""`-s`: the links on a web page that some extractor takes."""

from __future__ import annotations

import re
from itertools import count
from typing import TYPE_CHECKING
from urllib.parse import urldefrag, urljoin

from bs4 import BeautifulSoup

from .extractors import EXTRACTORS
from .session import HEADERS

if TYPE_CHECKING:
    from collections.abc import Iterator

    from httpx import Client

    from .extractor import Extractor

#: Seconds to wait for the page.
TIMEOUT = 30

#: `[1-3]` or `[1-]` in a page URL: the pages numbered 1 to 3, or from 1 on.
_RANGE = re.compile(r"\[(?P<start>[0-9]+)-(?P<stop>[0-9]*)\]")


def numbered_pages(url: str) -> tuple[Iterator[str], bool] | None:
    """Expand a `[1-3]` or `[1-]` in a page URL into the pages it stands for.

    Args:
        url: The page URL, with at most one range in it.

    Returns:
        None when the URL has no range. Else the page URLs in order, and
        whether the range is open-ended, in which case the pages never run
        out and the caller stops at the first one with nothing new on it.
        A start with a leading zero keeps its width: `[01-]` is 01, 02, ...
    """
    match = _RANGE.search(url)
    if match is None:
        return None
    start, stop = match["start"], match["stop"]
    width = len(start) if start.startswith("0") else 0
    numbers = count(int(start)) if not stop else range(int(start), int(stop) + 1)
    return (url[: match.start()] + f"{n:0{width}d}" + url[match.end() :] for n in numbers), not stop


def downloadable_links(html: str | bytes, base_url: str, extractor: type[Extractor] | None = None) -> list[str]:
    """Pick the `<a href>` links out of a page that an extractor takes.

    Args:
        html: The page.
        base_url: What relative links are resolved against; the page's own URL is left out.
        extractor: Keep only the links this extractor takes, instead of any extractor's.

    Returns:
        The links in page order, each once, fragments dropped.
    """
    takers = (extractor,) if extractor is not None else EXTRACTORS
    page = urldefrag(base_url).url
    links: list[str] = []
    for anchor in BeautifulSoup(html, "html.parser").find_all("a", href=True):
        href = anchor.get("href")
        if not isinstance(href, str):
            continue
        link = urldefrag(urljoin(base_url, href.strip())).url
        if link != page and link not in links and any(taker.suitable(link) for taker in takers):
            links.append(link)
    return links


def search(session: Client, url: str, extractor: type[Extractor] | None = None) -> list[str]:
    """Fetch `url` and pick the links out of it that an extractor takes.

    Args:
        session: The client to fetch the page with.
        url: The page to scan.
        extractor: Keep only the links this extractor takes, instead of any extractor's.

    Returns:
        What `downloadable_links()` finds, relative links resolved against
        where the page ended up after redirects.

    Raises:
        httpx.HTTPError: The page could not be fetched.
    """
    res = session.get(url, headers=HEADERS, timeout=TIMEOUT)
    res.raise_for_status()
    return downloadable_links(res.content, str(res.url), extractor)
