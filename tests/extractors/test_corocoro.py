from __future__ import annotations

from http import HTTPStatus
from io import BytesIO

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from PIL import Image

from getjmanga.downloader import Downloader
from getjmanga.extractors.common import NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractors.corocoro import (
    API_URL,
    BASE_URL,
    Corocoro,
    endpoint,
)
from getjmanga.protobuf import encode_bytes_field, encode_varint_field

KEY = "1274acbd105b7cd78f3ed24053ee7c5c516bfb70a5e2145646c4458b94ce7e8a"
IV = "40db84c9d7d4485a9685906c2a641c84"

TITLE_URL = f"{BASE_URL}/title/1186"
CHAPTER_URL = f"{BASE_URL}/chapter/51001/viewer"
SECOND_URL = f"{BASE_URL}/chapter/51043/viewer"
LOCKED_URL = f"{BASE_URL}/chapter/50845/viewer"
CDN = "https://img.www.corocoro.jp/chapter_page/51001"
FRONT_PAGE_HTML = '<!DOCTYPE html><html lang="ja"><head><meta charSet="utf-8"/></head><body>corocoro</body></html>'


# --- the site's messages, built from the field numbers of its protobufjs classes ------


def chapter_message(chapter_id, main_name, sub_name="", *, badge=2, cost=None):
    fields = encode_varint_field(1, chapter_id) + encode_bytes_field(2, main_name)
    if sub_name:
        fields += encode_bytes_field(3, sub_name)
    if cost is not None:
        kind, amount = cost
        fields += encode_bytes_field(5, encode_varint_field(1, kind) + encode_varint_field(2, amount))
    return fields + encode_varint_field(11, badge)


def title_message(title_id=1186, name="スーパーフィッシング グランダー武蔵", *, reversed_list=True):
    fields = encode_varint_field(1, title_id) + encode_bytes_field(3, name)
    return fields + encode_varint_field(14, int(reversed_list))


def image_message(src, width=1414, height=2048):
    return encode_bytes_field(1, src) + encode_varint_field(2, height) + encode_varint_field(3, width)


CHAPTERS = [
    chapter_message(51001, "第1話"),
    chapter_message(51043, "第2話"),
    chapter_message(50845, "最終回", badge=4, cost=(2, 60)),
]


def viewer_message(*, pages=None, current=CHAPTERS[0], prev=None, following=CHAPTERS[1], result=0, key=KEY, iv=IV):
    """A `Proto.ViewerView`, the answer to `chapter/viewer`."""
    if pages is None:
        pages = [image_message(f"{CDN}/1.webp.enc?h=a&e=1"), image_message(f"{CDN}/2.webp.enc?h=b&e=1")]
    body = b"".join(encode_bytes_field(2, page) for page in pages)
    body += encode_bytes_field(5, current)
    if prev is not None:
        body += encode_bytes_field(6, prev)
    if following is not None:
        body += encode_bytes_field(7, following)
    body += encode_varint_field(8, 1) + encode_varint_field(9, 0)
    body += b"".join(encode_bytes_field(10, entry) for entry in CHAPTERS)
    body += encode_bytes_field(15, title_message())
    body += encode_bytes_field(17, encode_varint_field(1, 7) + encode_bytes_field(2, "てしろぎたかし"))
    if key:
        body += encode_bytes_field(19, key)
    if iv:
        body += encode_bytes_field(20, iv)
    return body + encode_varint_field(21, result)


def detail_message(chapters=CHAPTERS, *, reversed_list=True):
    """A `Proto.TitleDetailView`, the answer to `title/detail`."""
    body = encode_bytes_field(2, title_message(reversed_list=reversed_list))
    body += encode_bytes_field(3, encode_bytes_field(2, "てしろぎたかし"))
    return body + b"".join(encode_bytes_field(8, entry) for entry in chapters)


def encrypted_webp(size=(8, 8), colour=(10, 20, 30)):
    raw = BytesIO()
    Image.new("RGB", size, colour).save(raw, "WEBP", lossless=True)
    plain = raw.getvalue()
    padding = 16 - len(plain) % 16
    plain += bytes([padding]) * padding
    encryptor = Cipher(algorithms.AES(bytes.fromhex(KEY)), modes.CBC(bytes.fromhex(IV))).encryptor()
    return encryptor.update(plain) + encryptor.finalize()


@pytest.fixture
def client(fake_session, fake_response):
    """A `Corocoro` on a scripted session; the viewer is asked with PUT."""

    def make(routes):
        # The API answers protobuf with no content type; a bytes route stands for one such answer.
        scripted = {
            needle: fake_response(body, content_type="application/octet-stream") if isinstance(body, bytes) else body
            for needle, body in routes.items()
        }
        session = fake_session(scripted)
        return Corocoro(session), session

    return make


