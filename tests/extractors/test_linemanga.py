from __future__ import annotations

import json
from http import HTTPStatus
from io import BytesIO

import pytest
from PIL import Image

from getjmanga.downloader import Downloader
from getjmanga.extractors.common import NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractors.linemanga import (
    BASE_URL,
    INDIES_LIST_URL,
    PERIODIC_LIST_URL,
    LineManga,
    descramble,
    parse_option,
)

SERIES_URL = f"{BASE_URL}/product/periodic?id=Z0001684"
EPISODE_URL = f"{BASE_URL}/book/viewer?id=Z0090127"
LOCKED_URL = f"{BASE_URL}/book/viewer?id=Z0090134"
PRINT_URL = f"{BASE_URL}/book/viewer?id=B00163114127"
INDIES_SERIES_URL = f"{BASE_URL}/indies/product/detail?id=20421"
INDIES_URL = f"{BASE_URL}/indies/book/article?id=184117"
CDN = "https://cdn-manga-static-file-origin.ldf-static.net"

# The viewer page of a free webtoon episode, cut down to what is read.
WEBTOON_HTML = f"""<!DOCTYPE html><html><head><title>x</title></head><body>
    <div id="root"></div>
    <script>
      var imgs = {{}};
      var links = {{}};
        imgs[0] = {{
          'url':    '{CDN}/router/book/private/FFeZ7qqIlvhe4o8usLAUlGac.jpg',
          'height': 1230,
          'width': 700
        }};
        links[0] = [];
        imgs[1] = {{
          'url':    '{CDN}/router/book/private/gfk1FSUeSh7sFsvPFBsRWuMi.jpg',
          'height': 784,
          'width': 700
        }};
        links[1] = [];
      var portal_pages = {{}};
      var portal_tocs = [];
      var OPTION = {{
          API_PATH: '',
          productName: 'Let&#39;s レベルMAX',
          isLoggedIn: false,
          nickname: "",
          show_modal: 0,
          imgs: imgs,
          links: links,
          start_point: 1,
          bookId: 'Z0090127',
          productId: 'Z0001684',
          mediado_token: '',
          orientation: 'vertical',
          isPortal:  false ,
          portalPages: portal_pages,
          likedCount: 44912,
          title: 'プロローグ. すべての始まり',
          description: decodeURIComponent('%E3%82%B2'),
          valuation: null,
            next_book: {{
              id: 'Z0090128',
              name: '塔の頂上',
              volume: 1,
              thumbnail: '{CDN}/x/middle',
              isFree:  true ,
              isPurchased:  false ,
            }},
            commentCount: 676,
            maxChargeVolume:  263 ,
          free_volume_text: '',
        }};
        OPTION.bookType = 'periodic'
    </script>
</body></html>"""

# A print comic: `portal_pages` with a 2x2 block shuffle on a 6x6 page.
PRINT_HTML = f"""<html><body><script>
      var imgs = {{}};
      var links = {{}};
      var portal_pages = {{}};
        portal_pages[0] = {{
          'page_number': 1,
          'url':         '{CDN}/0hW4gfhTuu',
          'metadata': {{
            'hc':  2,
            'bwd': 2,
            'vc':  2,
            'iw':  6,
            'ih':  6,
            'm' :  [],
          }}
        }};
          portal_pages[0].metadata.m[0] = '3';
          portal_pages[0].metadata.m[1] = '2';
          portal_pages[0].metadata.m[2] = '1';
          portal_pages[0].metadata.m[3] = '0';
        portal_pages[1] = {{
          'page_number': 2,
          'url':         '{CDN}/0hAkoxTeKK',
          'metadata': {{
            'hc':  11,
            'bwd': 64,
            'vc':  18,
            'iw':  744,
            'ih':  1200,
            'm' :  [],
          }}
        }};
          portal_pages[1].metadata.m[0] = 'd';
          portal_pages[1].metadata.m[1] = '2x';
      var portal_tocs = [];
        portal_tocs[0] = {{
            'page_index': 0,
            'label'     : '本文',
        }};
      var OPTION = {{
          productName: 'フルーツバスケット',
          bookId: 'B00163114127',
          productId: 'S113701',
          orientation: 'horizontal',
          isPortal:  true ,
          title: '第1話 第1話(1)',
            next_book: {{
              id: 'B00163114128',
              name: '第2話 第1話(2)',
              volume: 2,
              isFree:  true ,
            }},
        }};
        OPTION.bookType = 'periodic'
</script></body></html>"""

