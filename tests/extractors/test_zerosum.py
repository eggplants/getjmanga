from __future__ import annotations

import base64
from http import HTTPStatus
from io import BytesIO

import pytest
from PIL import Image

from getjmanga.downloader import Downloader
from getjmanga.errors import NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractors.zerosum import (
    API_URL,
    BASE_URL,
    ZeroSum,
    decode_viewer,
    decrypt_chapter_id,
    encrypt_chapter_id,
    episode_url,
)
from getjmanga.protobuf import encode_bytes_field, encode_varint_field

TAG = "migawari3"
SERIES_URL = f"{BASE_URL}/detail/{TAG}"
# What the site itself put in a link to chapter 3407 (a random salt, so it differs from ours).
SITE_TOKEN = "U2FsdGVkX1%2FuwOSu522UUh33hE7EBeaYpiUaKX4zN%2BU%3D"
CHAPTER_URL = episode_url(TAG, 3407)

# The series' public chapters as `/title` lists them: newest first, (id, name).
CHAPTERS = [
    (3452, "魔物王子と偽物の聖女　前編"),
    (3425, "続編？知りません。　後編"),
    (3407, "続編？知りません。　前編"),
]


def chapter_message(chapter_id, name):
    return encode_varint_field(1, chapter_id) + encode_bytes_field(2, name) + encode_varint_field(4, 1787886001)


def title_view(
    chapters=CHAPTERS, name="【お試し読み】身代わり花嫁は、旦那様から溺愛されるようです。アンソロジーコミック　3"
):
    title = encode_varint_field(1, 216) + encode_bytes_field(2, TAG) + encode_bytes_field(3, name)
    title += encode_bytes_field(5, "カバーイラスト：紫藤むらさき")
    header = encode_bytes_field(1, encode_varint_field(1, 1789095600))
    return (
        header + encode_bytes_field(2, title) + b"".join(encode_bytes_field(3, chapter_message(*c)) for c in chapters)
    )


def page_message(url):
    return encode_bytes_field(5, encode_bytes_field(1, url))


def viewer_view(chapter_id=3407, pages=None, title="続編？知りません。　前編", status=0, tag=TAG):
    """A `MangaViewerView`; the site ends the pages with an image-less last-page card."""
    if pages is None:
        pages = [f"https://contents.zerosumonline.com/chapter_page/{chapter_id}/{n}.webp" for n in (1, 2)]
    message = encode_varint_field(1, status) if status else b""
    message += encode_varint_field(2, 216) + encode_bytes_field(3, tag)
    if title:
        message += encode_bytes_field(4, title)
    message += b"".join(page_message(url) for url in pages)
    message += encode_bytes_field(5, encode_bytes_field(2, b""))
    return message + encode_bytes_field(7, encode_bytes_field(1, "https://contents.zerosumonline.com/ad.webp"))


def webp(colour=(10, 20, 30)):
    raw = BytesIO()
    Image.new("RGB", (8, 8), colour).save(raw, "WEBP", lossless=True)
    return raw.getvalue()


@pytest.fixture
def client(fake_session, fake_response):
    def build(routes=None):
        merged = dict(routes or {})
        merged.setdefault("/viewer?chapter_id=3407", fake_response(viewer_view()))
        merged.setdefault(f"/title?tag={TAG}", fake_response(title_view()))
        merged.setdefault("contents.zerosumonline.com", fake_response(webp(), content_type="image/webp"))
        session = fake_session(merged)
        return ZeroSum(session), session

    return build


# --- the chapter tokens -----------------------------------------------------------


def test_encrypt_chapter_id_round_trips():
    token = encrypt_chapter_id(3407)
    assert token.startswith("U2FsdGVkX1")  # "Salted__" in base64
    assert "/" not in token and "=" not in token  # noqa: PT018 (one fact: the token is URL-encoded)
    assert decrypt_chapter_id(token) == 3407


def test_decrypt_chapter_id_reads_the_site_token():
    assert decrypt_chapter_id(SITE_TOKEN) == 3407
    assert decrypt_chapter_id("U2FsdGVkX1/uwOSu522UUh33hE7EBeaYpiUaKX4zN+U=") == 3407


@pytest.mark.parametrize(
    ("token", "reason"),
    [
        ("not base64!", "base64"),
        ("aGVsbG8gd29ybGQgd2l0aG91dCBzYWx0", "salt"),
        (base64.b64encode(b"Salted__" + b"\x00" * 9).decode(), "cipher blocks"),
        (encrypt_chapter_id(3407, key="wrong"), "chapter id"),
    ],
)
def test_decrypt_chapter_id_rejects_junk(token, reason):
    with pytest.raises(ValueError, match=reason):
        decrypt_chapter_id(token)


