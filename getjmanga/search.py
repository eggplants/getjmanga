"""`-s`: the links on a web page that some extractor takes."""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import urldefrag, urljoin

from bs4 import BeautifulSoup

from .extractors import EXTRACTORS
from .session import HEADERS

if TYPE_CHECKING:
    from httpx import Client

    from .extractor import Extractor

#: Seconds to wait for the page.
TIMEOUT = 30


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
