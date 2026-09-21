from __future__ import annotations

import base64
import json
from datetime import date
from io import BytesIO
from urllib.parse import quote

import pytest
from PIL import Image

from getjmanga.downloader import Downloader
from getjmanga.errors import GetjmangaError, NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractors.gakcomic import (
    BASE_URL,
    Gakcomic,
    aes_decrypt,
    aes_encrypt,
    decode_container,
    dh_public,
    dh_shared_key,
    inline_image,
    key_hash,
    rc4,
)

WORK_URL = f"{BASE_URL}/comic/page-1020632500/"
EPISODE_URL = f"{WORK_URL}#episode-17830"
CID_1 = "0f3fcbd2b42749f788bfdc16731af5a9"
CID_2 = "9c2d53b5aa2c4abba9c7a8aa5adf4626"
VIEWER_URL = f"{BASE_URL}/viewer/?content_id={CID_1}"
FOUR_KOMA_URL = f"{BASE_URL}/comic/page-1020620800/"
API_KEY = "a3yDun7GZRcMAZsi9RyFR4CmiVkPHRCy"

# Fixed exponents stand in for the random ones of both sides of the handshake.
CLIENT_PRIVATE = 1234567890123456789012345678901234567890
SERVER_PRIVATE = 98765432109876543210
SHARED_KEY = dh_shared_key(dh_public(CLIENT_PRIVATE), SERVER_PRIVATE)
HEX_KEY = "5F2E2E4D2D384F5642683C62322938307A256543553852235E20416123315437"
LIBRARY = f"https://cloud-library.keyring.net/contents/0f/{CID_1}/"


def episode_block(post_id, title, *, content_id="", image=""):
    action = (
        f'<form action="/viewer/?content_id={content_id}" method="POST" class="episode-form">' if content_id else ""
    )
    modal = f'<div class="is-hidden"><img src="{image}" class="pg-book__4coma-img"></div>' if image else ""
    return f"""
      <div class="pg-book-episode js-modal-episode" id="episode-{post_id}">
        <div class="pg-book-episode__image"><img src="/t.jpg" alt="{title}" class="pg-book-episode__img"></div>
        <div class="pg-book-episode__data">
          <div class="pg-book-episode__title"><a href="#episode-{post_id}" class="episode-title-link">
            {title}          </a></div>
          {action}
            <input type="hidden" name="contentid" value="{content_id}">
            <input type="hidden" name="post_id" value="17826">
            <button type="submit" class="c-button pg-book-episode__button">無料で読む</button>
          {"</form>" if content_id else ""}
          {modal}{modal}
        </div>
      </div>"""


def work_html(blocks, title="うまくなる卓球"):
    return f"""<html><head><title>{title} | ガッコミ</title></head><body>
      <p class="pg-book-meta__subtitle">まんが入門シリーズNEO</p>
      <h1 class="pg-book-meta__title">
        {title}<div class="pg-book-meta__buttons"><span class="count-box">4</span></div>
      </h1>
      <h2 class="pg-book-meta__editor-title">編著者</h2>
      <ul class="pg-book-meta__editor-lists">
        <li class="pg-book-meta__editor-list">大富寺航（まんが）</li>
        <li class="pg-book-meta__editor-list">山口隆一（ぐっちぃ）【ＷＲＭ】（監修）</li>
      </ul>
      <div class="pg-book-episodes js-book-episodes">{"".join(blocks)}</div>
      </body></html>"""


WORK_HTML = work_html(
    [
        episode_block(17830, "第１章", content_id=CID_1),
        episode_block(17833, "第２章", content_id=CID_2),
        episode_block(17830, "第１章", content_id=CID_1),
        episode_block(17832, "第３章", content_id="83e3d6782e23482da8128a74d6de5dd8"),
    ],
)
FOUR_KOMA_HTML = work_html(
    [
        episode_block(17341, "はじめに", image="https://s3.example/uploads/hajimeni.jpg"),
        episode_block(17373, "北海道", image="https://s3.example/uploads/hokkaido.jpg"),
    ],
    title="ガッコミ出張版",
)


TOKEN = "Ajs9Ux2Jal5kDcM9wD97WLFXOnQfX0WW"


