from __future__ import annotations

import json
from http import HTTPStatus
from io import BytesIO

import pytest
from PIL import Image
from requests import HTTPError

from getjmanga.downloader import Downloader
from getjmanga.errors import GetjmangaError, NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractors.yanmaga import (
    YanMaga,
    parse_listing,
)
from getjmanga.viewers.speedbinb import viewer_key

TITLE = "妹は知っている"
TITLE_ENC = "%E5%A6%B9%E3%81%AF%E7%9F%A5%E3%81%A3%E3%81%A6%E3%81%84%E3%82%8B"
WORK_URL = f"https://yanmaga.jp/comics/{TITLE_ENC}"
EPISODE_ID = "8b0f8594559d188a6403d112db282f31"
NEXT_ID = "589ba3209053de03f68d96c68cead1a6"
LOCKED_ID = "1883b7686c9b6cebd6b5923b999afba2"
LAST_ID = "7403d97c3657ac08ecf118ca28e82158"
CID = "06A0000000000870304X"
EPISODE_URL = f"{WORK_URL}/{EPISODE_ID}"
NEXT_URL = f"{WORK_URL}/{NEXT_ID}"
LOCKED_URL = f"{WORK_URL}/{LOCKED_ID}"
LAST_URL = f"{WORK_URL}/{LAST_ID}"
READER_URL = f"https://yanmaga.jp/viewer/comics/{TITLE_ENC}/{EPISODE_ID}?cid={CID}"
INFO_PATH = f"/viewer/bibGetCntntInfo?random_identification={EPISODE_ID}&type=comics"
SERVER = f"https://sbc.yanmaga.jp/books/{CID}/20241121140144/2"

# A 2x2 grid, two pixels of padding, with the narrow column and the short row last
# on both sides: `BB` (short row per column) + `BB` (narrow column per row) + order.
IDENTITY_CTBL = "=2-2+2-BBBBABCD"
SWAPPED_PTBL = "=2-2-2-BBBBBADC"

COLOURS = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0)]

# The episodes the fake work lists, oldest first: the free first two, a login-only
# one, and the paid latest one.
LISTING = (
    (EPISODE_ID, "第１話　三木貴一郎という男", False),
    (NEXT_ID, "第２話　観察する男", False),
    (LOCKED_ID, "第８７話　助言する男", True),
    (LAST_ID, "第８９話　本気の男", True),
)


def reader_html(*, viewer=True, title="妹は知っている - 第１話　三木貴一郎という男 | ヤンマガWeb"):
    """The reader page a free episode redirects to: SpeedBinb mounted on `#content`."""
    content = (
        f'<div id="content" class="pages" data-ptbinb="{INFO_PATH.replace("&", "&amp;")}"></div>' if viewer else ""
    )
    return f"""<!DOCTYPE html><html lang="ja"><head><title>{title}</title>
<meta name="csrf-token" content="x" /></head><body>
<div id="content_base" class="content_base_comics">{content}</div>
<script>DomainMember='https://id-members.kodansha.co.jp';Config.OriginsUseCredentials=['https://sbc.yanmaga.jp'];</script>
</body></html>"""


def locked_html(episode_title="第８７話　助言する男", *, lead=False, page_title=True):
    """The episode page a locked episode answers with: a rental section, no reader."""
    eptitle = (
        '<div class="mod-episode-rental-section-eptitle-registration-lead">\n'
        f"無料登録で今すぐ<br>「<span>{episode_title}</span>」が<br>無料で読める!!\n</div>"
        if lead
        else f"\n{episode_title}\n"
    )
    title = (
        f"<title>妹は知っている - {episode_title} | ヤンマガWeb</title>" if page_title else "<title>ヤンマガWeb</title>"
    )
    return f"""<!DOCTYPE html><html lang="ja"><head>{title}</head><body>
<main><div class="container-fluid episode-container">
<div class="mod-episode-rental-section"><div class="container mod-episode-rental-section-inner" id="not-rental">
<div class="mod-episode-rental-section-body">
<h1 class="mod-episode-rental-section-title">妹は知っている</h1>
<div class="mod-episode-rental-section-eptitle">{eptitle}</div>
<div class="mod-episode-rental-section-button">
<a class="mod-button--yellow ga-rental-modal-sign-up" href="/customers/sign-up"><span>無料登録して読む</span></a>
</div>
</div></div></div>
<ul class="mod-episode-list">
<li class="mod-episode-item" data-original-url="/comics/{TITLE_ENC}/{EPISODE_ID}"></li>
</ul>
<button class="mod-episode-more-button" data-limit="80" data-offset="5"
 data-path="/comics/{TITLE_ENC}/episodes" data-sort="older"><span>もっと見る</span></button>
</div></main></body></html>"""


