from __future__ import annotations

import base64
import hashlib
from io import BytesIO

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from PIL import Image

from getjmanga.downloader import Downloader
from getjmanga.errors import GetjmangaError, NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractors.comico import (
    API_URL,
    BASE_URL,
    Comico,
    api_headers,
    decrypt_url,
)

KEY = b"a7fc9dc89f2c873d79397f8a0028a4cd"

EPISODE_URL = f"{BASE_URL}/comic/13956/chapter/1/product"
LOCKED_URL = f"{BASE_URL}/comic/13956/chapter/4/product"
SERIES_URL = f"{BASE_URL}/comic/13956"
EPUB_URL = f"{BASE_URL}/magazine_comic/141484/chapter/1/product"
EPUB_SERIES_URL = f"{BASE_URL}/magazine_comic/141484"

CDN = "https://images.comico.io/content/ja/956/13956/1"
EPUB_ROOT = "https://images.comico.io/magazine_comic/content/onetimeurl/ja/484/141484/1/1660556025379.zip/unzip/files/"
PARAMETER = "Policy=eyJTdGF0ZW1lbnQ__&Signature=cHrl62Zw~urF__&Key-Pair-Id=APKAIZOQXEUERT6TH4NQ"


def encrypt_url(url):
    """Wrap a URL the way the API does: AES-256-CBC, zero iv, PKCS#7, base64."""
    plain = url.encode()
    padding = 16 - len(plain) % 16
    plain += bytes([padding]) * padding
    encryptor = Cipher(algorithms.AES(KEY), modes.CBC(bytes(16))).encryptor()
    return base64.b64encode(encryptor.update(plain) + encryptor.finalize()).decode()


def chapter_entry(chapter_id, name, *, free=True, has_trial=False):
    return {
        "id": chapter_id,
        "volumeId": chapter_id,
        "name": name,
        "salesConfig": {"free": free, "price": 0 if free else 61},
        "aborted": False,
        "hasTrial": has_trial,
        "sort": chapter_id,
        "activity": {"rented": False, "unlocked": False},
    }


CONTENT = {
    "type": "comic",
    "id": 13956,
    "name": "最悪な鬱小説を書き直してみせます",
    "orientation": "TTB",
    "chapterUnit": "episode",
    "chapterFileFormat": "image",
    "authors": [
        {"id": 2, "name": "ケイト・ウォーカー", "role": "original_creator", "sort": 2},
        {"id": 1, "name": "えいだ恭子", "role": "creator", "sort": 1},
    ],
    "publisherName": "SBCr",
}
# The work page lists chapters in order; the API hands them over that way too.
CHAPTERS = [
    chapter_entry(1, "第 1 話"),
    chapter_entry(2, "第 2 話"),
    chapter_entry(4, "第 4 話", free=False),
    chapter_entry(3, "第 3 話"),
]
LISTING = {"result": {"code": 200}, "data": {"episode": {"content": {**CONTENT, "chapters": CHAPTERS}}}}


def image_entry(sort, path, parameter=PARAMETER):
    return {"sort": sort, "url": encrypt_url(f"{CDN}/{path}"), "parameter": parameter, "width": 800, "height": 2000}


def chapter_response(chapter=None, *, images=None, epub=None, prev_chapter=None, next_chapter=None, content=CONTENT):
    chapter = dict(chapter or chapter_entry(1, "第 1 話"))
    if prev_chapter is not None:
        chapter["previousChapter"] = prev_chapter
    if next_chapter is not None:
        chapter["nextChapter"] = next_chapter
    if images is not None:
        chapter["images"] = images
    if epub is not None:
        chapter["epub"] = epub
    return {"result": {"code": 200}, "data": {"content": content, "chapter": chapter}}


READABLE = chapter_response(
    images=[
        image_entry(2, "1_x.jpg/dims/crop/x2000+0+2000/optimize"),
        image_entry(1, "1_x.jpg/dims/crop/x2000+0+0/optimize"),
    ],
    next_chapter={"id": 2, "name": "第 2 話", "free": True},
)
LOCKED = chapter_response(
    chapter_entry(4, "第 4 話", free=False),
    prev_chapter={"id": 3, "name": "第 3 話"},
    next_chapter={"id": 5, "name": "第 5 話"},
)
NOT_FOUND = {"result": {"code": 404, "message": "対象のエピソードが見つかりませんでした。"}, "data": {}}
DISCONTINUED = {"result": {"code": 303, "message": "販売停止されたコンテンツです。"}, "data": {}}