def viewer_html(one_time=TOKEN):
    return f'<html><script>const episodes = [];\n init_content("{one_time}");</script></html>'


CUSTOM_JS = f"require.config({{ config: {{ 'ModuleConfig': {{ 'token': token, 'apiKey': '{API_KEY}', }} }} }});"


def exchange_answer():
    return {"Y": dh_public(SERVER_PRIVATE), "H": key_hash(SHARED_KEY), "S": "session-1", "Status": 0}


def token_answer(info=None, *, status=0, description="O.K."):
    """What `token/v1/use` answers: the plain JSON, AES-encrypted and url-quoted."""
    if status:
        plain = {"Status": status, "StatusDescription": description}
    else:
        plain = {
            "Status": 0,
            "StatusDescription": "O.K.",
            "FileID": CID_1,
            "ContentsInfo": {
                "title": "うまくなる卓球　第１章",
                "author": "大富寺航（漫画）",
                "pageCount": 2,
                "keyId": CID_1,
                "hexkey": HEX_KEY,
                **(info or {}),
            },
        }
    encrypted = base64.b64encode(aes_encrypt(SHARED_KEY, json.dumps(plain).encode())).decode()
    return {"EncryptedElement": quote(encrypted, safe="")}


CONTAINER_XML = """<?xml version="1.0"?><container><rootfiles>
  <rootfile full-path="OEBPS/Umakunaru_Takkyu.opf" media-type="application/oebps-package+xml"/>
</rootfiles></container>"""

OPF = """<?xml version="1.0" encoding="UTF-8"?><package version="3.0">
<metadata><dc:title id="title">うまくなる卓球　第１章</dc:title>
<meta name="original-resolution" content="1566x1912" /></metadata>
<manifest>
<item media-type="application/xhtml+xml" id="toc" href="toc.xhtml" properties="nav" />
<item id="cover" href="images/image_000h.jpg" properties="cover-image" media-type="image/jpeg" />
<item id="book_000" href="text/book_000.xhtml" properties="svg" media-type="application/xhtml+xml" />
<item href="text/book_002.xhtml" id="book_002" media-type="application/xhtml+xml" />
</manifest>
<spine page-progression-direction="rtl">
<itemref idref="book_000"/><itemref idref="book_002"/><itemref idref="missing"/>
</spine>
</package>"""


def png(colour):
    raw = BytesIO()
    Image.new("RGB", (8, 8), colour).save(raw, "PNG")
    return raw.getvalue()


def page_xhtml(colour):
    data = base64.b64encode(png(colour)).decode()
    return (
        '<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml"><body><div id="top">'
        '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" viewBox="0 0 8 8">'
        f'<image width="8" height="8" xlink:href="data:image/png;base64,{data}"/></svg></div></body></html>'
    ).encode()


def container(kind, body):
    return b"KREPUBB" + bytes([kind]) + body


def encrypted_page(colour):
    return container(3, rc4(bytes.fromhex(HEX_KEY[:32]), page_xhtml(colour)))


@pytest.fixture
def fixed_private(monkeypatch):
    monkeypatch.setattr(Gakcomic, "_private_key", staticmethod(lambda: CLIENT_PRIVATE))


@pytest.fixture
def client(fake_session, fake_response, fixed_private):
    def build(routes=None):
        merged = dict(routes or {})
        merged.setdefault("/comic/page-1020632500/", fake_response(text=WORK_HTML))
        merged.setdefault("/comic/page-1020620800/", fake_response(text=FOUR_KOMA_HTML))
        merged.setdefault("/viewer/custom.js", fake_response(text=CUSTOM_JS))
        merged.setdefault("/viewer/", fake_response(text=viewer_html()))
        merged.setdefault("session/key/v1/exchange", fake_response(payload=exchange_answer()))
        merged.setdefault("onetime/token/v1/use", fake_response(payload=token_answer()))
        merged.setdefault("META-INF/container.xml", fake_response(text=CONTAINER_XML))
        merged.setdefault(".opf", fake_response(container(2, bytes(b ^ 0xFF for b in OPF.encode()))))
        merged.setdefault("book_000.xhtml", fake_response(encrypted_page((10, 20, 30))))
        merged.setdefault("book_002.xhtml", fake_response(encrypted_page((40, 50, 60))))
        merged.setdefault("s3.example", fake_response(png((70, 80, 90))))
        session = fake_session(merged)
        return Gakcomic(session), session

    return build