# --- the messages ------------------------------------------------------------------


def test_decode_viewer_reads_an_empty_answer():
    assert decode_viewer(b"") == {"status": 0, "titleId": 0, "titleTag": "", "viewerTitle": "", "pages": []}


# --- urls ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (CHAPTER_URL, True),
        (CHAPTER_URL + "/", True),
        (f"{BASE_URL}/episode/{TAG}/chapter/{SITE_TOKEN}", True),
        (SERIES_URL, True),
        (SERIES_URL + "/", True),
        ("http://zerosumonline.com/detail/migawari3", False),
        (f"{BASE_URL}/", False),
        (f"{BASE_URL}/comic", False),
        (f"{BASE_URL}/episode/{TAG}", False),
        (f"https://api.zerosumonline.com/api/v1/title?tag={TAG}", False),
        ("https://example.com/detail/migawari3", False),
    ],
)
def test_suitable(url, expected):
    assert ZeroSum.suitable(url) is expected


def test_is_series_means_a_detail_page():
    assert ZeroSum.is_series(SERIES_URL)
    assert not ZeroSum.is_series(CHAPTER_URL)
    assert not ZeroSum.is_series("https://example.com/detail/migawari3")


# --- reading a chapter ----------------------------------------------------------------


def test_episode_reads_the_titles_the_pages_and_the_next_chapter(client):
    zerosum, session = client()
    episode = zerosum.episode(CHAPTER_URL)

    assert episode.url == CHAPTER_URL
    assert episode.series_title == "【お試し読み】身代わり花嫁は、旦那様から溺愛されるようです。アンソロジーコミック　3"
    assert episode.episode_title == "続編？知りません。　前編"
    assert [page.url for page in episode.pages] == [
        "https://contents.zerosumonline.com/chapter_page/3407/1.webp",
        "https://contents.zerosumonline.com/chapter_page/3407/2.webp",
    ]
    assert episode.next_url == episode_url(TAG, 3425)
    assert episode.metadata["chapter"]["id"] == 3407
    assert episode.metadata["title"]["id"] == 216
    assert episode.metadata["viewer"]["titleTag"] == TAG

    # The viewer is a POST with nothing in the body; the listing a GET.
    assert session.posts == [(f"{API_URL}/viewer?chapter_id=3407", None)]
    assert session.calls == [f"{API_URL}/title?tag={TAG}"]
    assert session.headers_seen[-1]["Referer"] == CHAPTER_URL
    assert session.headers_seen[-1]["Origin"] == BASE_URL


def test_episode_accepts_the_site_token_and_a_plain_id(client):
    zerosum, _ = client()
    for url in (f"{BASE_URL}/episode/{TAG}/chapter/{SITE_TOKEN}", f"{BASE_URL}/episode/{TAG}/chapter/3407"):
        episode = zerosum.episode(url)
        assert episode.url == CHAPTER_URL
        assert len(episode.pages) == 2


def test_episode_trusts_the_tag_the_viewer_names(client, fake_response):
    zerosum, session = client({"/viewer?chapter_id=3407": fake_response(viewer_view(tag=TAG))})
    episode = zerosum.episode(f"{BASE_URL}/episode/other/chapter/3407")

    assert episode.url == CHAPTER_URL
    assert session.calls == [f"{API_URL}/title?tag={TAG}"]


def test_episode_stops_at_the_newest_chapter(client, fake_response):
    zerosum, _ = client(
        {"/viewer?chapter_id=3452": fake_response(viewer_view(3452, title="魔物王子と偽物の聖女　前編"))}
    )
    episode = zerosum.episode(episode_url(TAG, 3452))
    assert episode.episode_title == "魔物王子と偽物の聖女　前編"
    assert episode.next_url is None


def test_an_expired_chapter_has_no_pages(client, fake_response):
    # A chapter whose public window closed: `/viewer` still answers, with no pages
    # and no name, and `/title` no longer lists it.
    zerosum, _ = client({"/viewer?chapter_id=3300": fake_response(viewer_view(3300, pages=[], title=""))})
    episode = zerosum.episode(episode_url(TAG, 3300))

    assert episode.pages == ()
    assert episode.series_title.startswith("【お試し読み】")
    assert episode.episode_title == "3300"
    assert episode.next_url is None


def test_a_chapter_the_viewer_refuses_has_no_pages(client, fake_response):
    zerosum, _ = client({"/viewer?chapter_id=3407": fake_response(viewer_view(status=2))})
    episode = zerosum.episode(CHAPTER_URL)

    assert episode.pages == ()
    assert episode.episode_title == "続編？知りません。　前編"
    assert episode.next_url == episode_url(TAG, 3425)