WORK_HTML = f"""<!DOCTYPE html><html lang="ja"><head>
<title>『妹は知っている』 【無料公開中】 | ヤンマガWeb</title></head>
<body><div class="detailv2-episodes">
<a class="ga-episode-link" href="/comics/{TITLE_ENC}/{EPISODE_ID}"><span>１話から無料で読む</span></a>
</div></body></html>"""


def listing_js(entries=LISTING, *, title_enc=TITLE_ENC):
    """What `/comics/<title>/episodes` answers: JavaScript inserting one `<li>` per episode."""
    lines = [
        'var target = document.querySelector(".mod-episode-list--close");',
        'target.classList.remove("mod-episode-list--close");',
    ]
    for episode_id, episode_title, locked in entries:
        classes = "mod-episode-item js-modal" if locked else "mod-episode-item"
        url = f"/comics/{title_enc}/{episode_id}"
        li = (
            f'<li class=\\"{classes}\\" data-episode-title=\\"{episode_title}\\" data-is-free=\\"true\\" '
            f'data-modal=\\"registration\\" data-original-url=\\"{url}\\">\\n'
            f'<div class=\\"mod-episode-public\\">\\n<a class=\\"mod-episode-link    \\" href=\\"{url}\\">'
            f'<p class=\\"mod-episode-title\\">{episode_title}<\\/p><\\/a><\\/div>\\n<\\/li>\\n'
        )
        lines.append(f"    target.insertAdjacentHTML('beforeend', \"{li}\")")
    return "\n".join(lines)


def content_json(srcs=("pages/a.jpg", "pages/b.jpg")):
    """What `<server>/content` answers on the Rest backend: the page list as plain JSON."""
    imgs = "".join(
        f'<t-img src="{src}" a="0" height="100%" shrink="screen" orgwidth="392" orgheight="392" preview="false" '
        f'id="P{index:04d}"><t-pb>'
        for index, src in enumerate(srcs)
    )
    ttx = (
        "<html><head><title>妹は知っている</title></head><body>"
        f'<t-case screen.portrait="screen.portrait">{imgs}</t-case></body></html>'
    )
    return json.dumps({"SBCVersion": "01.6930", "result": 1, "ttx": ttx})


def content_js(srcs=("pages/a.jpg", "pages/b.jpg")):
    """What `content.js` answers on the static backend: the same, as JSONP."""
    return "DataGet_Content(" + content_json(srcs) + ")"


def encode_table(content_id, key, value):
    """The inverse of `gaugau.decode_table`, so a fake API can hand tables out."""
    seed = 0
    for index, char in enumerate(f"{content_id}:{key}"):
        seed += ord(char) << (index % 16)
    state = (seed & 0x7FFFFFFF) or 0x12345678
    out = []
    for char in json.dumps(value):
        state = ((state >> 1) ^ (0x48200004 if state & 1 else 0)) & 0xFFFFFFFF
        out.append(chr((ord(char) - 32 - state) % 94 + 32))
    return "".join(out)


class InfoResponse:
    """A `bibGetCntntInfo` answer that encrypts its tables with whatever `k` was sent."""

    def __init__(self, session, *, ctbl=(IDENTITY_CTBL,), ptbl=(SWAPPED_PTBL,), server_type=2, body=None, **item):
        self.session = session
        self.ctbl = list(ctbl)
        self.ptbl = list(ptbl)
        self.server_type = server_type
        self.body = body
        self.item = item
        self.url = None
        self.status_code = HTTPStatus.OK
        self.ok = True

    def raise_for_status(self):
        pass

    def json(self):
        if self.body is not None:
            return self.body
        key = self.session.params_seen[-1]["k"]
        item = {
            "ContentID": CID,
            "ContentsServer": SERVER,
            "ServerType": self.server_type,
            "Title": "第１話　三木貴一郎という男",
            "ParentTitle": "妹は知っている",
            "ParentPath": f"/comics/{TITLE_ENC}",
            "ViewMode": 1,
            "PrevEpisode": {},
            "NextEpisode": {
                "Jdcn": "06A0000000000870305Y",
                "ViewerPath": f"/comics/{TITLE_ENC}/{NEXT_ID}",
                "Title": "第２話　観察する男",
                "ParentTitle": "妹は知っている",
            },
            "stbl": encode_table(CID, key, [1, 2]),
            "ttbl": encode_table(CID, key, [3, 4]),
            "ctbl": encode_table(CID, key, self.ctbl),
            "ptbl": encode_table(CID, key, self.ptbl),
            **self.item,
        }
        return {"result": 1, "rurl": f"/comics/{TITLE_ENC}?reading={CID}", "items": [item]}


