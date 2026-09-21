"""ガッコミ (Gakken), a WordPress site whose episodes open in Keyring's BookEnd EPUB viewer.

A work page (`/comic/page-<slug>/`) lists its episodes as `div.pg-book-episode`
blocks, each with a WordPress post id (`id="episode-<id>"`), newest first
unless asked for `?sort=asc`. An episode is one of two kinds:

- a BookEnd episode: the block holds a form posting to
  `/viewer/?content_id=<id>`. The viewer page carries a one-time token which
  the license server (`license.keyring.net`) exchanges -- after a
  Diffie-Hellman handshake, the request and the answer AES-encrypted under
  the shared key -- for the hex key of a fixed-layout EPUB on
  `cloud-library.keyring.net`. Its spine is one XHTML per page, each stored
  in a `KREPUBB` container (RC4 under the key, or inverted bytes) around an
  SVG with the page image inlined as base64. The CDN checks nothing.
- a one-image episode: the block holds the image itself, which the site
  shows in a modal. Plain S3 uploads, nothing to undo.

The site has no episode URL of its own, so the extractor makes one from the
work page and the block id, `/comic/page-<slug>/#episode-<id>`, which names
the titles and the next episode. A bare `/viewer/?content_id=<id>` is taken
too; its titles then come from the EPUB's own title, split at its last space,
unless a work page seen earlier in the run lists the content id.

Every episode is free; no purchase, no wait, no account. The license server
refusing a token (`Status` other than 0) is the one way an episode is not
readable, and comes back as an episode without pages.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
from dataclasses import dataclass
from io import BytesIO
from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import parse_qs, unquote, urlencode, urljoin, urlparse

from bs4 import BeautifulSoup
from bs4.element import Tag
from cryptography.hazmat.decrepit.ciphers.algorithms import ARC4
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from PIL import Image

from getjmanga.errors import GetjmangaError, NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractor import Episode, Extractor, Page

if TYPE_CHECKING:
    from collections.abc import Mapping

    from httpx2 import Client

BASE_URL = "https://gakcomic.gakken.jp"
LICENSE_URL = "https://license.keyring.net"
LIBRARY_URL = "https://cloud-library.keyring.net"

#: The site's BookEnd API key, as `viewer/custom.js` sets it; read from there when it can be.
_FALLBACK_API_KEY = "a3yDun7GZRcMAZsi9RyFR4CmiVkPHRCy"

_WORK_PATH = re.compile(r"^/comic/(?P<slug>page-[^/]+)/?$")
_VIEWER_PATH = re.compile(r"^/viewer/?$")
_EPISODE_FRAGMENT = re.compile(r"^episode-(?P<id>\d+)$")
_CONTENT_ID = re.compile(r"^[0-9a-f]{32}$")

_INIT_CONTENT = re.compile(r'init_content\("(?P<token>[^"]*)"\)')
_API_KEY = re.compile(r"'apiKey':\s*'(?P<key>[^']+)'")
_TITLE_SUFFIX = re.compile(r"\s*\|\s*ガッコミ\s*$")
#: `<series> <episode>`: how the EPUB titles are written, e.g. `うまくなる卓球　第１章`.
_EPUB_TITLE = re.compile(r"^(?P<series>.+?)\s+(?P<episode>\S+)$")

_ROOTFILE = re.compile(r'<rootfile[^>]*full-path="(?P<path>[^"]+)"')
_ITEM = re.compile(r"<item\s[^>]*>")
_ATTRIBUTE = re.compile(r'(?P<name>[a-z-]+)="(?P<value>[^"]*)"')
_ITEMREF = re.compile(r'<itemref[^>]*idref="(?P<id>[^"]+)"')
_RESOLUTION = re.compile(r'name="original-resolution"\s+content="(?P<width>\d+)x(?P<height>\d+)"')
_INLINE_IMAGE = re.compile(rb'(?:xlink:href|src)="data:image/[a-z]+;base64,(?P<data>[^"]+)"')

# ---------------------------------------------------------------------------
# The BookEnd handshake: RFC 3526 group 14 Diffie-Hellman, then AES-256-CBC
# under the first 256 bits of the shared secret, the key doubling as the IV.
# ---------------------------------------------------------------------------

_DH_PRIME = int(
    "FFFFFFFFFFFFFFFFC90FDAA22168C234C4C6628B80DC1CD129024E088A67CC74020BBEA63B139B22514A08798E3404DD"
    "EF9519B3CD3A431B302B0A6DF25F14374FE1356D6D51C245E485B576625E7EC6F44C42E9A637ED6B0BFF5CB6F406B7ED"
    "EE386BFB5A899FA5AE9F24117C4B1FE649286651ECE45B3DC2007CB8A163BF0598DA48361C55D39A69163FA8FD24CF5F"
    "83655D23DCA3AD961C62F356208552BB9ED529077096966D670C354E4ABC9804F1746C08CA18217C32905E462E36CE3B"
    "E39E772C180E86039B2783A2EC07A28FB5C55DF06F4C52C9DE2BCBF6955817183995497CEA956AE515D2261898FA0510"
    "15728E5A8AACAA68FFFFFFFFFFFFFFFF",
    16,
)
_DH_GENERATOR = 2
#: The viewer draws its private exponent as 100 random decimal digits.
_PRIVATE_DIGITS = 100
_KEY_BITS = 256
_KEY_HEX_LENGTH = _KEY_BITS // 4
_IV_LENGTH = 16

_CONTAINER_MAGIC = b"KREPUBB"
_CONTAINER_RC4_EMBEDDED_KEY = 1
_CONTAINER_INVERTED = 2
_CONTAINER_RC4_CONTENT_KEY = 3
_RC4_KEY_LENGTH = 16


def dh_public(private: int) -> str:
    """The public value to send for a private exponent.

    Args:
        private: The private exponent.

    Returns:
        `g ** private mod p`, as hex without leading zeros, the way the viewer writes it.
    """
    return format(pow(_DH_GENERATOR, private, _DH_PRIME), "x")


def dh_shared_key(peer_public: str, private: int) -> str:
    """The AES key both sides derive from the exchange.

    Args:
        peer_public: The other side's public value, hex.
        private: This side's private exponent.

    Returns:
        The shared secret as hex without leading zeros, cut to 64 characters (256 bits).
    """
    return format(pow(int(peer_public, 16), private, _DH_PRIME), "x")[:_KEY_HEX_LENGTH]


def key_hash(key: str) -> str:
    """What the license server sends as `H`, so the client can check its key.

    Args:
        key: The shared key, hex.

    Returns:
        The MD5 of the hex string.
    """
    return hashlib.md5(key.encode(), usedforsecurity=False).hexdigest()


def aes_encrypt(key: str, data: bytes) -> bytes:
    """AES-256-CBC with PKCS#7 padding, the IV being the first block of the key.

    Args:
        key: The shared key, hex.
        data: What to encrypt.

    Returns:
        The ciphertext.
    """
    raw = bytes.fromhex(key)
    padder = padding.PKCS7(algorithms.AES.block_size).padder()
    encryptor = Cipher(algorithms.AES(raw), modes.CBC(raw[:_IV_LENGTH])).encryptor()
    return encryptor.update(padder.update(data) + padder.finalize()) + encryptor.finalize()


def aes_decrypt(key: str, data: bytes) -> bytes:
    """Undo `aes_encrypt()`.

    Args:
        key: The shared key, hex.
        data: The ciphertext.

    Returns:
        The plaintext.

    Raises:
        GetjmangaError: The data is not a whole number of blocks, or the padding is off (wrong key).
    """
    raw = bytes.fromhex(key)
    if not data or len(data) % _IV_LENGTH:
        msg = "the license server's answer is not a whole number of AES blocks."
        raise GetjmangaError(msg)
    decryptor = Cipher(algorithms.AES(raw), modes.CBC(raw[:_IV_LENGTH])).decryptor()
    unpadder = padding.PKCS7(algorithms.AES.block_size).unpadder()
    try:
        return unpadder.update(decryptor.update(data) + decryptor.finalize()) + unpadder.finalize()
    except ValueError as error:
        msg = "the license server's answer carries no PKCS#7 padding; wrong session key?"
        raise GetjmangaError(msg) from error


def rc4(key: bytes, data: bytes) -> bytes:
    """RC4, which the EPUB pages are stored under.

    Args:
        key: The key, 16 bytes here.
        data: What to (de)crypt; RC4 is its own inverse.

    Returns:
        The data XORed with the keystream.
    """
    decryptor = Cipher(ARC4(key), mode=None).decryptor()
    return decryptor.update(data) + decryptor.finalize()


def decode_container(blob: bytes, key: str) -> bytes:
    """Unwrap a `KREPUBB` container, or hand back a file that is none.

    Args:
        blob: The file as the CDN serves it.
        key: The content's hex key from the license server.

    Returns:
        The file's plain content.

    Raises:
        GetjmangaError: The container is of a kind the viewer does not know either.
    """
    if not blob.startswith(_CONTAINER_MAGIC):
        return blob
    kind, body = blob[len(_CONTAINER_MAGIC)], blob[len(_CONTAINER_MAGIC) + 1 :]
    if kind == _CONTAINER_RC4_EMBEDDED_KEY:
        return rc4(body[:_RC4_KEY_LENGTH], body[_RC4_KEY_LENGTH:])
    if kind == _CONTAINER_INVERTED:
        return bytes(byte ^ 0xFF for byte in body)
    if kind == _CONTAINER_RC4_CONTENT_KEY:
        return rc4(bytes.fromhex(key[: _RC4_KEY_LENGTH * 2]), body)
    msg = f"unknown KREPUBB container type {kind}."
    raise GetjmangaError(msg)


def inline_image(xhtml: bytes) -> bytes:
    """The page image an EPUB page inlines as a data URI.

    Args:
        xhtml: The decoded page document.

    Returns:
        The image file.

    Raises:
        GetjmangaError: The page inlines no image.
    """
    match = _INLINE_IMAGE.search(xhtml)
    if match is None:
        msg = "the EPUB page inlines no image."
        raise GetjmangaError(msg)
    return base64.b64decode(match["data"])


# ---------------------------------------------------------------------------
# The site
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Item:
    """One episode block of a work page."""

    #: The block's WordPress post id, the `#episode-<id>` of its URL.
    id: int
    title: str
    #: The BookEnd content id, for an episode the viewer opens.
    content_id: str = ""
    #: The image, for a one-image episode the site shows in a modal.
    image: str = ""


@dataclass(frozen=True)
class Work:
    """A work page, its episodes in reading order."""

    url: str
    title: str
    items: tuple[Item, ...]
    #: The `編著者` entries, as the page writes them, the role in fullwidth parentheses after each name.
    writer: str = ""

    def episode_url(self, item: Item) -> str:
        """The URL the extractor gives an episode of the work."""
        return f"{self.url}#episode-{item.id}"

    def find(self, *, item_id: int | None = None, content_id: str = "") -> int | None:
        """The position of an episode, by post id or by content id."""
        for index, item in enumerate(self.items):
            if item.id == item_id or (content_id and item.content_id == content_id):
                return index
        return None

    def prev_url(self, index: int) -> str | None:
        """The URL of the episode before the one at `index`, None at the start."""
        return self.episode_url(self.items[index - 1]) if index else None

    def next_url(self, index: int) -> str | None:
        """The URL of the episode after the one at `index`, None at the end."""
        return self.episode_url(self.items[index + 1]) if index + 1 < len(self.items) else None


@dataclass(frozen=True)
class Content:
    """What the license server said about a content id."""

    #: The hex key the EPUB is encrypted under; empty when the token was refused.
    key: str
    #: `ContentsInfo` without the key, or the refusal's `Status` and `StatusDescription`.
    info: dict[str, Any]

    @property
    def readable(self) -> bool:
        """Whether the server handed the key over."""
        return bool(self.key)


class Gakcomic(Extractor):
    """Fetch episodes from ガッコミ."""

    NAME = "gakcomic"
    HOSTS = ("gakcomic.gakken.jp",)
    PUBLISHER = "Gakken"
    URL_FORMS = (
        "https://gakcomic.gakken.jp/comic/page-<slug>/#episode-<id>",
        "https://gakcomic.gakken.jp/viewer/?content_id=<content-id>",
        "https://gakcomic.gakken.jp/comic/page-<slug>/",
    )
    #: The license server is called cross-origin from the viewer page.
    LICENSE_HEADERS: ClassVar[dict[str, str]] = {
        **Extractor.HEADERS,
        "Origin": BASE_URL,
        "Referer": f"{BASE_URL}/viewer/",
    }

    def __init__(self, session: Client | None = None) -> None:
        """Build an extractor.

        Args:
            session: A session to reuse. A retrying one is made when omitted.
        """
        super().__init__(session)
        #: Work pages read so far, by URL: what names an episode and its successor.
        self._works: dict[str, Work] = {}
        self._api_key = ""

    @classmethod
    def suitable(cls, url: str) -> bool:
        """Report whether `url` is a work page, an episode of one, or a viewer URL.

        Args:
            url: The URL to check.

        Returns:
            True for `/comic/page-<slug>/` with or without `#episode-<id>`, and `/viewer/?content_id=<id>`.
        """
        if not super().suitable(url):
            return False
        parsed = urlparse(url)
        if _WORK_PATH.match(parsed.path):
            return not parsed.fragment or _EPISODE_FRAGMENT.match(parsed.fragment) is not None
        return _VIEWER_PATH.match(parsed.path) is not None and bool(_content_id(url))

    @classmethod
    def is_series(cls, url: str) -> bool:
        """Report whether `url` is a work page.

        Args:
            url: The URL to check.

        Returns:
            True for `/comic/page-<slug>/` without an episode fragment.
        """
        parsed = urlparse(url)
        return cls.suitable(url) and _WORK_PATH.match(parsed.path) is not None and not parsed.fragment

    def series_urls(self, url: str) -> list[str]:
        """List every episode of a work, oldest first.

        Args:
            url: A work page URL.

        Returns:
            One `/comic/page-<slug>/#episode-<id>` per listed episode.

        Raises:
            UnsupportedUrlError: The URL is no work page.
            NotAnEpisodePageError: The work lists no episode.
        """
        if not self.is_series(url):
            msg = f"{url} is not a work page."
            raise UnsupportedUrlError(msg)
        work = self._work(url)
        if not work.items:
            msg = f"the work at {url} lists no episode."
            raise NotAnEpisodePageError(msg)
        return [work.episode_url(item) for item in work.items]

    def episode(self, url: str) -> Episode:
        """Read one episode: its titles, its pages and the episode after it.

        Args:
            url: An episode URL of either form.

        Returns:
            The episode. `pages` is empty when the license server refused the token.

        Raises:
            UnsupportedUrlError: The URL is a work page, or no URL of the site.
            NotAnEpisodePageError: The work page lists no such episode, or the content id is unknown.
        """
        parsed = urlparse(url)
        if _VIEWER_PATH.match(parsed.path) and _content_id(url):
            return self._viewer_episode(url, _content_id(url))
        fragment = _EPISODE_FRAGMENT.match(parsed.fragment) if _WORK_PATH.match(parsed.path) else None
        if fragment is None:
            msg = f"{url} is not an episode url."
            raise UnsupportedUrlError(msg)
        work = self._work(url)
        index = work.find(item_id=int(fragment["id"]))
        if index is None:
            msg = f"the work at {work.url} lists no episode {fragment['id']}."
            raise NotAnEpisodePageError(msg)
        item = work.items[index]
        metadata: dict[str, Any] = {"episode_id": item.id, "work_url": work.url, "work_title": work.title}
        if not item.content_id:
            metadata["image"] = item.image
            return self._dated_by_upload(
                Episode(
                    url=url,
                    series_title=work.title,
                    episode_title=item.title,
                    pages=(Page(url=item.image),),
                    prev_url=work.prev_url(index),
                    next_url=work.next_url(index),
                    metadata=metadata,
                    writer=work.writer,
                    publisher=self.PUBLISHER,
                    number=index + 1,
                )
            )
        content = self._open(item.content_id)
        return self._dated_by_upload(
            Episode(
                url=url,
                series_title=work.title,
                episode_title=item.title,
                pages=self._pages(item.content_id, content),
                prev_url=work.prev_url(index),
                next_url=work.next_url(index),
                metadata={**metadata, "content_id": item.content_id, **content.info},
                writer=work.writer,
                publisher=self.PUBLISHER,
                number=index + 1,
            )
        )

    def image(self, page: Page, episode: Episode) -> Image.Image:
        """Fetch one page: an EPUB page to unwrap and read the image out of, or a plain image.

        Args:
            page: The page to fetch.
            episode: The episode the page belongs to.

        Returns:
            The page image.
        """
        if "key" not in page.extra:
            return super().image(page, episode)
        res = self._get(page.url, timeout=self.IMAGE_TIMEOUT)
        return Image.open(BytesIO(inline_image(decode_container(res.content, str(page.extra["key"])))))

    # --- the work page -----------------------------------------------------------

    def _work(self, url: str) -> Work:
        """The work page `url` is on, read once per run."""
        parsed = urlparse(url)
        work_url = f"{BASE_URL}{parsed.path.rstrip('/')}/"
        if work_url not in self._works:
            res = self._get(work_url, params={"sort": "asc"})
            self._works[work_url] = self._parse_work(work_url, res.text)
        return self._works[work_url]

    @staticmethod
    def _parse_work(url: str, html: str) -> Work:
        soup = BeautifulSoup(html, "html.parser")
        heading = soup.select_one("h1.pg-book-meta__title")
        title = "".join(str(s) for s in heading.find_all(string=True, recursive=False)).strip() if heading else ""
        if not title:
            title = _TITLE_SUFFIX.sub("", soup.title.get_text(strip=True) if soup.title else "")
        items: list[Item] = []
        seen: set[int] = set()
        for block in soup.select("div.pg-book-episode[id]"):
            match = _EPISODE_FRAGMENT.match(str(block["id"]))
            if match is None or int(match["id"]) in seen:
                continue
            seen.add(int(match["id"]))
            link = block.select_one(".pg-book-episode__title")
            thumb = block.select_one("img.pg-book-episode__img[alt]")
            episode_title = (link.get_text(strip=True) if link else "") or (str(thumb["alt"]) if thumb else "")
            form = block.select_one("form[action*='content_id=']")
            image = block.select_one("img.pg-book__4coma-img[src]")
            content_id = _content_id(urljoin(url, str(form["action"]))) if form else ""
            if not content_id and not isinstance(image, Tag):
                continue
            items.append(
                Item(
                    int(match["id"]),
                    episode_title or match["id"],
                    content_id,
                    urljoin(url, str(image["src"])) if isinstance(image, Tag) and not content_id else "",
                ),
            )
        writer = ", ".join(li.get_text(strip=True) for li in soup.select("ul.pg-book-meta__editor-lists li"))
        return Work(url, title, tuple(items), writer)

    # --- the viewer ----------------------------------------------------------------

    def _viewer_episode(self, url: str, content_id: str) -> Episode:
        """An episode given by content id alone: named by a work seen earlier, or by the EPUB's title."""
        content = self._open(content_id)
        metadata: dict[str, Any] = {"content_id": content_id, **content.info}
        for work in self._works.values():
            index = work.find(content_id=content_id)
            if index is not None:
                return self._dated_by_upload(
                    Episode(
                        url=url,
                        series_title=work.title,
                        episode_title=work.items[index].title,
                        pages=self._pages(content_id, content),
                        prev_url=work.prev_url(index),
                        next_url=work.next_url(index),
                        metadata={**metadata, "episode_id": work.items[index].id, "work_url": work.url},
                        writer=work.writer,
                        publisher=self.PUBLISHER,
                        number=index + 1,
                    )
                )
        title = str(content.info.get("title") or content_id)
        match = _EPUB_TITLE.match(title)
        series_title, episode_title = (match["series"], match["episode"]) if match else (title, title)
        return self._dated_by_upload(
            Episode(
                url=url,
                series_title=series_title,
                episode_title=episode_title,
                pages=self._pages(content_id, content),
                metadata=metadata,
                publisher=self.PUBLISHER,
            )
        )

    def _open(self, content_id: str) -> Content:
        """Get a viewer page's one-time token and trade it for the content's key.

        Raises:
            NotAnEpisodePageError: The site issues no token for the content id.
        """
        res = self._get(f"{BASE_URL}/viewer/", params={"content_id": content_id})
        match = _INIT_CONTENT.search(res.text)
        if match is None or not match["token"]:
            msg = f"the viewer opens no content {content_id}."
            raise NotAnEpisodePageError(msg)
        private = self._private_key()
        key, session_id = self._exchange(private)
        query = urlencode(
            {"APIKey": self._key(), "WebSiteHost": BASE_URL, "TokenID": match["token"], "WithKey": "true"},
        )
        answer = self._license(
            "/BookEnd/onetime/token/v1/use",
            {"EncryptedData": base64.b64encode(aes_encrypt(key, query.encode())).decode(), "SessionID": session_id},
        )
        if "EncryptedElement" in answer:
            answer = json.loads(aes_decrypt(key, base64.b64decode(unquote(str(answer["EncryptedElement"])))))
        if answer.get("Status") != 0 or not isinstance(answer.get("ContentsInfo"), dict):
            return Content("", {"status": answer.get("Status"), "status_description": answer.get("StatusDescription")})
        info = dict(answer["ContentsInfo"])
        return Content(str(info.pop("hexkey", "") or ""), info)

    def _exchange(self, private: int) -> tuple[str, str]:
        """The Diffie-Hellman exchange: the shared key and the session id it goes with.

        Raises:
            GetjmangaError: The server's answer is incomplete, or its key hash does not match.
        """
        answer = self._license(
            "/BookEnd/owner/session/key/v1/exchange",
            {"APIKey": self._key(), "WebSiteHost": BASE_URL, "Y": dh_public(private), "K": _KEY_BITS // 128},
        )
        if not all(isinstance(answer.get(field), str) and answer[field] for field in ("Y", "H", "S")):
            msg = f"the license server answered the key exchange with {answer}."
            raise GetjmangaError(msg)
        key = dh_shared_key(answer["Y"], private)
        if key_hash(key) != answer["H"]:
            msg = "the license server's key hash does not match the shared key."
            raise GetjmangaError(msg)
        return key, answer["S"]

    def _license(self, path: str, data: Mapping[str, str | int]) -> dict[str, Any]:
        """POST a form to the license server and read its JSON answer."""
        res = self._session.post(
            f"{LICENSE_URL}{path}",
            data=dict(data),
            headers=self.LICENSE_HEADERS,
            timeout=self.TIMEOUT,
        )
        res.raise_for_status()
        answer = res.json()
        return answer if isinstance(answer, dict) else {}

    def _key(self) -> str:
        """The site's BookEnd API key, read off `viewer/custom.js` once per run."""
        if not self._api_key:
            self._api_key = _FALLBACK_API_KEY
            try:
                match = _API_KEY.search(self._get(f"{BASE_URL}/viewer/custom.js").text)
            except OSError:
                match = None
            if match is not None:
                self._api_key = match["key"]
        return self._api_key

    @staticmethod
    def _private_key() -> int:
        """A private exponent, drawn as the viewer draws it: 100 random decimal digits."""
        return secrets.randbelow(10**_PRIVATE_DIGITS)

    # --- the EPUB ------------------------------------------------------------------

    def _pages(self, content_id: str, content: Content) -> tuple[Page, ...]:
        """The pages of the content's EPUB, from its spine; none when the key was refused."""
        if not content.readable:
            return ()
        base = f"{LIBRARY_URL}/contents/{content_id[:2]}/{content_id}/"
        rootfile = _ROOTFILE.search(self._get(f"{base}META-INF/container.xml").text)
        if rootfile is None:
            msg = f"the EPUB of {content_id} names no package document."
            raise GetjmangaError(msg)
        opf_url = urljoin(base, rootfile["path"])
        opf = decode_container(self._get(opf_url).content, content.key).decode("utf-8", "replace")
        resolution = _RESOLUTION.search(opf)
        width, height = (int(resolution["width"]), int(resolution["height"])) if resolution else (0, 0)
        hrefs: dict[str, str] = {}
        for item in _ITEM.findall(opf):
            attributes = dict(_ATTRIBUTE.findall(item))
            if "id" in attributes and "href" in attributes:
                hrefs[attributes["id"]] = attributes["href"]
        return tuple(
            Page(url=urljoin(opf_url, hrefs[idref]), width=width, height=height, extra={"key": content.key})
            for idref in _ITEMREF.findall(opf)
            if idref in hrefs
        )


def _content_id(url: str) -> str:
    """The `content_id` of a viewer URL, or empty when there is no well-formed one."""
    values = parse_qs(urlparse(url).query).get("content_id", [])
    return values[0] if len(values) == 1 and _CONTENT_ID.match(values[0]) else ""