EPUB_CONTENT = {"type": "magazine_comic", "id": 141484, "name": "恋降るカラフル", "chapterFileFormat": "epub"}
EPUB = {
    "url": encrypt_url("https://images.comico.io/magazine_comic/content/ja/484/141484/1/1660556025379.zip"),
    "decryptKey": "tHklBbK0nU7lXutb1LBrNqPz2JDgaMptA0y2gRadzGJx3zG2tQ1rdih+s3Apr/eY",
    "chapterEpubIncludedFile": {
        "parameter": PARAMETER,
        "url": encrypt_url(EPUB_ROOT),
        "m2Parameter": {"optimize": "/dims/optimize"},
        "rootPath": "item/",
        "rootFileName": "standard.opf",
    },
}
OPF = """<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="BookId">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>恋降るカラフル_第1話(1)</dc:title></metadata>
<manifest>
<item media-type="application/xhtml+xml" id="toc" href="navigation-documents.xhtml" properties="nav"/>
<item media-type="text/css" id="css" href="style/fixed-layout-jp.css"/>
<item media-type="image/jpeg" id="cover" href="image/cover.jpg" properties="cover-image"/>
<item media-type="image/jpeg" id="i-001" href="image/i-001.jpg" />
<item media-type="image/jpeg" id="i-002" href="image/i-002.jpg" />
<item media-type="application/xhtml+xml" id="p-cover" href="xhtml/p-cover.xhtml" properties="svg" fallback="cover"/>
<item media-type="application/xhtml+xml" id="p-001" href="xhtml/p-001.xhtml" properties="svg" fallback="i-001"/>
<item media-type="application/xhtml+xml" id="p-002" href="xhtml/p-002.xhtml" properties="svg"/>
<item media-type="application/xhtml+xml" id="p-003" href="xhtml/p-003.xhtml" properties="svg"/>
<item media-type="image/jpeg" id="i-004" href="image/i-004.jpg" />
</manifest>
<spine page-progression-direction="rtl">
<itemref linear="yes" idref="p-cover" properties="rendition:page-spread-center"/>
<itemref linear="yes" idref="p-001" properties="page-spread-right"/>
<itemref linear="yes" idref="p-002" properties="page-spread-left"/>
<itemref linear="no" idref="toc"/>
<itemref idref="p-003"/>
<itemref idref="i-004"/>
<itemref idref="nowhere"/>
</spine>
</package>
"""
XHTML_002 = """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><body><div class="main">
<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" viewBox="0 0 764 1200">
<image width="764" height="1200" xlink:href="../image/i-002.jpg" />
</svg></div></body></html>
"""
XHTML_003 = "<html><body><p>an interstitial page with no picture</p></body></html>"


def jpeg_bytes(colour=(10, 20, 30), size=(8, 8)):
    raw = BytesIO()
    Image.new("RGB", size, colour).save(raw, "JPEG")
    return raw.getvalue()


@pytest.fixture
def client(fake_session, fake_response):
    def build(routes=None):
        merged = dict(routes or {})
        merged.setdefault(f"{API_URL}/comic/13956/chapter/1/product", fake_response(payload=READABLE))
        merged.setdefault(f"{API_URL}/comic/13956/chapter/4/product", fake_response(payload=LOCKED))
        merged.setdefault(f"{API_URL}/comic/13956/chapter/999/product", fake_response(payload=NOT_FOUND))
        merged.setdefault(f"{API_URL}/comic/13956", fake_response(payload=LISTING))
        merged.setdefault(f"{API_URL}/comic/411", fake_response(payload=DISCONTINUED))
        merged.setdefault("images.comico.io", fake_response(jpeg_bytes(), content_type="image/jpeg"))
        session = fake_session(merged)
        return Comico(session), session

    return build


# --- the pure parts --------------------------------------------------------------


def test_api_headers_sign_the_request_time():
    headers = api_headers(1789671992)
    assert headers["X-comico-request-time"] == "1789671992"
    expected = hashlib.sha256(b"9241d2f090d01716feac20ae08ba791a0.0.0.01789671992").hexdigest()
    assert headers["X-comico-check-sum"] == expected
    assert headers["X-comico-client-platform"] == "web"
    assert headers["X-comico-client-immutable-uid"] == "0.0.0.0"  # noqa: S104 (the ip the app signs with)


def test_decrypt_url_round_trips():
    url = f"{CDN}/1_1774340329847.jpg/dims/crop/x2000+0+0/optimize"
    assert decrypt_url(encrypt_url(url)) == url