INDIES_HTML = f"""<html><body><script>
      var imgs = {{}};
      var links = {{}};
        imgs[0] = {{
          'url':    '{CDN}/router/indies_book/LV8To6lt0ha7nQiypEdx5NxT.PNG',
          'height': 5016,
          'width': 3541
        }};
        links[0] = [];
      var OPTION = {{
          productName: 'クレイト！',
          bookId: '184117',
          productId: '20421',
          orientation: 'vertical',
          isPortal:  false ,
          title: 'クレイと仮想空間',
          authorComment: 'It\\'s fine',
          valuation: null,
        }};
        OPTION.bookType = 'indies'
</script></body></html>"""

NOT_FOUND_HTML = "<html><head><title>ページが見つかりません｜LINE マンガ</title></head><body></body></html>"


def book(book_id, name, volume, price=0):
    return {"id": book_id, "name": name, "volume": volume, "episode_volume": volume, "selling_price": price}


# `/api/book/product_list` for the serial, cut down.
LISTING = {
    "result": {
        "pager": {"rows": 1000, "page": 1, "hasNext": False},
        "error_code": 0,
        "max_episode_count": 3,
        "rows": [
            book("Z0090127", "プロローグ. すべての始まり", 0),
            book("Z0090128", "塔の頂上", 1),
            book("Z0090134", "国立中央博物館（２）", 6, price=67),
            book("Z0090136", "国立中央博物館（３）", 7, price=67),
        ],
        "product": {"id": "Z0001684", "name": "俺だけレベルMAXなビギナー", "is_periodic": True},
    }
}

# `/api/indies/book/product_list`, newest first.
INDIES_LISTING = {
    "result": {
        "rows": [
            {
                "id": "184117",
                "name": "クレイと仮想空間",
                "volume": 15,
                "product_name": "クレイト！",
                "product_id": "20421",
            },
            {"id": "183344", "name": "スイカ割り", "volume": 14, "product_name": "クレイト！", "product_id": "20421"},
            {"id": "176141", "name": "第1話", "volume": 1, "product_name": "クレイト！", "product_id": "20421"},
        ],
        "pager": {"rows": 1000, "page": 1, "hasNext": False},
    }
}


def jpeg_bytes(image):
    raw = BytesIO()
    image.save(raw, "PNG")
    return raw.getvalue()


def tiled_image():
    """A 6x6 image whose four 2x2 blocks are solid colours, plus an edge strip of grey."""
    image = Image.new("RGB", (6, 6), (128, 128, 128))
    for index, color in enumerate([(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0)]):
        x, y = index % 2 * 2, index // 2 * 2
        image.paste(Image.new("RGB", (2, 2), color), (x, y))
    return image


@pytest.fixture
def client(fake_session, fake_response):
    """A `LineManga` over a session answering the viewers, the listings and the images."""

    def build(extra=None):
        routes = {
            EPISODE_URL: fake_response(text=WEBTOON_HTML),
            PRINT_URL: fake_response(text=PRINT_HTML),
            INDIES_URL: fake_response(text=INDIES_HTML),
            f"{BASE_URL}/book/viewer?id=Z0090128": fake_response(text=WEBTOON_HTML.replace("Z0090127", "Z0090128")),
            LOCKED_URL: fake_response(text=NOT_FOUND_HTML, status_code=HTTPStatus.NOT_FOUND),
            f"{BASE_URL}/book/viewer?id=Z0090136": fake_response(text=NOT_FOUND_HTML, status_code=HTTPStatus.NOT_FOUND),
            f"{BASE_URL}/book/viewer?id=Z9999999": fake_response(text=NOT_FOUND_HTML, status_code=HTTPStatus.NOT_FOUND),
            f"{BASE_URL}/book/viewer?id=Z0000001": fake_response(text=NOT_FOUND_HTML, status_code=HTTPStatus.NOT_FOUND),
            f"{BASE_URL}/indies/book/article?id=999": fake_response(
                text=NOT_FOUND_HTML, status_code=HTTPStatus.NOT_FOUND
            ),
            # `/book/detail` redirects a serial's book to its work page, and 404s an unknown one.
            f"{BASE_URL}/book/detail?id=Z0090134": fake_response(text="<html></html>", url=SERIES_URL),
            f"{BASE_URL}/book/detail?id=Z0090136": fake_response(text="<html></html>", url=SERIES_URL),
            f"{BASE_URL}/book/detail?id=Z0000001": fake_response(text="<html></html>", url=SERIES_URL),
            f"{BASE_URL}/book/detail?id=Z9999999": fake_response(text=NOT_FOUND_HTML, status_code=HTTPStatus.NOT_FOUND),
            f"{PERIODIC_LIST_URL}Z0001684": fake_response(payload=LISTING),
            f"{PERIODIC_LIST_URL}Z9999999": fake_response(payload={}, status_code=HTTPStatus.NOT_FOUND),
            f"{INDIES_LIST_URL}20421": fake_response(payload=INDIES_LISTING),
            "/0hW4gfhTuu": fake_response(jpeg_bytes(tiled_image()), content_type="image/jpeg"),
            "/router/": fake_response(jpeg_bytes(Image.new("RGB", (8, 8), (1, 2, 3))), content_type="image/jpeg"),
        }
        session = fake_session({**(extra or {}), **routes})
        return LineManga(session), session

    return build


