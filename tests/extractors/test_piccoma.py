from __future__ import annotations

import re
from io import BytesIO

import pytest
from PIL import Image

from getjmanga.downloader import Downloader
from getjmanga.errors import GetjmangaError, LoginError, NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractors.piccoma import BASE_URL, TILE_SIZE, Entry, Piccoma, parse_seed

# The checksum and the `expires` stamp of a real page image, and the seed the
# viewer derives from them.
CHECKSUM = "G0UQD7CENPH26H9YIEIYEU"
EXPIRES = "1788372000"
SEED = "OQI26H8XIDIYETF0TQD7BD"
IMAGE_URL = f"//pcm.kakaocdn.net/dna/ta9nw/btqGHxiZm9S/{CHECKSUM}/i00001.jpg?credential=abc&expires={EXPIRES}"

VIEWER_HTML = f"""
<html><head>
<title>第1話 その一｜ひげ(しめさば)｜ピッコマ</title>
<meta property="og:title" content="第1話 その一｜ひげ(しめさば)｜ピッコマ">
</head><body>
<script>
    var _init_ = {{
        'login': false,
        'os': 'PC'
    }}
</script>
<script>
    var _pdata_ = {{
        'product_id': 8195,
        'episode_id': 1185884,
        'is_bookmark': 0,
        'title': '第1話 その一',
        'isScrambled': true,
        'eType': 'E',
        'img': [
        {{'path':'{IMAGE_URL}','width':1441, 'height':2048}},
        {{'path':'{IMAGE_URL.replace("i00001", "i00002")}','width':1441, 'height':2048}}
        ],
        'for_viewer_end': {{"ticket_type": "FREE"}},
        reduction_balloon_text: null,
    }}
</script>
</body></html>
"""

# The last episode of the list, and a locked one with an empty `img` array.
LAST_HTML = VIEWER_HTML.replace("1185884", "1185887")
LOCKED_HTML = re.sub(r"'img': \[.*?\]", "'img': [\n        ]", VIEWER_HTML, flags=re.DOTALL)

EPISODE_LIST_HTML = """
<html><head>
<meta property="og:title" content="ひげ｜無料漫画（まんが）ならピッコマ｜しめさば">
</head><body>
<ul id="js_episodeList">
  <li class="PCM-epList_read">
    <a href="#" data-product_id="8195" data-episode_id="1185884">
      <div class="PCM-epList_title"><h2>第1話 その一</h2></div>
      <div class="PCM-epList_status"><p class="PCM-epList_status_free"><span>0</span></p></div>
    </a>
  </li>
  <li>
    <a href="#" data-product_id="8195" data-episode_id="1185887">
      <div class="PCM-epList_title"><h2>第2話 その二</h2></div>
      <div class="PCM-epList_status">
        <div class="PCM-epList_status_waitfree"></div>
        <div class="PCM-epList_status_zeroPlus"></div>
      </div>
    </a>
  </li>
</ul>
</body></html>
"""

VOLUME_LIST_HTML = """
<html><body>
<ul id="js_volumeList">
  <li class="PCM-volList_read">
    <div class="PCM-prdVol_title"><h2>1巻</h2></div>
    <div class="PCM-prdVol_btns"><a href="#" class="PCM-prdVol_readBtn" data-episode_id="900"></a></div>
  </li>
</ul>
</body></html>
"""

SIGNIN_HTML = """
<html><body>
<script>var _init_ = { 'login': false }</script>
<form><input type="hidden" name="csrfmiddlewaretoken" value="token-1"></form>
</body></html>
"""

SIGNED_IN_HTML = SIGNIN_HTML.replace("'login': false", "'login': true")

VIEWER_URL = f"{BASE_URL}/web/viewer/8195/1185884"