# --- the handshake ------------------------------------------------------------------


def test_both_sides_derive_the_same_key():
    assert dh_shared_key(dh_public(SERVER_PRIVATE), CLIENT_PRIVATE) == SHARED_KEY
    assert len(SHARED_KEY) == 64


def test_aes_round_trips_and_the_key_is_the_iv():
    plain = b"APIKey=x&WebSiteHost=https%3A%2F%2Fgakcomic.gakken.jp&TokenID=t&WithKey=true"
    data = aes_encrypt(SHARED_KEY, plain)
    assert len(data) % 16 == 0
    assert aes_decrypt(SHARED_KEY, data) == plain


def test_aes_decrypt_rejects_a_partial_block_and_the_wrong_key():
    with pytest.raises(GetjmangaError, match="whole number"):
        aes_decrypt(SHARED_KEY, b"\x00" * 17)
    with pytest.raises(GetjmangaError, match="padding"):
        aes_decrypt("00" * 32, aes_encrypt(SHARED_KEY, b"hello"))


def test_rc4_matches_the_rfc_6229_vector():
    key = bytes.fromhex("0102030405060708090a0b0c0d0e0f10")
    assert rc4(key, bytes(16)) == bytes.fromhex("9ac7cc9a609d1ef7b2932899cde41b97")
    assert rc4(key, rc4(key, b"Plaintext")) == b"Plaintext"


# --- the container ---------------------------------------------------------------------


def test_decode_container_passes_a_plain_file_through():
    assert decode_container(b"<?xml version", HEX_KEY) == b"<?xml version"


def test_decode_container_uses_the_content_key_for_type_3():
    assert decode_container(encrypted_page((1, 2, 3)), HEX_KEY) == page_xhtml((1, 2, 3))


def test_decode_container_reads_the_embedded_key_of_type_1():
    key = bytes(range(16))
    assert decode_container(container(1, key + rc4(key, b"page")), "") == b"page"


def test_decode_container_inverts_type_2():
    assert decode_container(container(2, bytes([0xFF ^ b for b in b"page"])), "") == b"page"


def test_decode_container_rejects_an_unknown_type():
    with pytest.raises(GetjmangaError, match="type 9"):
        decode_container(container(9, b"x"), HEX_KEY)


def test_inline_image_reads_the_data_uri():
    assert inline_image(page_xhtml((1, 2, 3))) == png((1, 2, 3))
    with pytest.raises(GetjmangaError, match="no image"):
        inline_image(b"<html/>")


# --- urls --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (WORK_URL, True),
        (f"{BASE_URL}/comic/page-1020632500", True),
        (f"{BASE_URL}/comic/page-5byo-kotowaza/?sort=asc", True),
        (EPISODE_URL, True),
        (VIEWER_URL, True),
        (f"{BASE_URL}/viewer?content_id={CID_1}", True),
        (f"{WORK_URL}#comments", False),
        (f"{BASE_URL}/viewer/", False),
        (f"{BASE_URL}/viewer/?content_id=short", False),
        (f"{BASE_URL}/comic/", False),
        (f"{BASE_URL}/entries/", False),
        ("http://gakcomic.gakken.jp/comic/page-1020632500/", False),
        ("https://example.com/comic/page-1020632500/", False),
    ],
)
def test_suitable(url, expected):
    assert Gakcomic.suitable(url) is expected


def test_is_series_means_a_work_page_without_an_episode():
    assert Gakcomic.is_series(WORK_URL)
    assert Gakcomic.is_series(f"{BASE_URL}/comic/page-1020632500")
    assert not Gakcomic.is_series(EPISODE_URL)
    assert not Gakcomic.is_series(VIEWER_URL)
    assert not Gakcomic.is_series("https://example.com/comic/page-1020632500/")


# --- reading an episode --------------------------------------------------------------------


