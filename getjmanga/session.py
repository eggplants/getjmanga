"""The HTTP session every extractor shares."""

from __future__ import annotations

from requests import Session
from requests.adapters import HTTPAdapter, Retry

#: Headers a browser would send, so sites serve what they serve a browser.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
}


def make_session() -> Session:
    """Build a session that retries transient failures with a backoff.

    Returns:
        A session whose https and http mounts retry up to ten times.
    """
    session = Session()
    adapter = HTTPAdapter(max_retries=Retry(total=10, backoff_factor=1))
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session