# --- URLs ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (CHAPTER_URL, True),
        (f"{CHAPTER_URL}/", True),
        (TITLE_URL, True),
        (f"{TITLE_URL}/", True),
        ("http://www.corocoro.jp/chapter/51001/viewer", False),
        ("https://corocoro.jp/chapter/51001/viewer", False),
        ("https://www.corocoro.jp/chapter/51001", False),
        ("https://www.corocoro.jp/rensai", False),
        ("https://www.corocoro.jp/title/1186/comments", False),
        ("https://comic-fuz.com/manga/viewer/79232", False),
    ],
)
def test_suitable(url, expected):
    assert Corocoro.suitable(url) is expected


def test_is_series_tells_a_title_page_from_a_chapter():
    assert Corocoro.is_series(TITLE_URL)
    assert not Corocoro.is_series(CHAPTER_URL)


# --- message readers -----------------------------------------------------------------


# --- episode() -----------------------------------------------------------------------


def test_episode_reads_the_titles_the_pages_and_the_next_chapter(client, fake_response):
    corocoro, session = client({"rq=chapter/viewer": viewer_message()})
    episode = corocoro.episode(CHAPTER_URL)

    assert episode.url == CHAPTER_URL
    assert episode.series_title == "スーパーフィッシング グランダー武蔵"
    assert episode.episode_title == "第1話"
    assert [page.url for page in episode.pages] == [f"{CDN}/1.webp.enc?h=a&e=1", f"{CDN}/2.webp.enc?h=b&e=1"]
    assert episode.pages[0].extra == {"key": KEY, "iv": IV}
    assert (episode.pages[0].width, episode.pages[0].height) == (1414, 2048)
    assert episode.next_url == SECOND_URL
    assert episode.metadata["chapter"]["badge"] == "free"
    assert episode.metadata["authors"] == [{"name": "てしろぎたかし", "role": ""}]
    assert [entry["id"] for entry in episode.metadata["chapters"]] == [51001, 51043, 50845]

    url, params = session.puts[0]
    assert url == endpoint("chapter/viewer") == f"{API_URL}?rq=chapter/viewer"
    assert params == {"chapter_id": 51001, "use_ticket": 0, "event_point": 0, "paid_point": 0}
    assert session.headers_seen[-1]["Referer"] == CHAPTER_URL


def test_episode_takes_the_query_string_off_the_url(client, fake_response):
    corocoro, session = client({"rq=chapter/viewer": viewer_message()})
    episode = corocoro.episode(f"{CHAPTER_URL}/?from=list")
    assert episode.url == CHAPTER_URL
    assert session.puts[0][1]["chapter_id"] == 51001


def test_episode_is_locked_when_the_site_says_so(client, fake_response):
    answer = viewer_message(pages=[], current=CHAPTERS[2], prev=CHAPTERS[1], following=None, result=1, key="", iv="")
    corocoro, _ = client({"rq=chapter/viewer": answer})
    episode = corocoro.episode(LOCKED_URL)

    assert episode.pages == ()
    assert not episode.readable
    assert episode.episode_title == "最終回"
    assert episode.next_url is None
    assert episode.metadata["result"] == 1
    assert episode.metadata["chapter"] == {
        "id": 50845,
        "main_name": "最終回",
        "sub_name": "",
        "badge": "premium",
        "point_consumption": {"type": 2, "amount": 60},
    }
    assert episode.metadata["prev_chapter"]["id"] == 51043
    assert episode.metadata["next_chapter"] is None


def test_episode_lists_no_page_when_the_result_is_not_success(client, fake_response):
    # A page list with an error result is not trusted: the viewer would not decrypt it either.
    corocoro, _ = client({"rq=chapter/viewer": viewer_message(result=2)})
    episode = corocoro.episode(CHAPTER_URL)
    assert episode.pages == ()
    assert episode.next_url == SECOND_URL


@pytest.mark.parametrize(
    "answer",
    [
        # The site's front page, a 404, and an empty message.
        {"text": FRONT_PAGE_HTML, "content_type": "text/html; charset=utf-8"},
        {"content": b"", "status_code": HTTPStatus.NOT_FOUND},
        {"content": b""},
    ],
)
def test_episode_raises_when_the_site_names_no_chapter(client, fake_response, answer):
    corocoro, _ = client({"rq=chapter/viewer": fake_response(**answer)})
    with pytest.raises(NotAnEpisodePageError):
        corocoro.episode(f"{BASE_URL}/chapter/999999999/viewer")