def test_episode_reads_the_titles_the_pages_and_the_next_episode(client):
    gakcomic, session = client()
    episode = gakcomic.episode(EPISODE_URL)

    assert episode.series_title == "うまくなる卓球"
    assert (episode.writer, episode.publisher) == (
        "大富寺航（まんが）, 山口隆一（ぐっちぃ）【ＷＲＭ】（監修）",
        "Gakken",
    )
    assert episode.episode_title == "第１章"
    assert [page.url for page in episode.pages] == [
        f"{LIBRARY}OEBPS/text/book_000.xhtml",
        f"{LIBRARY}OEBPS/text/book_002.xhtml",
    ]
    assert episode.pages[0].extra == {"key": HEX_KEY}
    assert (episode.pages[0].width, episode.pages[0].height) == (1566, 1912)
    assert (episode.prev_url, episode.next_url) == (None, f"{WORK_URL}#episode-17833")
    assert episode.number == 1
    assert episode.metadata["content_id"] == CID_1
    assert episode.metadata["episode_id"] == 17830
    assert episode.metadata["title"] == "うまくなる卓球　第１章"
    assert "hexkey" not in episode.metadata
    json.dumps(episode.metadata)
    # The work page is asked for oldest first, the viewer for its token.
    assert session.calls[0] == WORK_URL
    assert session.params_seen[0] == {"sort": "asc"}
    assert session.params_seen[session.calls.index(f"{BASE_URL}/viewer/")] == {"content_id": CID_1}


def test_episode_is_dated_by_its_first_page_upload(client, fake_response, uploaded):
    gakcomic, _ = client({"book_000.xhtml": fake_response(encrypted_page((10, 20, 30)), headers=uploaded)})
    assert gakcomic.episode(EPISODE_URL).published == date(2025, 8, 21)


def test_episode_runs_the_handshake_the_viewer_runs(client):
    gakcomic, session = client()
    gakcomic.episode(EPISODE_URL)

    (exchange_url, exchange), (use_url, use) = session.posts
    assert exchange_url == "https://license.keyring.net/BookEnd/owner/session/key/v1/exchange"
    assert exchange == {"APIKey": API_KEY, "WebSiteHost": BASE_URL, "Y": dh_public(CLIENT_PRIVATE), "K": 2}
    assert use_url == "https://license.keyring.net/BookEnd/onetime/token/v1/use"
    assert use["SessionID"] == "session-1"
    query = aes_decrypt(SHARED_KEY, base64.b64decode(use["EncryptedData"])).decode()
    assert query == (f"APIKey={API_KEY}&WebSiteHost=https%3A%2F%2Fgakcomic.gakken.jp&TokenID={TOKEN}&WithKey=true")


def test_episode_reads_the_api_key_off_custom_js_or_falls_back(client, fake_response):
    gakcomic, session = client({"/viewer/custom.js": fake_response(text="'apiKey': 'other-key',")})
    gakcomic.episode(EPISODE_URL)
    assert session.posts[0][1]["APIKey"] == "other-key"

    gakcomic, session = client({"/viewer/custom.js": fake_response(text="nothing here")})
    gakcomic.episode(EPISODE_URL)
    assert session.posts[0][1]["APIKey"] == API_KEY


def test_episode_stops_at_the_last_episode(client):
    gakcomic, _ = client()
    episode = gakcomic.episode(f"{WORK_URL}#episode-17832")
    assert (episode.prev_url, episode.next_url) == (f"{WORK_URL}#episode-17833", None)
    assert episode.number == 3


def test_a_refused_token_makes_a_locked_episode(client, fake_response):
    refusal = token_answer(status=1401, description="This TokenID is invalid. Access limit exceeded.")
    gakcomic, _ = client({"onetime/token/v1/use": fake_response(payload=refusal)})
    episode = gakcomic.episode(EPISODE_URL)

    assert episode.pages == ()
    assert episode.series_title == "うまくなる卓球"
    assert episode.episode_title == "第１章"
    assert episode.next_url == f"{WORK_URL}#episode-17833"
    assert episode.metadata["status"] == 1401
    assert "Access limit" in episode.metadata["status_description"]