# --- URLs ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        EPISODE_URL,
        PRINT_URL,
        SERIES_URL,
        INDIES_URL,
        INDIES_SERIES_URL,
        "https://manga.line.me/book/viewer/?id=Z0090127",
    ],
)
def test_suitable_accepts_episode_and_series_urls(url):
    assert LineManga.suitable(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://manga.line.me/book/viewer?id=Z0090127",
        "https://manga.line.me/",
        "https://manga.line.me/book/viewer",
        "https://manga.line.me/book/viewer?id=",
        "https://manga.line.me/book/viewer?id=Z0090127&id=Z0090128",
        "https://manga.line.me/book/detail?id=B00167033069",
        "https://manga.line.me/product/detail?id=Z0090134",
        "https://manga.line.me/indies/",
        "https://manga.line.me/webtoons/article?id=1&no=1",
        "https://www.mangabox.me/reader/616489/episodes/217816/",
    ],
)
def test_suitable_rejects_other_urls(url):
    assert not LineManga.suitable(url)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (EPISODE_URL, False),
        (INDIES_URL, False),
        (SERIES_URL, True),
        (INDIES_SERIES_URL, True),
        ("https://manga.line.me/", False),
    ],
)
def test_is_series(url, expected):
    assert LineManga.is_series(url) is expected


# --- parsing ---------------------------------------------------------------------------


def test_parse_option_unescapes_a_backslashed_quote_and_ignores_a_page_without_one():
    assert parse_option(INDIES_HTML)["authorComment"] == "It's fine"
    assert "next_book" not in parse_option(INDIES_HTML)
    assert parse_option(NOT_FOUND_HTML) == {}


# --- descrambling ----------------------------------------------------------------------


def test_descramble_moves_the_blocks_and_keeps_the_edge_strip():
    served = tiled_image()
    # Slot n takes the served block named by blocks[n]: a full reversal here.
    fixed = descramble(served, 2, 2, ["3", "2", "1", "0"])
    assert fixed.size == (6, 6)
    assert fixed.getpixel((0, 0)) == (255, 255, 0)
    assert fixed.getpixel((2, 0)) == (0, 0, 255)
    assert fixed.getpixel((0, 2)) == (0, 255, 0)
    assert fixed.getpixel((2, 2)) == (255, 0, 0)
    assert fixed.getpixel((5, 5)) == (128, 128, 128)
    assert fixed.getpixel((4, 0)) == (128, 128, 128)
    # The served image is left alone.
    assert served.getpixel((0, 0)) == (255, 0, 0)


def test_descramble_reads_block_names_in_base_35():
    # 'z' is not a base-35 digit; 'y' is 34, so a 35-column page has slot 0 fed from block 34.
    image = Image.new("RGB", (35, 1), (0, 0, 0))
    image.putpixel((34, 0), (9, 9, 9))
    assert descramble(image, 35, 1, ["y"]).getpixel((0, 0)) == (9, 9, 9)


# --- episodes --------------------------------------------------------------------------


def test_episode_reads_a_webtoon(client):
    linemanga, session = client()
    episode = linemanga.episode(EPISODE_URL)

    assert episode.url == EPISODE_URL
    assert episode.series_title == "Let's レベルMAX"
    assert episode.episode_title == "プロローグ. すべての始まり"
    assert len(episode.pages) == 2
    assert episode.pages[0].url.endswith("FFeZ7qqIlvhe4o8usLAUlGac.jpg")
    assert episode.next_url == f"{BASE_URL}/book/viewer?id=Z0090128"
    assert episode.metadata["option"]["bookId"] == "Z0090127"
    assert json.dumps(episode.metadata)
    # Only the viewer page was fetched.
    assert session.calls == [EPISODE_URL]


