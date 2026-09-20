from __future__ import annotations

from http import HTTPStatus
from io import BytesIO

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from PIL import Image

from getjmanga.downloader import Downloader
from getjmanga.errors import LoginError, NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractors.fuz import BASE_URL, Fuz
from getjmanga.protobuf import decode_fields, encode_bytes_field, encode_varint_field

KEY = "3ac550b62b4734c8411b5076b748a9c8bd6af1e87117d012789d5cc8ae1563a7"
IV = "484a87c5baeac6a0bafd335e5f5055b2"

CHAPTER_URL = f"{BASE_URL}/manga/viewer/79232"
MANGA_URL = f"{BASE_URL}/manga/4066"

# The manga's chapters as the API lists them: newest first, (id, title, points).
CHAPTERS = [(79238, "2話（1）", 30), (79235, "1話（2）", 0), (79232, "1話（1）", 0)]


def chapter_message(chapter_id, title, points):
    fields = encode_varint_field(1, chapter_id) + encode_bytes_field(2, title)
    if points:
        fields += encode_bytes_field(5, encode_varint_field(1, 1) + encode_varint_field(2, points))
    return encode_bytes_field(2, fields)


def image_page(path, *, key=KEY, iv=IV, extra=False):
    image = encode_bytes_field(1, path) + encode_varint_field(5, 8) + encode_varint_field(6, 8)
    if key:
        image += encode_bytes_field(3, iv) + encode_bytes_field(4, key)
    if extra:
        image += encode_varint_field(7, 1)
    return encode_bytes_field(2, encode_bytes_field(1, image))


def viewer_response(chapter_id=79232, pages=None, chapters=CHAPTERS):
    """A `WebMangaViewer2Response` with the fields the extractor reads."""
    if pages is None:
        pages = [image_page("/f/x/0.jpeg.enc?h=a"), image_page("/f/x/1.jpeg.enc?h=b")]
    viewer_data = encode_bytes_field(1, "1話（1）") + b"".join(pages)
    # A last-page card and an ad, which are not images.
    viewer_data += encode_bytes_field(2, encode_bytes_field(3, b"")) + encode_bytes_field(2, encode_bytes_field(4, b""))
    group = encode_bytes_field(1, encode_bytes_field(3, "issue")) + b"".join(chapter_message(*c) for c in chapters)
    manga = encode_varint_field(1, 4066) + encode_bytes_field(2, "氷舞のアウフギーサー")
    return (
        encode_bytes_field(2, viewer_data)
        + encode_bytes_field(5, group)
        + encode_bytes_field(11, manga)
        + encode_varint_field(12, chapter_id)
    )


def encrypted_png(size=(8, 8), colour=(10, 20, 30)):
    raw = BytesIO()
    Image.new("RGB", size, colour).save(raw, "PNG")
    plain = raw.getvalue()
    padding = 16 - len(plain) % 16
    plain += bytes([padding]) * padding
    encryptor = Cipher(algorithms.AES(bytes.fromhex(KEY)), modes.CBC(bytes.fromhex(IV))).encryptor()
    return encryptor.update(plain) + encryptor.finalize()


@pytest.fixture
def client(fake_session, fake_response):
    def build(routes=None):
        merged = dict(routes or {})
        merged.setdefault("web_manga_viewer_2", fake_response(viewer_response(), content_type="application/protobuf"))
        merged.setdefault("img.comic-fuz.com", fake_response(encrypted_png()))
        session = fake_session(merged)
        return Fuz(session), session

    return build


# --- the wire format ----------------------------------------------------------


# --- urls -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (CHAPTER_URL, True),
        (CHAPTER_URL + "/", True),
        (MANGA_URL, True),
        ("https://comic-fuz.com/manga/4066/", True),
        ("http://comic-fuz.com/manga/viewer/79232", False),
        ("https://comic-fuz.com/book/viewer/1", False),
        ("https://comic-fuz.com/manga/ranking", False),
        ("https://example.com/manga/viewer/79232", False),
    ],
)
def test_suitable(url, expected):
    assert Fuz.suitable(url) is expected