def test_an_unknown_content_id_is_no_episode(client, fake_response):
    gakcomic, _ = client({"/viewer/": fake_response(text=viewer_html(""))})
    with pytest.raises(NotAnEpisodePageError, match="opens no content"):
        gakcomic.episode(VIEWER_URL)


def test_a_viewer_page_without_the_viewer_is_no_episode(client, fake_response):
    gakcomic, _ = client({"/viewer/": fake_response(text="<html>maintenance</html>")})
    with pytest.raises(NotAnEpisodePageError, match="opens no content"):
        gakcomic.episode(EPISODE_URL)


def test_an_episode_the_work_does_not_list_is_no_episode(client):
    gakcomic, _ = client()
    with pytest.raises(NotAnEpisodePageError, match="lists no episode 1"):
        gakcomic.episode(f"{WORK_URL}#episode-1")


def test_episode_refuses_a_work_page(client):
    gakcomic, _ = client()
    with pytest.raises(UnsupportedUrlError, match="not an episode"):
        gakcomic.episode(WORK_URL)


@pytest.mark.parametrize(
    "answer",
    [{"Y": dh_public(SERVER_PRIVATE), "S": "session-1"}, {**exchange_answer(), "H": "0" * 32}, []],
)
def test_a_broken_exchange_is_an_error(client, fake_response, answer):
    gakcomic, _ = client({"session/key/v1/exchange": fake_response(payload=answer)})
    with pytest.raises(GetjmangaError, match="license server"):
        gakcomic.episode(EPISODE_URL)


def test_an_epub_without_a_package_document_is_an_error(client, fake_response):
    gakcomic, _ = client({"META-INF/container.xml": fake_response(text="<container/>")})
    with pytest.raises(GetjmangaError, match="package document"):
        gakcomic.episode(EPISODE_URL)


# --- a viewer url on its own -------------------------------------------------------------------


def test_a_viewer_url_is_named_by_the_epub_title(client):
    gakcomic, session = client()
    episode = gakcomic.episode(VIEWER_URL)

    assert episode.series_title == "うまくなる卓球"
    assert episode.episode_title == "第１章"
    assert len(episode.pages) == 2
    assert episode.next_url is None
    assert "episode_id" not in episode.metadata
    assert WORK_URL not in session.calls


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("メンダコメンタン　ヤバイ！深海モンスターズ 第２話", ("メンダコメンタン　ヤバイ！深海モンスターズ", "第２話")),
        ("たいむすりっぷ？　Y2K", ("たいむすりっぷ？", "Y2K")),
        ("一語", ("一語", "一語")),
        ("", (CID_1, CID_1)),
    ],
)
def test_the_epub_title_splits_at_its_last_space(client, fake_response, title, expected):
    gakcomic, _ = client({"onetime/token/v1/use": fake_response(payload=token_answer({"title": title}))})
    episode = gakcomic.episode(VIEWER_URL)
    assert (episode.series_title, episode.episode_title) == expected


def test_a_viewer_url_is_named_by_a_work_seen_earlier(client):
    gakcomic, _ = client()
    gakcomic.series_urls(WORK_URL)
    episode = gakcomic.episode(f"{BASE_URL}/viewer/?content_id={CID_2}")

    assert episode.series_title == "うまくなる卓球"
    assert episode.episode_title == "第２章"
    assert episode.next_url == f"{WORK_URL}#episode-17832"
    assert episode.metadata["episode_id"] == 17833
    assert episode.metadata["work_url"] == WORK_URL


# --- a one-image episode ---------------------------------------------------------------------------


def test_a_one_image_episode_has_the_image_as_its_page(client):
    gakcomic, session = client()
    episode = gakcomic.episode(f"{FOUR_KOMA_URL}#episode-17341")

    assert episode.series_title == "ガッコミ出張版"
    assert episode.episode_title == "はじめに"
    assert [page.url for page in episode.pages] == ["https://s3.example/uploads/hajimeni.jpg"]
    assert episode.pages[0].extra == {}
    assert episode.next_url == f"{FOUR_KOMA_URL}#episode-17373"
    assert episode.metadata == {
        "episode_id": 17341,
        "work_url": FOUR_KOMA_URL,
        "work_title": "ガッコミ出張版",
        "image": "https://s3.example/uploads/hajimeni.jpg",
    }
    assert session.posts == []