def test_episode_reads_a_print_comic_with_its_blocks(client):
    linemanga, _ = client()
    episode = linemanga.episode(PRINT_URL)
    assert episode.series_title == "フルーツバスケット"
    assert episode.episode_title == "第1話 第1話(1)"
    assert episode.pages[0].extra["blocks"] == ["3", "2", "1", "0"]
    assert episode.next_url == f"{BASE_URL}/book/viewer?id=B00163114128"


def test_episode_reads_an_indies_work(client):
    linemanga, _ = client()
    episode = linemanga.episode(INDIES_URL)
    assert episode.url == INDIES_URL
    assert episode.series_title == "クレイト！"
    assert episode.episode_title == "クレイと仮想空間"
    assert len(episode.pages) == 1
    assert episode.next_url is None


def test_episode_canonicalises_the_url(client):
    linemanga, session = client()
    episode = linemanga.episode("https://manga.line.me/book/viewer/?id=Z0090127&from=list")
    assert episode.url == EPISODE_URL
    assert session.calls == [EPISODE_URL]


def test_locked_episode_has_no_pages_but_keeps_its_titles_and_the_next(client):
    linemanga, session = client()
    episode = linemanga.episode(LOCKED_URL)

    assert episode.pages == ()
    assert not episode.readable
    assert episode.series_title == "俺だけレベルMAXなビギナー"
    assert episode.episode_title == "国立中央博物館（２）"
    assert episode.next_url == f"{BASE_URL}/book/viewer?id=Z0090136"
    assert episode.metadata["book"]["selling_price"] == 67
    # The viewer said 404, `/book/detail` named the work, and its listing was read with a Referer.
    assert session.calls == [LOCKED_URL, f"{BASE_URL}/book/detail?id=Z0090134", f"{PERIODIC_LIST_URL}Z0001684"]
    assert session.headers_seen[-1]["Referer"] == LOCKED_URL
    assert session.headers_seen[-1]["X-Requested-With"] == "XMLHttpRequest"


def test_last_locked_episode_has_no_next(client):
    linemanga, _ = client()
    episode = linemanga.episode(f"{BASE_URL}/book/viewer?id=Z0090136")
    assert not episode.readable
    assert episode.next_url is None


def test_unknown_episode_is_not_an_episode_page(client):
    linemanga, _ = client()
    with pytest.raises(NotAnEpisodePageError, match="no episode Z9999999"):
        linemanga.episode(f"{BASE_URL}/book/viewer?id=Z9999999")


def test_book_missing_from_its_series_is_not_an_episode_page(client):
    linemanga, _ = client()
    with pytest.raises(NotAnEpisodePageError, match="no episode Z0000001 in the series Z0001684"):
        linemanga.episode(f"{BASE_URL}/book/viewer?id=Z0000001")


def test_unknown_indies_episode_is_not_an_episode_page(client):
    linemanga, session = client()
    with pytest.raises(NotAnEpisodePageError, match="no episode 999"):
        linemanga.episode(f"{BASE_URL}/indies/book/article?id=999")
    assert session.calls == [f"{BASE_URL}/indies/book/article?id=999"]


def test_page_without_a_viewer_is_not_an_episode_page(client, fake_response):
    linemanga, _ = client({f"{BASE_URL}/book/viewer?id=Z0000002": fake_response(text="<html><body>x</body></html>")})
    with pytest.raises(NotAnEpisodePageError, match="no viewer"):
        linemanga.episode(f"{BASE_URL}/book/viewer?id=Z0000002")


def test_episode_rejects_a_series_url(client):
    linemanga, _ = client()
    with pytest.raises(UnsupportedUrlError):
        linemanga.episode(SERIES_URL)


# --- series ----------------------------------------------------------------------------


def test_series_urls_lists_the_episodes_in_order_locked_ones_included(client):
    linemanga, _ = client()
    urls = linemanga.series_urls(SERIES_URL)
    assert urls == [
        EPISODE_URL,
        f"{BASE_URL}/book/viewer?id=Z0090128",
        LOCKED_URL,
        f"{BASE_URL}/book/viewer?id=Z0090136",
    ]
    assert all(LineManga.suitable(url) for url in urls)


def test_series_urls_reverses_an_indies_listing(client):
    linemanga, _ = client()
    assert linemanga.series_urls(INDIES_SERIES_URL) == [
        f"{BASE_URL}/indies/book/article?id=176141",
        f"{BASE_URL}/indies/book/article?id=183344",
        INDIES_URL,
    ]