def tile_image(values, columns=4, rows=3, tile=TILE_SIZE):
    """A grid of flat tiles, tile n painted with a colour derived from values[n]."""
    image = Image.new("RGB", (columns * tile, rows * tile))
    for index, value in enumerate(values):
        row, column = divmod(index, columns)
        patch = Image.new("RGB", (tile, tile), (value * 7 % 256, value * 13 % 256, value * 29 % 256))
        image.paste(patch, (column * tile, row * tile))
    return image


@pytest.fixture
def client(fake_session, fake_response):
    def build(routes=None):
        # Routes are matched in order, so the caller's own come first, and the
        # defaults fill in behind them from the most specific to the least.
        merged = dict(routes or {})
        for needle, response in (
            ("/web/viewer/8195/1185887", fake_response(text=LAST_HTML)),
            ("/web/viewer/", fake_response(text=VIEWER_HTML)),
            ("/web/product/8195/episodes?etype=E", fake_response(text=EPISODE_LIST_HTML)),
            ("/web/product/8195/episodes?etype=V", fake_response(text=VOLUME_LIST_HTML)),
        ):
            merged.setdefault(needle, response)
        session = fake_session(merged)
        return Piccoma(session), session

    return build


@pytest.fixture
def image_routes(fake_response):
    raw = BytesIO()
    Image.new("RGB", (4 * TILE_SIZE, 3 * TILE_SIZE)).save(raw, "JPEG")
    return {"kakaocdn.net": fake_response(raw.getvalue())}


# --- the seed -----------------------------------------------------------------


def test_parse_seed_reads_the_seed_off_an_image_url():
    assert parse_seed(f"https:{IMAGE_URL}") == SEED


def test_parse_seed_ignores_unscrambled_pages():
    assert parse_seed(f"https:{IMAGE_URL.replace(CHECKSUM, CHECKSUM.lower())}") is None


def test_parse_seed_needs_an_expires_stamp():
    with pytest.raises(GetjmangaError, match="expires"):
        parse_seed(f"https://pcm.kakaocdn.net/dna/{CHECKSUM}/i00001.jpg")


def test_parse_seed_needs_a_checksum_segment():
    with pytest.raises(GetjmangaError, match="checksum"):
        parse_seed("https://pcm.kakaocdn.net/i00001.jpg?expires=1")


# --- urls ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://piccoma.com/web/viewer/8195/1185884",
        "https://piccoma.com/web/viewer/8195/1185884/",
        "https://piccoma.com/web/product/8195/episodes",
        "https://piccoma.com/web/product/8195/episodes?etype=V",
        "https://piccoma.com/web/product/8195",
    ],
)
def test_suitable_accepts_piccoma_urls(url):
    assert Piccoma.suitable(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://piccoma.com/web/viewer/8195/1185884",
        "https://piccoma.com/web/bookshelf/history",
        "https://example.com/web/viewer/8195/1185884",
        "https://piccoma.com/web/viewer/abc/def",
        "not a url",
    ],
)
def test_suitable_rejects_everything_else(url):
    assert not Piccoma.suitable(url)


def test_is_series_means_a_product_page():
    assert Piccoma.is_series("https://piccoma.com/web/product/8195/episodes")
    assert Piccoma.is_series("https://piccoma.com/web/product/8195")
    assert not Piccoma.is_series(VIEWER_URL)
    assert not Piccoma.is_series("https://example.com/web/product/8195")


# --- reading a viewer page ----------------------------------------------------


def test_episode_reads_the_titles_and_the_pages(client):
    piccoma, _ = client()
    episode = piccoma.episode(VIEWER_URL)

    assert episode.series_title == "ひげ(しめさば)"
    assert episode.episode_title == "第1話 その一"
    assert [page.url for page in episode.pages] == [
        f"https:{IMAGE_URL}",
        f"https:{IMAGE_URL}".replace("i00001", "i00002"),
    ]
    assert episode.pages[0].width == 1441
    assert episode.pages[0].extra == {"scrambled": True}
    assert episode.next_url == f"{BASE_URL}/web/viewer/8195/1185887"
    assert episode.metadata == {
        "product_id": "8195",
        "episode_id": "1185884",
        "episode_type": "E",
        "scrambled": True,
    }