#: What the API says about an episode the reader cannot open.
REFUSED = {
    "result": 0,
    "eurl": f"/customers/sign-up?forward={LOCKED_ID}&viewer_error_code=403002",
    "rurl": "",
    "items": [],
}


@pytest.fixture
def client(fake_session, fake_response):
    """A YanMaga on a fake site: the episode page (redirecting to the reader), the API, the content, the listing."""

    def build(routes=None, **info):
        session = fake_session({})
        # Routes match by substring, first wins: the API and the content go before the pages.
        session.routes = {
            "bibGetCntntInfo": InfoResponse(session, **info),
            f"{SERVER}/content": fake_response(text=content_json()),
            f"/{EPISODE_ID}": fake_response(text=reader_html(), url=READER_URL),
            f"/{LOCKED_ID}": fake_response(text=locked_html()),
            f"/{LAST_ID}": fake_response(text=locked_html("第８９話　本気の男")),
            "/episodes": fake_response(text=listing_js()),
            **(routes or {}),
        }
        return YanMaga(session), session

    return build


def served_image(order):
    """A 400x400 served image of a 2x2 grid, tile n painted COLOURS[order[n]] inside its padding."""
    image = Image.new("RGB", (400, 400), (0, 0, 0))
    for index, colour_index in enumerate(order):
        column, row = index % 2, index // 2
        image.paste(Image.new("RGB", (196, 196), COLOURS[colour_index]), (2 + column * 200, 2 + row * 200))
    return image


# --- URLs ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        EPISODE_URL,
        f"{EPISODE_URL}/",
        f"https://yanmaga.jp/comics/{TITLE}/{EPISODE_ID}",
        READER_URL,
        f"https://yanmaga.jp/viewer/comics/{TITLE}/{EPISODE_ID}",
        WORK_URL,
        f"{WORK_URL}/",
        f"{WORK_URL}?sort=older",
        f"https://yanmaga.jp/comics/{TITLE}",
        "https://yanmaga.jp/comics/GLITCH_WITCH",
        "https://yanmaga.jp/comics/100%E5%B9%B4%E7%95%99%E5%B9%B4%E3%81%97%E3%81%A6%E3%82%8B%E3%82%A8%E3%83%AB%E3%83%95",
    ],
)
def test_suitable_accepts_episode_reader_and_work_urls(url):
    assert YanMaga.suitable(url)


@pytest.mark.parametrize(
    "url",
    [
        EPISODE_URL.replace("https://", "http://"),
        f"https://www.yanmaga.jp/comics/{TITLE_ENC}/{EPISODE_ID}",
        f"https://magapoke.jp/comics/{TITLE_ENC}/{EPISODE_ID}",
        "https://yanmaga.jp/",
        "https://yanmaga.jp/comics",
        "https://yanmaga.jp/comics/",
        "https://yanmaga.jp/comics/series",
        "https://yanmaga.jp/comics/authors",
        "https://yanmaga.jp/comics/authors/d8e709b0cd1bf30b8673b6a673e4d8e4",
        f"https://yanmaga.jp/comics/{TITLE_ENC}/episodes",
        f"https://yanmaga.jp/comics/{TITLE_ENC}/{EPISODE_ID}/extra",
        f"https://yanmaga.jp/gravures/{TITLE_ENC}/{EPISODE_ID}",
        "https://yanmaga.jp/viewer/bibGetCntntInfo",
    ],
)
def test_suitable_rejects_other_urls(url):
    assert not YanMaga.suitable(url)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (WORK_URL, True),
        (f"{WORK_URL}?sort=newer", True),
        (f"https://yanmaga.jp/comics/{TITLE}/", True),
        (EPISODE_URL, False),
        (READER_URL, False),
        ("https://yanmaga.jp/comics/series", False),
        ("https://example.com/comics/x", False),
    ],
)
def test_is_series(url, expected):
    assert YanMaga.is_series(url) is expected