# --- listing a work ---------------------------------------------------------------------------------


def test_series_urls_lists_the_episodes_oldest_first_without_repeats(client):
    gakcomic, session = client()
    urls = gakcomic.series_urls(f"{BASE_URL}/comic/page-1020632500?sort=desc")

    assert urls == [f"{WORK_URL}#episode-{post_id}" for post_id in (17830, 17833, 17832)]
    assert all(Gakcomic.suitable(url) for url in urls)
    assert session.calls == [WORK_URL]
    assert session.params_seen == [{"sort": "asc"}]


def test_series_urls_names_the_work_from_the_title_when_the_heading_is_missing(client, fake_response):
    html = WORK_HTML.replace('<h1 class="pg-book-meta__title">', '<h2 class="other">')
    gakcomic, _ = client({"/comic/page-1020632500/": fake_response(text=html)})
    assert gakcomic.episode(EPISODE_URL).series_title == "うまくなる卓球"


def test_series_urls_rejects_a_work_without_episodes(client, fake_response):
    gakcomic, _ = client({"/comic/page-1020632500/": fake_response(text=work_html([]))})
    with pytest.raises(NotAnEpisodePageError, match="lists no episode"):
        gakcomic.series_urls(WORK_URL)


def test_series_urls_rejects_an_episode_url(client):
    gakcomic, _ = client()
    with pytest.raises(UnsupportedUrlError, match="not a work page"):
        gakcomic.series_urls(EPISODE_URL)


# --- downloading --------------------------------------------------------------------------------------


def test_download_writes_the_pages_read_out_of_the_epub(client, tmp_path):
    gakcomic, session = client()
    result = Downloader(gakcomic, tmp_path).download(EPISODE_URL)

    assert result.status == "saved"
    assert result.save_dir == tmp_path / "gakcomic.gakken.jp" / "うまくなる卓球" / "第１章"
    assert sorted(path.name for path in result.save_dir.iterdir()) == ["0.jpg", "1.jpg"]
    with Image.open(result.save_dir / "0.jpg") as saved:
        assert saved.getpixel((4, 4)) == pytest.approx((10, 20, 30), abs=8)
    with Image.open(result.save_dir / "1.jpg") as saved:
        assert saved.getpixel((4, 4)) == pytest.approx((40, 50, 60), abs=8)
    assert session.calls[-1] == f"{LIBRARY}OEBPS/text/book_002.xhtml"


# --- the real site ---------------------------------------------------------------------------------------

TEST_URLS: dict[str, str] = {
    "gakcomic.gakken.jp": "https://gakcomic.gakken.jp/comic/page-1020632500/#episode-17830",
}


@pytest.mark.network
@pytest.mark.parametrize("host", TEST_URLS)
def test_site_download(tmp_path, host):
    result = Downloader(Gakcomic(), tmp_path, only_first=True).download(TEST_URLS[host])
    assert result.status == "saved"
    assert (result.save_dir / "0.jpg").stat().st_size > 0


@pytest.mark.network
def test_work_page_lists_episodes():
    urls = Gakcomic().series_urls("https://gakcomic.gakken.jp/comic/page-1020632500/")
    assert urls[0] == TEST_URLS["gakcomic.gakken.jp"]
    assert all(Gakcomic.suitable(url) for url in urls)


@pytest.mark.network
def test_a_viewer_url_downloads_on_its_own(tmp_path):
    result = Downloader(Gakcomic(), tmp_path, only_first=True).download(
        "https://gakcomic.gakken.jp/viewer/?content_id=0f3fcbd2b42749f788bfdc16731af5a9",
    )
    assert result.status == "saved"
    assert result.save_dir == tmp_path / "gakcomic.gakken.jp" / "うまくなる卓球" / "第１章"


@pytest.mark.network
def test_a_one_image_episode_downloads(tmp_path):
    result = Downloader(Gakcomic(), tmp_path, only_first=True).download(
        "https://gakcomic.gakken.jp/comic/page-1020620800/#episode-17341",
    )
    assert result.status == "saved"
    assert result.episode.episode_title == "はじめに"
