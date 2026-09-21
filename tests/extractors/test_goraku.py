from __future__ import annotations

import json
from http import HTTPStatus
from io import BytesIO

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from PIL import Image

from getjmanga.downloader import Downloader
from getjmanga.errors import NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractors.goraku import BASE_URL, Goraku

TITLE_ID = "2319284769883687304"
WORK_URL = f"{BASE_URL}/episode/{TITLE_ID}"
FIRST_URL = f"{WORK_URL}/3274159097835255566"
SECOND_URL = f"{WORK_URL}/7292859689346124972"
LOCKED_URL = f"{WORK_URL}/6364663364950346898"
CDN_BASE = (
    "https://gorakuweb-content.akamaized.net/cloud/8832056972476726230/7045725686578979035/3022500173479969201/1/1/c9a8"
)
ACCESS_KEY = (
    "st=1789703233~exp=1789714033~acl=/cloud/8832056972476726230/*~hmac=53502d0dcc899a7b5a160c19c9e08431b4d266b3"
)
KEY = "a6c93b7819de40607729021727427ce4"
IV = "f7ca242eca06e4f81d284de17b667921"

EPISODE_LIST = [
    {"href": f"/episode/{TITLE_ID}/6364663364950346898", "title": "第十六話 真摯", "status": "closed"},
    {"href": f"/episode/{TITLE_ID}/7292859689346124972", "title": "第二話 【剣想】", "status": "opened"},
    {"href": f"/episode/{TITLE_ID}/7292859689346124972", "title": "第二話 【剣想】", "status": "opened"},
    {"href": f"/episode/{TITLE_ID}/3274159097835255566", "title": "第一話 腐神と赤錆", "status": "opened"},
]


def props(**overrides):
    base = {
        "titleId": TITLE_ID,
        "episodeId": "3274159097835255566",
        "base": CDN_BASE,
        "metadata": {
            "count": 2,
            "pages": [
                {"page": 1, "filename": "1", "width": 1600, "height": 2275, "spread": "center"},
                {"page": 2, "filename": "2", "width": 1600, "height": 2275, "spread": "right"},
            ],
        },
        "accessKey": ACCESS_KEY,
        "keyBytes": KEY,
        "ivBytes": IV,
        "title": "第一話 腐神と赤錆",
        "seriesTitle": "堕ちた剣聖、腐神に拾われる",
        "author": "谷川人鳥 田鵺功空",
        "episodeList": EPISODE_LIST,
        "episodeType": "hasNext",
        "prevEpisodeUrl": "$undefined",
        "nextEpisodeUrl": f"/episode/{TITLE_ID}/7292859689346124972",
        "nextOpenedEpisodeUrl": "$undefined",
    }
    base.update(overrides)
    return base


def flight_html(episode=None, *, split_at=None):
    """An episode page the way Next.js streams it: rows pushed in chunks, the props row among them."""
    rows = ['1:"$Sreact.fragment"', '3:I[5244,[],""]']
    if episode is not None:
        rows.append("6:" + json.dumps(["$", "$L18", None, episode], ensure_ascii=False))
    rows.append("8:null")
    payload = "\n".join(rows) + "\n"
    chunks = [payload] if split_at is None else [payload[:split_at], payload[split_at:]]
    scripts = "".join(
        f"<script>self.__next_f.push({json.dumps([1, chunk], ensure_ascii=False)})</script>" for chunk in chunks
    )
    head = "<!DOCTYPE html><html><head><title>x</title></head><body>"
    return f"{head}<script>self.__next_f.push([0])</script>{scripts}</body></html>"


LOCKED = props(
    episodeId="6364663364950346898",
    base=None,
    metadata=None,
    accessKey=None,
    keyBytes=None,
    ivBytes=None,
    title="第十六話 真摯",
    prevEpisodeUrl=f"/episode/{TITLE_ID}/1145059663106682550",
    nextEpisodeUrl=f"/episode/{TITLE_ID}/1145059663106682552",
    nextOpenedEpisodeUrl=f"/episode/{TITLE_ID}/1145059663106682552",
)


def encrypt(data: bytes) -> bytes:
    pad = 16 - len(data) % 16
    encryptor = Cipher(algorithms.AES(bytes.fromhex(KEY)), modes.CBC(bytes.fromhex(IV))).encryptor()
    return encryptor.update(data + bytes([pad]) * pad) + encryptor.finalize()


def webp_bytes(color=(200, 30, 30)) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (32, 48), color).save(buffer, format="WEBP", lossless=True)
    return buffer.getvalue()


# --- URLs ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (FIRST_URL, True),
        (FIRST_URL + "/", True),
        (WORK_URL, True),
        (WORK_URL + "/", True),
        (FIRST_URL.replace("https://", "http://"), False),
        ("https://www.gorakuweb.com/episode/1/2", False),
        ("https://comic-days.com/episode/2319284769883687304", False),
        (f"{BASE_URL}/series", False),
        (f"{BASE_URL}/episode/abc", False),
        (f"{BASE_URL}/episode/1/2/3", False),
    ],
)
def test_suitable(url, expected):
    assert Goraku.suitable(url) is expected


def test_is_series_only_for_a_work_url():
    assert Goraku.is_series(WORK_URL)
    assert not Goraku.is_series(FIRST_URL)


# --- the flight payload ----------------------------------------------------------


# --- episode() -------------------------------------------------------------------