def test_episode_refuses_an_unknown_chapter(client, fake_response):
    zerosum, _ = client(
        {"/viewer?chapter_id=99999": fake_response(text="{}", status_code=HTTPStatus.INTERNAL_SERVER_ERROR)}
    )
    with pytest.raises(NotAnEpisodePageError, match="no chapter 99999"):
        zerosum.episode(episode_url(TAG, 99999))


def test_episode_refuses_a_token_that_is_no_chapter_id(client):
    zerosum, session = client()
    with pytest.raises(NotAnEpisodePageError, match="names no chapter"):
        zerosum.episode(f"{BASE_URL}/episode/{TAG}/chapter/xxx")
    assert session.posts == []


def test_episode_refuses_a_series_url(client):
    zerosum, _ = client()
    with pytest.raises(UnsupportedUrlError, match="not an episode"):
        zerosum.episode(SERIES_URL)


# --- listing a series -------------------------------------------------------------------


def test_series_urls_lists_the_chapters_oldest_first(client):
    zerosum, session = client()
    urls = zerosum.series_urls(SERIES_URL)

    assert urls == [episode_url(TAG, chapter_id) for chapter_id in (3407, 3425, 3452)]
    assert all(ZeroSum.suitable(url) for url in urls)
    assert session.calls == [f"{API_URL}/title?tag={TAG}"]
    assert session.headers_seen[-1]["Referer"] == SERIES_URL


def test_series_urls_deduplicates(client, fake_response):
    zerosum, _ = client({f"/title?tag={TAG}": fake_response(title_view(chapters=[CHAPTERS[0], *CHAPTERS]))})
    assert zerosum.series_urls(SERIES_URL) == [episode_url(TAG, chapter_id) for chapter_id in (3407, 3425, 3452)]


def test_series_urls_rejects_a_series_without_chapters(client, fake_response):
    zerosum, _ = client({f"/title?tag={TAG}": fake_response(title_view(chapters=[]))})
    with pytest.raises(NotAnEpisodePageError, match="lists no episode"):
        zerosum.series_urls(SERIES_URL)


def test_series_urls_rejects_an_unknown_series(client, fake_response):
    zerosum, _ = client(
        {"/title?tag=nothing": fake_response(text='"Not Content"', status_code=HTTPStatus.NOT_IMPLEMENTED)}
    )
    with pytest.raises(NotAnEpisodePageError, match="no series 'nothing'"):
        zerosum.series_urls(f"{BASE_URL}/detail/nothing")


def test_series_urls_rejects_a_chapter_url(client):
    zerosum, _ = client()
    with pytest.raises(UnsupportedUrlError, match="not a series page"):
        zerosum.series_urls(CHAPTER_URL)


# --- downloading ---------------------------------------------------------------------------


def test_download_writes_the_pages(client, tmp_path):
    zerosum, session = client()
    result = Downloader(zerosum, tmp_path).download(CHAPTER_URL)

    assert result.status == "saved"
    assert result.save_dir == (
        tmp_path
        / "zerosumonline.com"
        / "【お試し読み】身代わり花嫁は、旦那様から溺愛されるようです。アンソロジーコミック　3"
        / "続編？知りません。　前編"
    )
    assert sorted(path.name for path in result.save_dir.iterdir()) == ["0.jpg", "1.jpg"]
    with Image.open(result.save_dir / "0.jpg") as saved:
        assert saved.getpixel((4, 4)) == pytest.approx((10, 20, 30), abs=8)
    assert session.calls[-1] == "https://contents.zerosumonline.com/chapter_page/3407/2.webp"
    assert session.headers_seen[-1]["Referer"] == CHAPTER_URL


# --- logging in ------------------------------------------------------------------------------


# --- the real site -----------------------------------------------------------------------------

# One episode per known host, free to read without an account: the first chapter of a
# long-running series, which the site lists without an end to its public window.
TEST_URLS: dict[str, str] = {
    "zerosumonline.com": episode_url("dogs", 1141),
}


@pytest.mark.network
@pytest.mark.parametrize("host", TEST_URLS)
def test_site_download(tmp_path, host):
    result = Downloader(ZeroSum(), tmp_path, only_first=True).download(TEST_URLS[host])
    assert result.status == "saved"
    assert (result.save_dir / "0.jpg").stat().st_size > 0


@pytest.mark.network
def test_detail_page_lists_chapters():
    urls = ZeroSum().series_urls(f"{BASE_URL}/detail/dogs")
    assert TEST_URLS["zerosumonline.com"] in urls
    assert all(ZeroSum.suitable(url) for url in urls)