# --- the listing --------------------------------------------------------------------------


def test_parse_listing_deduplicates_and_canonicalises_the_urls():
    text = listing_js(((EPISODE_ID, "a", False), (EPISODE_ID, "a again", False)), title_enc=TITLE)
    assert [entry.url for entry in parse_listing(text)] == [EPISODE_URL]


def test_parse_listing_undoes_the_javascript_escapes():
    # Rails HTML-escapes the attribute (`&quot;`), then JavaScript-escapes the string (`\'`, `\$`).
    text = listing_js(((EPISODE_ID, "It\\'s a &quot;title&quot; for \\$5", False),))
    assert parse_listing(text)[0].title == 'It\'s a "title" for $5'


# --- reading an episode -----------------------------------------------------------------------


def test_episode_reads_the_titles_the_pages_and_the_next_episode(client):
    yanmaga, _ = client()
    episode = yanmaga.episode(EPISODE_URL)

    assert episode.url == EPISODE_URL
    assert episode.series_title == "妹は知っている"
    assert episode.episode_title == "第１話　三木貴一郎という男"
    assert [page.url for page in episode.pages] == [f"{SERVER}/img/pages/a.jpg", f"{SERVER}/img/pages/b.jpg"]
    assert episode.pages[0].width == 392
    assert episode.pages[0].extra == {"ctbl": IDENTITY_CTBL, "ptbl": SWAPPED_PTBL}
    assert episode.next_url == NEXT_URL
    assert episode.metadata["episode_id"] == EPISODE_ID
    assert episode.metadata["content_id"] == CID
    assert episode.metadata["contents_server"] == SERVER
    assert episode.metadata["locked"] is False
    assert episode.metadata["info"]["Title"] == "第１話　三木貴一郎という男"
    assert "ctbl" not in episode.metadata["info"]
    json.dumps(episode.metadata)


def test_episode_calls_the_api_and_the_content_server_the_way_the_reader_does(client):
    yanmaga, session = client()
    yanmaga.episode(EPISODE_URL)

    assert session.calls[0] == EPISODE_URL
    info_call = next(index for index, url in enumerate(session.calls) if "bibGetCntntInfo" in url)
    assert session.calls[info_call] == f"https://yanmaga.jp{INFO_PATH}"
    params = session.params_seen[info_call]
    assert params["cid"] == CID
    assert params["k"] == viewer_key(CID, params["k"][::2])
    assert isinstance(params["dmytime"], int)
    assert session.headers_seen[info_call]["Referer"] == READER_URL

    content_call = next(index for index, url in enumerate(session.calls) if url == f"{SERVER}/content")
    assert session.params_seen[content_call] is None
    assert session.headers_seen[content_call]["Referer"] == READER_URL
    assert not any("/episodes" in url for url in session.calls)


@pytest.mark.parametrize(
    "url",
    [
        f"https://yanmaga.jp/comics/{TITLE}/{EPISODE_ID}",
        f"{EPISODE_URL}/",
        READER_URL,
        f"https://yanmaga.jp/viewer/comics/{TITLE}/{EPISODE_ID}?cid={CID}",
    ],
)
def test_episode_takes_the_decoded_and_the_reader_forms(client, url):
    yanmaga, session = client()
    episode = yanmaga.episode(url)
    assert episode.url == EPISODE_URL
    assert session.calls[0] == EPISODE_URL
    assert len(episode.pages) == 2


def test_episode_on_the_static_backend_reads_content_js(client, fake_response):
    yanmaga, _ = client({f"{SERVER}/content.js": fake_response(text=content_js())}, server_type=1)
    episode = yanmaga.episode(EPISODE_URL)
    assert [page.url for page in episode.pages] == [f"{SERVER}/pages/a.jpg/M_H.jpg", f"{SERVER}/pages/b.jpg/M_H.jpg"]