def test_episode_reads_the_titles_and_the_pages(fake_session, fake_response):
    session = fake_session({"/episode/": fake_response(text=flight_html(props(), split_at=500))})
    episode = Goraku(session).episode(FIRST_URL)

    assert episode.url == FIRST_URL
    assert episode.series_title == "堕ちた剣聖、腐神に拾われる"
    assert (episode.writer, episode.publisher) == ("谷川人鳥 田鵺功空", "日本文芸社")
    assert episode.episode_title == "第一話 腐神と赤錆"
    assert [page.url for page in episode.pages] == [
        f"{CDN_BASE}/1?__token__={ACCESS_KEY}",
        f"{CDN_BASE}/2?__token__={ACCESS_KEY}",
    ]
    assert (episode.pages[0].width, episode.pages[0].height) == (1600, 2275)
    assert episode.pages[0].extra == {"key": KEY, "iv": IV}
    assert episode.next_url == SECOND_URL
    assert episode.metadata["prevEpisodeUrl"] is None
    json.dumps(episode.metadata)
    assert session.calls == [FIRST_URL]
    assert session.params_seen == [None]


def test_episode_at_the_end_of_the_series_has_no_next(fake_session, fake_response):
    session = fake_session({"/episode/": fake_response(text=flight_html(props(nextEpisodeUrl="$undefined")))})
    assert Goraku(session).episode(FIRST_URL).next_url is None


def test_locked_episode_has_no_pages_but_a_next(fake_session, fake_response):
    session = fake_session({"/episode/": fake_response(text=flight_html(LOCKED))})
    episode = Goraku(session).episode(LOCKED_URL)

    assert episode.pages == ()
    assert not episode.readable
    assert episode.episode_title == "第十六話 真摯"
    assert (episode.prev_url, episode.next_url) == (
        f"{WORK_URL}/1145059663106682550",
        f"{WORK_URL}/1145059663106682552",
    )


def test_work_url_reads_the_episode_it_renders(fake_session, fake_response):
    session = fake_session({"/episode/": fake_response(text=flight_html(props()))})
    episode = Goraku(session).episode(WORK_URL)

    assert episode.url == FIRST_URL
    assert len(episode.pages) == 2


def test_episode_without_the_component_is_not_an_episode(fake_session, fake_response):
    session = fake_session({"/episode/": fake_response(text=flight_html(None))})
    with pytest.raises(NotAnEpisodePageError, match="no episode on"):
        Goraku(session).episode(FIRST_URL)


@pytest.mark.parametrize("status", [HTTPStatus.NOT_FOUND, HTTPStatus.INTERNAL_SERVER_ERROR])
def test_unknown_id_is_not_an_episode(fake_session, fake_response, status):
    session = fake_session({"/episode/": fake_response(text="<html>error</html>", status_code=status)})
    with pytest.raises(NotAnEpisodePageError, match=str(int(status))):
        Goraku(session).episode(f"{BASE_URL}/episode/1234567890123456789")


def test_episode_rejects_other_urls(fake_session):
    with pytest.raises(UnsupportedUrlError):
        Goraku(fake_session({})).episode(f"{BASE_URL}/series")


# --- series_urls() ---------------------------------------------------------------


def test_series_urls_lists_oldest_first_without_duplicates(fake_session, fake_response):
    session = fake_session({"/episode/": fake_response(text=flight_html(props()))})
    urls = Goraku(session).series_urls(WORK_URL)

    assert urls == [FIRST_URL, SECOND_URL, LOCKED_URL]
    assert all(Goraku.suitable(url) for url in urls)
    assert session.calls == [WORK_URL]


def test_series_urls_rejects_an_episode_url(fake_session):
    with pytest.raises(UnsupportedUrlError):
        Goraku(fake_session({})).series_urls(FIRST_URL)


def test_series_urls_raises_on_an_empty_listing(fake_session, fake_response):
    session = fake_session({"/episode/": fake_response(text=flight_html(props(episodeList=[])))})
    with pytest.raises(NotAnEpisodePageError, match="lists no episode"):
        Goraku(session).series_urls(WORK_URL)


# --- image() and the download ----------------------------------------------------


def test_image_decrypts_the_page_file(fake_session, fake_response):
    session = fake_session(
        {
            "/episode/": fake_response(text=flight_html(props())),
            "__token__": fake_response(encrypt(webp_bytes()), content_type="application/octet-stream"),
        },
    )
    goraku = Goraku(session)
    episode = goraku.episode(FIRST_URL)
    image = goraku.image(episode.pages[0], episode)

    assert image.size == (32, 48)
    assert image.convert("RGB").getpixel((0, 0)) == (200, 30, 30)
    assert session.headers_seen[-1]["Referer"] == FIRST_URL
    assert session.calls[-1] == f"{CDN_BASE}/1?__token__={ACCESS_KEY}"


def test_image_is_served_as_is_without_a_key(fake_session, fake_response):
    session = fake_session(
        {
            "/episode/": fake_response(text=flight_html(props(keyBytes=None, ivBytes=None))),
            "__token__": fake_response(webp_bytes((0, 0, 255)), content_type="image/webp"),
        },
    )
    goraku = Goraku(session)
    episode = goraku.episode(FIRST_URL)
    assert goraku.image(episode.pages[0], episode).convert("RGB").getpixel((0, 0)) == (0, 0, 255)


# --- the real site ---------------------------------------------------------------

# One episode per known host, free to read without an account.
TEST_URLS: dict[str, str] = {
    "gorakuweb.com": "https://gorakuweb.com/episode/2319284769883687304/3274159097835255566",
}


@pytest.mark.network
@pytest.mark.parametrize("host", TEST_URLS)
def test_site_download(tmp_path, host):
    result = Downloader(Goraku(), tmp_path, only_first=True).download(TEST_URLS[host])
    assert result.status == "saved"
    assert (result.save_dir / "0.jpg").stat().st_size > 0


@pytest.mark.network
def test_work_page_lists_episodes():
    urls = Goraku().series_urls(WORK_URL)
    assert urls[0] == TEST_URLS["gorakuweb.com"]
    assert all(Goraku.suitable(url) for url in urls)