def test_is_series_means_a_manga_page():
    assert Fuz.is_series(MANGA_URL)
    assert not Fuz.is_series(CHAPTER_URL)
    assert not Fuz.is_series("https://example.com/manga/4066")


# --- reading a chapter ------------------------------------------------------------


def test_episode_reads_the_titles_the_pages_and_the_next_chapter(client):
    fuz, _ = client()
    episode = fuz.episode(CHAPTER_URL)

    assert episode.series_title == "氷舞のアウフギーサー"
    assert episode.episode_title == "1話（1）"
    assert [page.url for page in episode.pages] == [
        "https://img.comic-fuz.com/f/x/0.jpeg.enc?h=a",
        "https://img.comic-fuz.com/f/x/1.jpeg.enc?h=b",
    ]
    assert episode.pages[0].extra == {"key": KEY, "iv": IV}
    assert episode.pages[0].width == 8
    assert episode.next_url == f"{BASE_URL}/manga/viewer/79235"
    assert episode.metadata["manga_id"] == 4066
    assert [c["id"] for c in episode.metadata["chapters"]] == [79232, 79235, 79238]
    assert episode.metadata["chapters"][2]["points"] == 30


def test_episode_sends_the_chapter_id_and_the_browser_device(client):
    fuz, session = client()
    fuz.episode(CHAPTER_URL)

    url, body = session.posts[0]
    assert url == "https://api.comic-fuz.com/v1/web_manga_viewer_2"
    assert list(decode_fields(body)) == [(1, encode_varint_field(3, 2)), (4, 79232)]


def test_episode_skips_extra_pages(client, fake_response):
    pages = [image_page("/f/x/0.jpeg.enc"), image_page("/m/promo.jpeg", key="", extra=True)]
    fuz, _ = client({"web_manga_viewer_2": fake_response(viewer_response(pages=pages))})
    assert [page.url for page in fuz.episode(CHAPTER_URL).pages] == ["https://img.comic-fuz.com/f/x/0.jpeg.enc"]


def test_episode_stops_at_the_last_chapter(client, fake_response):
    fuz, _ = client({"web_manga_viewer_2": fake_response(viewer_response(chapter_id=79238))})
    assert fuz.episode(f"{BASE_URL}/manga/viewer/79238").next_url is None


@pytest.mark.parametrize("status", [HTTPStatus.UNAUTHORIZED, HTTPStatus.PAYMENT_REQUIRED])
def test_a_locked_chapter_has_no_pages(client, fake_response, status):
    fuz, _ = client({"web_manga_viewer_2": fake_response(b"", status_code=status)})
    episode = fuz.episode(f"{BASE_URL}/manga/viewer/79238")

    assert episode.pages == ()
    # Nothing has named the manga yet, so the chapter goes by its id.
    assert episode.series_title == "79238"
    assert episode.episode_title == "79238"
    assert episode.next_url is None


def test_a_locked_chapter_is_named_from_a_list_seen_earlier(client, fake_response):
    fuz, _ = client(
        {
            "web_manga_viewer_2": [
                fake_response(viewer_response()),
                fake_response(b"", status_code=HTTPStatus.PAYMENT_REQUIRED),
            ],
        },
    )
    fuz.episode(CHAPTER_URL)
    episode = fuz.episode(f"{BASE_URL}/manga/viewer/79235")

    assert episode.pages == ()
    assert episode.series_title == "氷舞のアウフギーサー"
    assert episode.episode_title == "1話（2）"
    assert episode.next_url == f"{BASE_URL}/manga/viewer/79238"


def test_episode_refuses_a_manga_page(client):
    fuz, _ = client()
    with pytest.raises(UnsupportedUrlError, match="not a chapter"):
        fuz.episode(MANGA_URL)


# --- listing a manga ---------------------------------------------------------------


def test_series_urls_lists_the_chapters_oldest_first(client):
    fuz, session = client()
    urls = fuz.series_urls(MANGA_URL)

    assert urls == [f"{BASE_URL}/manga/viewer/{chapter_id}" for chapter_id in (79232, 79235, 79238)]
    _, body = session.posts[0]
    assert list(decode_fields(body)) == [
        (1, encode_varint_field(3, 2)),
        (5, encode_varint_field(1, 4066) + encode_varint_field(2, 2)),
    ]