def test_episode_rejects_a_title_url(client):
    corocoro, _ = client({})
    with pytest.raises(UnsupportedUrlError):
        corocoro.episode(TITLE_URL)


# --- series_urls() -------------------------------------------------------------------


def test_series_urls_keeps_an_oldest_first_listing(client, fake_response):
    corocoro, session = client({"rq=title/detail": detail_message()})
    assert corocoro.series_urls(TITLE_URL) == [CHAPTER_URL, SECOND_URL, LOCKED_URL]
    assert session.calls[-1] == f"{API_URL}?rq=title/detail"
    assert session.params_seen[-1] == {"title_id": "1186"}
    assert session.headers_seen[-1]["Referer"] == TITLE_URL


def test_series_urls_flips_a_newest_first_listing(client, fake_response):
    listing = detail_message(list(reversed(CHAPTERS)), reversed_list=False)
    corocoro, _ = client({"rq=title/detail": listing})
    assert corocoro.series_urls(TITLE_URL) == [CHAPTER_URL, SECOND_URL, LOCKED_URL]


def test_series_urls_deduplicates(client, fake_response):
    corocoro, _ = client({"rq=title/detail": detail_message([CHAPTERS[0], CHAPTERS[0], CHAPTERS[1]])})
    assert corocoro.series_urls(TITLE_URL) == [CHAPTER_URL, SECOND_URL]


def test_series_urls_raises_on_an_empty_listing(client, fake_response):
    corocoro, _ = client({"rq=title/detail": detail_message([])})
    with pytest.raises(NotAnEpisodePageError, match="no chapter"):
        corocoro.series_urls(TITLE_URL)


def test_series_urls_raises_on_an_unknown_title(client, fake_response):
    corocoro, _ = client({"rq=title/detail": fake_response(b"", status_code=HTTPStatus.NOT_FOUND)})
    with pytest.raises(NotAnEpisodePageError, match="no title"):
        corocoro.series_urls(f"{BASE_URL}/title/1")


def test_series_urls_rejects_a_chapter_url(client):
    corocoro, _ = client({})
    with pytest.raises(UnsupportedUrlError):
        corocoro.series_urls(CHAPTER_URL)


# --- images --------------------------------------------------------------------------


def test_image_decrypts_a_page(client, fake_response):
    corocoro, session = client(
        {
            "rq=chapter/viewer": viewer_message(),
            "img.www.corocoro.jp": fake_response(encrypted_webp(), content_type="application/octet-stream"),
        },
    )
    episode = corocoro.episode(CHAPTER_URL)
    image = corocoro.image(episode.pages[0], episode)

    assert image.size == (8, 8)
    assert image.convert("RGB").getpixel((0, 0)) == (10, 20, 30)
    assert session.calls[-1] == f"{CDN}/1.webp.enc?h=a&e=1"
    assert session.headers_seen[-1]["Referer"] == CHAPTER_URL


def test_image_leaves_a_plain_page_alone(client, fake_response):
    raw = BytesIO()
    Image.new("RGB", (8, 8), (1, 2, 3)).save(raw, "PNG")
    corocoro, _ = client(
        {
            "rq=chapter/viewer": viewer_message(key="", iv=""),
            "img.www.corocoro.jp": fake_response(raw.getvalue()),
        },
    )
    episode = corocoro.episode(CHAPTER_URL)
    assert episode.pages[0].extra == {"key": "", "iv": ""}
    assert corocoro.image(episode.pages[0], episode).getpixel((0, 0)) == (1, 2, 3)


# --- the real site --------------------------------------------------------------------

# One chapter per known host, free to read without an account.
TEST_URLS: dict[str, str] = {
    "www.corocoro.jp": CHAPTER_URL,
}


@pytest.mark.network
@pytest.mark.parametrize("host", TEST_URLS)
def test_site_download(tmp_path, host):
    result = Downloader(Corocoro(), tmp_path, only_first=True).download(TEST_URLS[host])
    assert result.status == "saved"
    assert (result.save_dir / "0.jpg").stat().st_size > 0


@pytest.mark.network
def test_title_page_lists_chapters_oldest_first():
    corocoro = Corocoro()
    urls = corocoro.series_urls(TITLE_URL)
    assert urls[0] == CHAPTER_URL
    assert all(Corocoro.suitable(url) for url in urls)
    # The first listed chapter has no predecessor, whichever way the site lists them.
    assert corocoro.episode(urls[0]).metadata["prev_chapter"] is None


@pytest.mark.network
def test_locked_chapter_returns_without_pages():
    episode = Corocoro().episode(LOCKED_URL)
    assert episode.pages == ()
    assert episode.metadata["chapter"]["badge"] == "premium"