def test_decrypt_url_rejects_a_partial_block():
    with pytest.raises(GetjmangaError, match="whole number"):
        decrypt_url(base64.b64encode(b"\x00" * 17).decode())


def test_decrypt_url_rejects_a_foreign_token():
    with pytest.raises(GetjmangaError, match="padding"):
        decrypt_url(base64.b64encode(b"\x00" * 32).decode())


# --- urls ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (EPISODE_URL, True),
        (EPISODE_URL + "/", True),
        (f"{BASE_URL}/comic/13956/chapter/4/trial", True),
        (EPUB_URL, True),
        (SERIES_URL, True),
        (SERIES_URL + "/", True),
        (EPUB_SERIES_URL, True),
        ("http://www.comico.jp/comic/13956/chapter/1/product", False),
        ("https://comico.jp/comic/13956", False),
        (f"{BASE_URL}/comic/13956/chapter/1", False),
        (f"{BASE_URL}/comic/13956/chapter/1/rental", False),
        (f"{BASE_URL}/novel/13956", False),
        (f"{BASE_URL}/challenge/comic/1", False),
        (f"{BASE_URL}/comic/daily/monday", False),
        (f"{BASE_URL}/login", False),
    ],
)
def test_suitable(url, expected):
    assert Comico.suitable(url) is expected


def test_is_series_means_a_work_page():
    assert Comico.is_series(SERIES_URL)
    assert Comico.is_series(EPUB_SERIES_URL)
    assert not Comico.is_series(EPISODE_URL)
    assert not Comico.is_series("https://example.com/comic/13956")


# --- reading a chapter ---------------------------------------------------------------


def test_episode_reads_the_titles_the_pages_and_the_next_chapter(client):
    comico, session = client()
    episode = comico.episode(EPISODE_URL)

    assert episode.url == EPISODE_URL
    assert episode.series_title == "最悪な鬱小説を書き直してみせます"
    assert episode.episode_title == "第 1 話"
    assert (episode.writer, episode.publisher) == ("えいだ恭子, ケイト・ウォーカー (原作)", "SBCr")
    # Decrypted, in `sort` order, with the signed query the CDN checks.
    assert [page.url for page in episode.pages] == [
        f"{CDN}/1_x.jpg/dims/crop/x2000+0+0/optimize?{PARAMETER}",
        f"{CDN}/1_x.jpg/dims/crop/x2000+0+2000/optimize?{PARAMETER}",
    ]
    assert (episode.pages[0].width, episode.pages[0].height) == (800, 2000)
    assert (episode.prev_url, episode.next_url) == (None, f"{BASE_URL}/comic/13956/chapter/2/product")
    assert episode.metadata["content"]["id"] == 13956
    assert episode.metadata["chapter"]["id"] == 1

    assert session.calls == [f"{API_URL}/comic/13956/chapter/1/product"]
    sent = session.headers_seen[0]
    assert sent["X-comico-client-platform"] == "web"
    assert sent["X-comico-check-sum"] == api_headers(int(sent["X-comico-request-time"]))["X-comico-check-sum"]
    assert sent["Referer"] == EPISODE_URL


def test_episode_accepts_a_trailing_slash(client):
    comico, _ = client()
    assert comico.episode(EPISODE_URL + "/").url == EPISODE_URL


def test_episode_stops_at_the_last_chapter(client, fake_response):
    comico, _ = client({f"{API_URL}/comic/13956/chapter/1/product": fake_response(payload=chapter_response(images=[]))})
    assert comico.episode(EPISODE_URL).next_url is None


def test_a_locked_chapter_has_no_pages_but_still_a_next_chapter(client):
    comico, session = client()
    episode = comico.episode(LOCKED_URL)

    assert episode.pages == ()
    assert episode.series_title == "最悪な鬱小説を書き直してみせます"
    assert episode.episode_title == "第 4 話"
    assert (episode.writer, episode.publisher) == ("えいだ恭子, ケイト・ウォーカー (原作)", "SBCr")
    assert (episode.prev_url, episode.next_url) == (
        f"{BASE_URL}/comic/13956/chapter/3/product",
        f"{BASE_URL}/comic/13956/chapter/5/product",
    )
    # A 200 without `images` is the whole answer; the work page is not consulted.
    assert session.calls == [f"{API_URL}/comic/13956/chapter/4/product"]