def test_episode_stops_at_the_last_episode(client):
    piccoma, _ = client()
    episode = piccoma.episode(f"{BASE_URL}/web/viewer/8195/1185887")
    assert (episode.prev_url, episode.next_url) == (f"{BASE_URL}/web/viewer/8195/1185884", None)


def test_episode_rejects_a_page_without_a_viewer(fake_session, fake_response):
    piccoma = Piccoma(fake_session({"/web/viewer/": fake_response(text="<html></html>")}))
    with pytest.raises(NotAnEpisodePageError, match="_pdata_"):
        piccoma.episode(VIEWER_URL)


def test_episode_refuses_a_product_url(client):
    piccoma, _ = client()
    with pytest.raises(UnsupportedUrlError, match="product page"):
        piccoma.episode("https://piccoma.com/web/product/8195/episodes")


def test_an_episode_with_no_pages_is_not_readable(client, fake_response):
    piccoma, _ = client({"/web/viewer/": fake_response(text=LOCKED_HTML)})
    episode = piccoma.episode(VIEWER_URL)
    assert episode.pages == ()
    assert episode.episode_title == "第1話 その一"


def test_episode_reports_an_episode_it_was_bounced_off(client, fake_response):
    signin = f"{BASE_URL}/web/acc/signin?next_url=/web/viewer/s/8195/1185887"
    piccoma, _ = client({"/web/viewer/8195/1185887": fake_response(text=SIGNIN_HTML, url=signin)})

    episode = piccoma.episode(f"{BASE_URL}/web/viewer/8195/1185887")

    assert episode.pages == ()
    # The titles come off the series' list, since the sign-in page names neither.
    assert episode.series_title == "ひげ"
    assert episode.episode_title == "第2話 その二"
    assert episode.metadata["episode_id"] == "1185887"


def test_episode_names_an_unlisted_locked_episode_by_its_id(client, fake_response):
    signin = f"{BASE_URL}/web/acc/signin"
    piccoma, _ = client({"/web/viewer/8195/999": fake_response(text=SIGNIN_HTML, url=signin)})
    assert piccoma.episode(f"{BASE_URL}/web/viewer/8195/999").episode_title == "999"


# --- listing a product --------------------------------------------------------


def test_entries_reads_the_episode_list(client):
    piccoma, _ = client()
    entries = piccoma.entries("8195")

    assert [entry.id for entry in entries] == ["1185884", "1185887"]
    assert [entry.title for entry in entries] == ["第1話 その一", "第2話 その二"]
    assert entries[0].url == VIEWER_URL


def test_entries_reads_the_volume_list(client):
    piccoma, _ = client()
    assert piccoma.entries("8195", "V") == [Entry(id="900", title="1巻", url=f"{BASE_URL}/web/viewer/8195/900")]


def test_entries_survives_a_product_without_a_list(fake_session, fake_response):
    piccoma = Piccoma(fake_session({"/web/product/": fake_response(text="<html></html>")}))
    assert piccoma.entries("8195") == []


def test_series_title_comes_off_the_product_page(client):
    piccoma, _ = client()
    assert piccoma.series_title("8195") == "ひげ"


def test_series_title_falls_back_to_the_product_id(fake_session, fake_response):
    piccoma = Piccoma(fake_session({"/web/product/": fake_response(text="<html></html>")}))
    assert piccoma.series_title("8195") == "8195"


def test_series_urls_lists_the_episodes(client):
    piccoma, _ = client()
    assert piccoma.series_urls("https://piccoma.com/web/product/8195/episodes") == [
        VIEWER_URL,
        f"{BASE_URL}/web/viewer/8195/1185887",
    ]