def test_last_episode_has_no_next(client):
    yanmaga, _ = client(NextEpisode={})
    assert yanmaga.episode(EPISODE_URL).next_url is None


def test_episode_titles_fall_back_to_the_page_title(client):
    yanmaga, _ = client(Title="", ParentTitle="")
    episode = yanmaga.episode(EPISODE_URL)
    assert episode.series_title == "妹は知っている"
    assert episode.episode_title == "第１話　三木貴一郎という男"


def test_episode_refuses_a_work_url(client):
    yanmaga, _ = client()
    with pytest.raises(UnsupportedUrlError):
        yanmaga.episode(WORK_URL)


# --- locked and missing episodes ---------------------------------------------------------


def test_a_locked_episode_has_no_pages_but_still_a_next(client):
    yanmaga, session = client()
    episode = yanmaga.episode(LOCKED_URL)

    assert episode.url == LOCKED_URL
    assert episode.pages == ()
    assert episode.series_title == "妹は知っている"
    assert episode.episode_title == "第８７話 助言する男"
    assert episode.next_url == LAST_URL
    assert episode.metadata == {"episode_id": LOCKED_ID, "content_id": None, "locked": True}
    assert not any("bibGetCntntInfo" in url for url in session.calls)
    listing_call = next(index for index, url in enumerate(session.calls) if url.endswith("/episodes"))
    assert session.calls[listing_call] == f"{WORK_URL}/episodes"
    assert session.params_seen[listing_call] == {"offset": 0, "limit": 10000, "sort": "older"}


def test_the_last_locked_episode_has_no_next(client):
    yanmaga, _ = client()
    episode = yanmaga.episode(f"https://yanmaga.jp/comics/{TITLE}/{LAST_ID}")
    assert episode.pages == ()
    assert episode.episode_title == "第８９話 本気の男"
    assert episode.next_url is None


def test_a_locked_episode_the_listing_does_not_know_is_titled_off_the_page(client, fake_response):
    yanmaga, _ = client({"/episodes": fake_response(text=listing_js(LISTING[:1]))})
    episode = yanmaga.episode(LOCKED_URL)
    assert episode.episode_title == "第８７話　助言する男"
    assert episode.next_url is None


@pytest.mark.parametrize("lead", [False, True])
def test_a_locked_episode_without_a_page_title_is_titled_off_the_rental_section(client, fake_response, lead):
    yanmaga, _ = client(
        {
            f"/{LOCKED_ID}": fake_response(text=locked_html("第８７話　助言する男", lead=lead, page_title=False)),
            "/episodes": fake_response(text=listing_js(LISTING[:1])),
        },
    )
    episode = yanmaga.episode(LOCKED_URL)
    assert episode.series_title == "妹は知っている"
    assert episode.episode_title == "第８７話 助言する男"


def test_a_reader_page_the_api_refuses_is_locked(client, fake_response):
    yanmaga, _ = client(
        {
            f"/{LOCKED_ID}": fake_response(
                text=reader_html(title="妹は知っている - 第８７話　助言する男 | ヤンマガWeb"),
                url=f"https://yanmaga.jp/viewer/comics/{TITLE_ENC}/{LOCKED_ID}?cid=06A0000000000969392C",
            )
        },
        body=REFUSED,
    )
    episode = yanmaga.episode(LOCKED_URL)
    assert episode.pages == ()
    assert episode.episode_title == "第８７話 助言する男"
    assert episode.next_url == LAST_URL
    assert episode.metadata == {"episode_id": LOCKED_ID, "content_id": "06A0000000000969392C", "locked": True}


def test_an_unknown_episode_is_sent_back_to_the_work_page(client, fake_response):
    yanmaga, _ = client({"/000000000000000000000000deadbeef": fake_response(text=WORK_HTML, url=WORK_URL)})
    with pytest.raises(NotAnEpisodePageError, match="sent the browser"):
        yanmaga.episode(f"{WORK_URL}/000000000000000000000000deadbeef")


def test_a_404_is_not_an_episode(client, fake_response):
    yanmaga, _ = client({f"/{EPISODE_ID}": fake_response(text="", status_code=HTTPStatus.NOT_FOUND)})
    with pytest.raises(NotAnEpisodePageError, match="404"):
        yanmaga.episode(EPISODE_URL)