def test_series_urls_drops_a_repeated_entry(fake_session, fake_response):
    twice = {"result": {"rows": [book("Z1", "a", 1), book("Z1", "a", 1), book("Z2", "b", 2)], "product": {}}}
    session = fake_session({f"{PERIODIC_LIST_URL}Z0000009": fake_response(payload=twice)})
    assert LineManga(session).series_urls(f"{BASE_URL}/product/periodic?id=Z0000009") == [
        f"{BASE_URL}/book/viewer?id=Z1",
        f"{BASE_URL}/book/viewer?id=Z2",
    ]


def test_series_urls_rejects_an_episode_url(client):
    linemanga, _ = client()
    with pytest.raises(UnsupportedUrlError):
        linemanga.series_urls(EPISODE_URL)


def test_series_urls_raises_on_an_empty_listing(fake_session, fake_response):
    empty = {"result": {"rows": [], "pager": {"hasNext": False}}}
    session = fake_session({f"{INDIES_LIST_URL}1": fake_response(payload=empty)})
    with pytest.raises(NotAnEpisodePageError, match="lists no episode"):
        LineManga(session).series_urls(f"{BASE_URL}/indies/product/detail?id=1")


def test_unknown_series_is_not_an_episode_page(client):
    linemanga, _ = client()
    with pytest.raises(NotAnEpisodePageError, match="no series Z9999999"):
        linemanga.series_urls(f"{BASE_URL}/product/periodic?id=Z9999999")


# --- images ----------------------------------------------------------------------------


def test_image_sends_the_episode_as_referer_and_leaves_a_webtoon_alone(client):
    linemanga, session = client()
    episode = linemanga.episode(EPISODE_URL)
    image = linemanga.image(episode.pages[0], episode)
    assert image.getpixel((0, 0)) == (1, 2, 3)
    assert session.calls[-1] == episode.pages[0].url
    assert session.headers_seen[-1]["Referer"] == EPISODE_URL


def test_image_descrambles_a_print_page(client):
    linemanga, _ = client()
    episode = linemanga.episode(PRINT_URL)
    image = linemanga.image(episode.pages[0], episode)
    assert image.getpixel((0, 0)) == (255, 255, 0)
    assert image.getpixel((2, 2)) == (255, 0, 0)
    assert image.getpixel((5, 5)) == (128, 128, 128)


# --- logging in ------------------------------------------------------------------------


# --- the real site ---------------------------------------------------------------------

# One free episode per known host: the prologue of a long-running serial.
TEST_URLS: dict[str, str] = {
    "manga.line.me": "https://manga.line.me/book/viewer?id=Z0090127",
}


@pytest.mark.network
@pytest.mark.geoblocked
@pytest.mark.parametrize("host", TEST_URLS)
def test_site_download(tmp_path, host):
    result = Downloader(LineManga(), tmp_path, only_first=True).download(TEST_URLS[host])
    assert result.status == "saved"
    assert (result.save_dir / "0.jpg").stat().st_size > 0


@pytest.mark.network
@pytest.mark.geoblocked
def test_site_print_comic_is_descrambled(tmp_path):
    # フルーツバスケット, a `portal_pages` serial: the first page is the shuffled title page.
    result = Downloader(LineManga(), tmp_path, only_first=True).download(PRINT_URL)
    assert result.status == "saved"
    assert result.episode.pages[0].extra["blocks"]
    assert Image.open(result.save_dir / "0.jpg").size == (651, 1050)


@pytest.mark.network
@pytest.mark.geoblocked
def test_site_indies_episode_downloads(tmp_path):
    result = Downloader(LineManga(), tmp_path, only_first=True).download(
        "https://manga.line.me/indies/book/article?id=176141"
    )
    assert result.status == "saved"
    assert result.episode.series_title == "クレイト！"


@pytest.mark.network
@pytest.mark.geoblocked
def test_site_locked_episode_has_no_pages():
    episode = LineManga().episode(LOCKED_URL)
    assert not episode.readable
    assert episode.series_title == "俺だけレベルMAXなビギナー"
    assert episode.next_url == "https://manga.line.me/book/viewer?id=Z0090136"


@pytest.mark.network
@pytest.mark.geoblocked
def test_site_series_lists_episodes():
    urls = LineManga().series_urls(SERIES_URL)
    assert urls[0] == "https://manga.line.me/book/viewer?id=Z0090127"
    assert all(LineManga.suitable(url) for url in urls)