def test_a_chapter_the_api_refuses_is_named_from_the_work_page(client, fake_response):
    refused = {"result": {"code": 401, "message": "ログインが必要です。"}, "data": {}}
    comico, session = client({f"{API_URL}/comic/13956/chapter/4/product": fake_response(payload=refused)})
    episode = comico.episode(LOCKED_URL)

    assert episode.pages == ()
    assert episode.episode_title == "第 4 話"
    # The work lists 4 before 3; `sort` settles the order.
    assert (episode.prev_url, episode.next_url) == (f"{BASE_URL}/comic/13956/chapter/3/product", None)
    assert episode.metadata["reason"] == "ログインが必要です。"
    assert session.calls == [f"{API_URL}/comic/13956/chapter/4/product", f"{API_URL}/comic/13956"]


def test_episode_rejects_an_unknown_chapter(client):
    comico, _ = client()
    with pytest.raises(NotAnEpisodePageError, match="見つかりませんでした"):
        comico.episode(f"{BASE_URL}/comic/13956/chapter/999/product")


def test_episode_rejects_a_discontinued_work(client, fake_response):
    comico, _ = client({f"{API_URL}/comic/411/chapter/1/product": fake_response(payload=DISCONTINUED)})
    with pytest.raises(NotAnEpisodePageError, match="販売停止"):
        comico.episode(f"{BASE_URL}/comic/411/chapter/1/product")


def test_episode_refuses_a_work_page(client):
    comico, _ = client()
    with pytest.raises(UnsupportedUrlError, match="not a chapter"):
        comico.episode(SERIES_URL)


def test_a_trial_is_named_after_the_chapter_it_previews(client, fake_response):
    comico, _ = client(
        {
            f"{API_URL}/comic/13956/chapter/4/trial": fake_response(
                payload=chapter_response(
                    chapter_entry(4, "第 4 話", free=False, has_trial=True),
                    images=[image_entry(1, "4_x.jpg/dims/crop/x2000+0+0/optimize")],
                    next_chapter={"id": 5, "name": "第 5 話"},
                )
            )
        }
    )
    episode = comico.episode(f"{BASE_URL}/comic/13956/chapter/4/trial")

    assert episode.episode_title == "第 4 話 試し読み"
    assert len(episode.pages) == 1
    assert episode.next_url == f"{BASE_URL}/comic/13956/chapter/5/trial"


# --- an EPUB chapter ------------------------------------------------------------------


def test_episode_reads_an_epub_off_the_unzipped_package(client, fake_response):
    comico, session = client(
        {
            f"{API_URL}/magazine_comic/141484/chapter/1/product": fake_response(
                payload=chapter_response(
                    chapter_entry(1, "第1話(1)"), epub=EPUB, next_chapter={"id": 2}, content=EPUB_CONTENT
                )
            ),
            "item/standard.opf": fake_response(text=OPF),
            "item/xhtml/p-002.xhtml": fake_response(text=XHTML_002),
            "item/xhtml/p-003.xhtml": fake_response(text=XHTML_003),
        }
    )
    episode = comico.episode(EPUB_URL)

    assert episode.series_title == "恋降るカラフル"
    assert episode.episode_title == "第1話(1)"
    assert [page.url for page in episode.pages] == [
        f"{EPUB_ROOT}item/image/cover.jpg/dims/optimize?{PARAMETER}",
        f"{EPUB_ROOT}item/image/i-001.jpg/dims/optimize?{PARAMETER}",
        f"{EPUB_ROOT}item/image/i-002.jpg/dims/optimize?{PARAMETER}",
        f"{EPUB_ROOT}item/image/i-004.jpg/dims/optimize?{PARAMETER}",
    ]
    assert episode.next_url == f"{BASE_URL}/magazine_comic/141484/chapter/2/product"
    assert session.calls[1] == f"{EPUB_ROOT}item/standard.opf?{PARAMETER}"
    assert session.calls[2] == f"{EPUB_ROOT}item/xhtml/p-002.xhtml?{PARAMETER}"
    assert session.headers_seen[1]["Referer"] == EPUB_URL


def test_an_epub_without_its_package_is_locked(client, fake_response):
    epub = {**EPUB, "chapterEpubIncludedFile": {}}
    comico, _ = client(
        {
            f"{API_URL}/magazine_comic/141484/chapter/1/product": fake_response(
                payload=chapter_response(chapter_entry(1, "第1話(1)"), epub=epub, content=EPUB_CONTENT)
            ),
        }
    )
    assert comico.episode(EPUB_URL).pages == ()


# --- listing a work --------------------------------------------------------------------