def test_other_http_errors_propagate(client, fake_response):
    yanmaga, _ = client({f"/{EPISODE_ID}": fake_response(text="", status_code=HTTPStatus.SERVICE_UNAVAILABLE)})
    with pytest.raises(HTTPError):
        yanmaga.episode(EPISODE_URL)


def test_a_page_with_neither_a_reader_nor_a_title_is_not_an_episode(client, fake_response):
    yanmaga, _ = client({f"/{EPISODE_ID}": fake_response(text="<html><body>nope</body></html>")})
    with pytest.raises(NotAnEpisodePageError):
        yanmaga.episode(EPISODE_URL)


def test_api_on_another_backend_is_unsupported(client):
    yanmaga, _ = client(server_type=0)
    with pytest.raises(GetjmangaError, match="ServerType"):
        yanmaga.episode(EPISODE_URL)


# --- listing a work -----------------------------------------------------------------------


@pytest.mark.parametrize("url", [f"{WORK_URL}?sort=newer", f"https://yanmaga.jp/comics/{TITLE}/"])
def test_series_urls_lists_the_episodes_oldest_first(client, url):
    yanmaga, session = client()
    urls = yanmaga.series_urls(url)
    assert urls == [EPISODE_URL, NEXT_URL, LOCKED_URL, LAST_URL]
    assert all(YanMaga.suitable(url) for url in urls)
    assert session.calls == [f"{WORK_URL}/episodes"]
    assert session.headers_seen[0]["Referer"] == WORK_URL


def test_series_urls_raises_on_an_empty_listing(client, fake_response):
    yanmaga, _ = client({"/episodes": fake_response(text=listing_js(()))})
    with pytest.raises(NotAnEpisodePageError):
        yanmaga.series_urls(WORK_URL)


def test_series_urls_raises_on_a_missing_work(client, fake_response):
    yanmaga, _ = client({"/episodes": fake_response(text="", status_code=HTTPStatus.NOT_FOUND)})
    with pytest.raises(NotAnEpisodePageError, match="404"):
        yanmaga.series_urls("https://yanmaga.jp/comics/nonexistent")


def test_series_urls_refuses_an_episode_url(client):
    yanmaga, _ = client()
    with pytest.raises(UnsupportedUrlError):
        yanmaga.series_urls(EPISODE_URL)


# --- downloading --------------------------------------------------------------------------


def test_image_puts_the_tiles_back(client, fake_response):
    raw = BytesIO()
    served_image([1, 0, 3, 2]).save(raw, "PNG")
    yanmaga, session = client({f"{SERVER}/img/": fake_response(raw.getvalue(), content_type="image/png")})
    episode = yanmaga.episode(EPISODE_URL)

    page = yanmaga.image(episode.pages[0], episode)

    assert page.size == (392, 392)
    assert [page.getpixel((column * 196 + 5, row * 196 + 5)) for row in range(2) for column in range(2)] == COLOURS
    assert session.calls[-1] == f"{SERVER}/img/pages/a.jpg"
    assert session.headers_seen[-1]["Referer"] == EPISODE_URL


# --- the real site --------------------------------------------------------------------

# One episode per known host, free to read without an account: the first episode of a long-running series.
TEST_URLS: dict[str, str] = {
    "yanmaga.jp": EPISODE_URL,
}


@pytest.mark.network
@pytest.mark.parametrize("host", TEST_URLS)
def test_site_download(tmp_path, host):
    result = Downloader(YanMaga(), tmp_path, only_first=True).download(TEST_URLS[host])
    assert result.status == "saved"
    assert (result.save_dir / "0.jpg").stat().st_size > 0
    # A page the tables did not put back together keeps its padding.
    assert Image.open(result.save_dir / "0.jpg").size == (result.episode.pages[0].width, result.episode.pages[0].height)


@pytest.mark.network
def test_work_page_lists_episodes_oldest_first():
    urls = YanMaga().series_urls(f"https://yanmaga.jp/comics/{TITLE}")
    assert urls[0] == EPISODE_URL
    assert len(urls) > 10
    assert all(YanMaga.suitable(url) for url in urls)


@pytest.mark.network
def test_an_unknown_episode_is_not_an_episode_on_the_site():
    with pytest.raises(NotAnEpisodePageError):
        YanMaga().episode(f"{WORK_URL}/000000000000000000000000deadbeef")