def test_series_urls_rejects_a_manga_without_chapters(client, fake_response):
    fuz, _ = client({"web_manga_viewer_2": fake_response(viewer_response(chapters=[]))})
    with pytest.raises(NotAnEpisodePageError, match="lists no chapter"):
        fuz.series_urls(MANGA_URL)


def test_series_urls_rejects_a_manga_that_opens_nothing(client, fake_response):
    fuz, _ = client({"web_manga_viewer_2": fake_response(b"", status_code=HTTPStatus.UNAUTHORIZED)})
    with pytest.raises(NotAnEpisodePageError, match="opens no chapter"):
        fuz.series_urls(MANGA_URL)


def test_series_urls_rejects_a_chapter_url(client):
    fuz, _ = client()
    with pytest.raises(UnsupportedUrlError, match="not a manga page"):
        fuz.series_urls(CHAPTER_URL)


# --- downloading --------------------------------------------------------------------


def test_download_writes_decrypted_pages(client, tmp_path):
    fuz, session = client()
    result = Downloader(fuz, tmp_path).download(CHAPTER_URL)

    assert result.status == "saved"
    assert result.save_dir == tmp_path / "氷舞のアウフギーサー" / "1話（1）"
    assert sorted(path.name for path in result.save_dir.iterdir()) == ["0.jpg", "1.jpg"]
    with Image.open(result.save_dir / "0.jpg") as saved:
        assert saved.getpixel((4, 4)) == pytest.approx((10, 20, 30), abs=8)
    assert session.headers_seen[-1]["Referer"] == CHAPTER_URL


def test_image_leaves_an_unencrypted_page_alone(client, fake_response):
    raw = BytesIO()
    Image.new("RGB", (8, 8), (1, 2, 3)).save(raw, "PNG")
    fuz, _ = client(
        {
            "web_manga_viewer_2": fake_response(viewer_response(pages=[image_page("/f/x/0.jpeg", key="")])),
            "img.comic-fuz.com": fake_response(raw.getvalue()),
        },
    )
    episode = fuz.episode(CHAPTER_URL)
    assert episode.pages[0].extra == {"key": "", "iv": ""}
    assert fuz.image(episode.pages[0], episode).getpixel((0, 0)) == (1, 2, 3)


# --- logging in ----------------------------------------------------------------------


def test_login_posts_the_credentials(client, fake_response):
    fuz, session = client({"sign_in": fake_response(encode_varint_field(1, 1))})
    fuz.login(CHAPTER_URL, "someone@example.com", "hunter2")

    url, body = session.posts[0]
    assert url == "https://api.comic-fuz.com/v1/sign_in"
    assert list(decode_fields(body)) == [(1, encode_varint_field(3, 2)), (2, b"someone@example.com"), (3, b"hunter2")]


def test_login_raises_with_the_site_reason(client, fake_response):
    answer = encode_varint_field(1, 0) + encode_bytes_field(2, "メールアドレスまたはパスワードが間違っています。")
    fuz, _ = client({"sign_in": fake_response(answer)})
    with pytest.raises(LoginError, match="間違っています"):
        fuz.login(CHAPTER_URL, "someone@example.com", "wrong")


# --- the real site --------------------------------------------------------------------

TEST_URLS: dict[str, str] = {
    "comic-fuz.com": "https://comic-fuz.com/manga/viewer/79232",
}


@pytest.mark.network
@pytest.mark.parametrize("host", TEST_URLS)
def test_site_download(tmp_path, host):
    result = Downloader(Fuz(), tmp_path, only_first=True).download(TEST_URLS[host])
    assert result.status == "saved"
    assert (result.save_dir / "0.jpg").stat().st_size > 0


@pytest.mark.network
def test_manga_page_lists_chapters():
    urls = Fuz().series_urls("https://comic-fuz.com/manga/4066")
    assert "https://comic-fuz.com/manga/viewer/79232" in urls
    assert all(Fuz.suitable(url) for url in urls)