def test_series_urls_lists_the_chapters_in_reading_order(client, fake_session):
    comico, session = client()
    urls = comico.series_urls(SERIES_URL)

    assert urls == [f"{BASE_URL}/comic/13956/chapter/{chapter_id}/product" for chapter_id in (1, 2, 3, 4)]
    assert session.calls == [f"{API_URL}/comic/13956"]
    assert session.headers_seen[0]["Referer"] == SERIES_URL
    # Asked again, the listing is remembered.
    assert comico.series_urls(SERIES_URL) == urls
    assert len(session.calls) == 1


def test_series_urls_deduplicates(client, fake_response):
    listing = {"result": {"code": 200}, "data": {"episode": {"content": {**CONTENT, "chapters": [CHAPTERS[0]] * 2}}}}
    comico, _ = client({f"{API_URL}/comic/13956": fake_response(payload=listing)})
    assert comico.series_urls(SERIES_URL) == [EPISODE_URL]


def test_series_urls_falls_back_to_the_volume_tab(client, fake_response):
    volumes = {
        **EPUB_CONTENT,
        "chapterUnit": "volume",
        "chapters": [chapter_entry(1, "1巻", free=False, has_trial=True)],
    }
    listing = {"result": {"code": 200}, "data": {"episode": {"content": None}, "volume": {"content": volumes}}}
    comico, _ = client({f"{API_URL}/magazine_comic/141484": fake_response(payload=listing)})
    assert comico.series_urls(EPUB_SERIES_URL) == [EPUB_URL]


def test_series_urls_rejects_a_work_without_chapters(client, fake_response):
    listing = {"result": {"code": 200}, "data": {"episode": {"content": {**CONTENT, "chapters": []}}}}
    comico, _ = client({f"{API_URL}/comic/13956": fake_response(payload=listing)})
    with pytest.raises(NotAnEpisodePageError, match="lists no chapter"):
        comico.series_urls(SERIES_URL)


def test_series_urls_rejects_a_discontinued_work(client):
    comico, _ = client()
    with pytest.raises(NotAnEpisodePageError, match="販売停止"):
        comico.series_urls(f"{BASE_URL}/comic/411")


def test_series_urls_rejects_a_chapter_url(client):
    comico, _ = client()
    with pytest.raises(UnsupportedUrlError, match="not a work page"):
        comico.series_urls(EPISODE_URL)


# --- downloading -------------------------------------------------------------------------


def test_download_writes_the_pages(client, tmp_path):
    comico, session = client()
    result = Downloader(comico, tmp_path).download(EPISODE_URL)

    assert result.status == "saved"
    assert result.save_dir == tmp_path / "www.comico.jp" / "最悪な鬱小説を書き直してみせます" / "第 1 話"
    assert sorted(path.name for path in result.save_dir.iterdir()) == ["0.jpg", "1.jpg"]
    with Image.open(result.save_dir / "0.jpg") as saved:
        assert saved.getpixel((4, 4)) == pytest.approx((10, 20, 30), abs=8)
    assert session.calls[-1] == f"{CDN}/1_x.jpg/dims/crop/x2000+0+2000/optimize?{PARAMETER}"
    assert session.headers_seen[-1]["Referer"] == EPISODE_URL


# --- the real site ------------------------------------------------------------------------

# One chapter per known host, free to read without an account.
TEST_URLS: dict[str, str] = {
    "www.comico.jp": "https://www.comico.jp/comic/25/chapter/1/product",
}
# A page-flip book, served as an EPUB.
EPUB_TEST_URL = "https://www.comico.jp/magazine_comic/141484/chapter/1/product"


@pytest.mark.network
@pytest.mark.parametrize("url", [*TEST_URLS.values(), EPUB_TEST_URL])
def test_site_download(tmp_path, url):
    result = Downloader(Comico(), tmp_path, only_first=True).download(url)
    assert result.status == "saved"
    assert (result.save_dir / "0.jpg").stat().st_size > 0


@pytest.mark.network
def test_work_page_lists_chapters():
    urls = Comico().series_urls("https://www.comico.jp/comic/25")
    assert TEST_URLS["www.comico.jp"] in urls
    assert all(Comico.suitable(url) for url in urls)


@pytest.mark.network
def test_a_paid_chapter_is_locked():
    episode = Comico().episode("https://www.comico.jp/magazine_comic/127597/chapter/1/product")
    assert episode.pages == ()
    assert episode.episode_title == "1巻"
    assert episode.next_url == "https://www.comico.jp/magazine_comic/127597/chapter/2/product"