@pytest.mark.parametrize("etype", ["V", "v", "volume"])
def test_series_urls_lists_the_volumes_when_asked(client, etype):
    piccoma, _ = client()
    assert piccoma.series_urls(f"https://piccoma.com/web/product/8195/episodes?etype={etype}") == [
        f"{BASE_URL}/web/viewer/8195/900",
    ]


def test_series_urls_rejects_an_empty_product(fake_session, fake_response):
    piccoma = Piccoma(fake_session({"/web/product/": fake_response(text="<html></html>")}))
    with pytest.raises(NotAnEpisodePageError, match="lists no episodes"):
        piccoma.series_urls("https://piccoma.com/web/product/8195")


# --- downloading --------------------------------------------------------------


def test_download_saves_every_page(client, image_routes, tmp_path):
    piccoma, session = client(image_routes)
    result = Downloader(piccoma, tmp_path).download(VIEWER_URL)

    assert result.status == "saved"
    assert result.episode.next_url == f"{BASE_URL}/web/viewer/8195/1185887"
    assert result.save_dir == tmp_path / "piccoma.com" / "ひげ(しめさば)" / "第1話 その一"
    assert sorted(path.name for path in result.save_dir.iterdir()) == ["0.jpg", "1.jpg"]
    assert session.headers_seen[-1]["Referer"] == VIEWER_URL


def test_image_leaves_an_unscrambled_page_alone(client, fake_response):
    raw = BytesIO()
    tile_image(range(12)).save(raw, "PNG")
    html = VIEWER_HTML.replace("'isScrambled': true", "'isScrambled': false")
    piccoma, _ = client({"/web/viewer/": fake_response(text=html), "kakaocdn.net": fake_response(raw.getvalue())})
    episode = piccoma.episode(VIEWER_URL)

    assert episode.pages[0].extra == {"scrambled": False}
    assert piccoma.image(episode.pages[0], episode).tobytes() == tile_image(range(12)).tobytes()


# --- logging in ---------------------------------------------------------------


def test_login_posts_the_csrf_token_back(fake_session, fake_response):
    session = fake_session(
        {"/web/acc/email/signin": [fake_response(text=SIGNIN_HTML), fake_response(text=SIGNED_IN_HTML)]},
    )
    piccoma = Piccoma(session)

    piccoma.login(VIEWER_URL, "someone@example.com", "hunter2")

    assert piccoma.logged_in
    url, data = session.posts[0]
    assert url.endswith("/web/acc/email/signin")
    assert data["csrfmiddlewaretoken"] == "token-1"
    assert data["email"] == "someone@example.com"
    assert data["password"] == "hunter2"


def test_login_raises_when_piccoma_says_no(fake_session, fake_response):
    piccoma = Piccoma(fake_session({"/web/acc/email/signin": fake_response(text=SIGNIN_HTML)}))

    with pytest.raises(LoginError, match="refused"):
        piccoma.login(VIEWER_URL, "someone@example.com", "wrong")

    assert not piccoma.logged_in


def test_login_raises_without_a_form(fake_session, fake_response):
    piccoma = Piccoma(fake_session({"/web/acc/email/signin": fake_response(text="<html></html>")}))
    with pytest.raises(LoginError, match="no sign-in form"):
        piccoma.login(VIEWER_URL, "someone@example.com", "hunter2")


# --- the real site --------------------------------------------------------------


# One episode per known host, free to read without an account.
TEST_URLS: dict[str, str] = {
    "piccoma.com": "https://piccoma.com/web/viewer/8195/1185884",
}


@pytest.mark.network
@pytest.mark.geoblocked
@pytest.mark.parametrize("host", TEST_URLS)
def test_site_download(tmp_path, host):
    result = Downloader(Piccoma(), tmp_path, only_first=True).download(TEST_URLS[host])
    assert result.status == "saved"
    assert (result.save_dir / "0.jpg").exists()
