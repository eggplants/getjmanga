from __future__ import annotations

import json
from http import HTTPStatus
from io import BytesIO

import pytest
from PIL import Image

from getjmanga.downloader import Downloader
from getjmanga.errors import GetjmangaError, NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractors.gaugau import Gaugau
from getjmanga.viewers.speedbinb import viewer_key

WORK_ID = "g5JBaom0KiedHfUAbnZwXURtl"
WORK_URL = f"https://gaugau.futabanet.jp/list/work/{WORK_ID}"
EPISODE_URL = f"{WORK_URL}/episodes/1"
CONTENT_ID = f"{WORK_ID}_001-1"
SERVER = f"https://gaugau.futabanet.jp/trial_data/{CONTENT_ID}/non_member_trial"

# A 2x2 grid, two pixels of padding, with the narrow column and the short row last
# on both sides: `BB` (short row per column) + `BB` (narrow column per row) + order.
IDENTITY_CTBL = "=2-2+2-BBBBABCD"
SWAPPED_PTBL = "=2-2-2-BBBBBADC"

COLOURS = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0)]


def encode_table(content_id, key, value):
    """The inverse of `decode_table`, so a fake API can hand tables out."""
    seed = 0
    for index, char in enumerate(f"{content_id}:{key}"):
        seed += ord(char) << (index % 16)
    state = (seed & 0x7FFFFFFF) or 0x12345678
    out = []
    for char in json.dumps(value):
        state = ((state >> 1) ^ (0x48200004 if state & 1 else 0)) & 0xFFFFFFFF
        out.append(chr((ord(char) - 32 - state) % 94 + 32))
    return "".join(out)


def episode_html(*, viewer=True, title="第1話(1)　", series=True):
    crumb = (
        f'<ol class="breadcrumb"><li><a itemprop="item" href="{WORK_URL}">'
        '<span itemprop="name">宝石の聖女</span></a></li></ol>'
        if series
        else ""
    )
    content = (
        f'<div id="content" class="pages" data-ptbinb="/sws/bibGetCntntInfo?hash={WORK_ID}&display_order=1" '
        f'data-ptbinb-cid="{CONTENT_ID}"></div>'
        if viewer
        else ""
    )
    heading = f'<h1 class="detailHead__title">{title}</h1>' if title is not None else ""
    return f"""<html><head>
<title>公式-宝石の聖女 第1話(1) | 無料・試し読み豊富、Web漫画・コミックサイト がうがうモンスター＋</title>
</head><body>{content}{heading}{crumb}</body></html>"""


def listing_html(orders=(3, 2, 1), work_id=WORK_ID):
    """The episode list as the site writes it: newest first, one link per episode."""
    links = "".join(
        f'<div class="episode__grid"><a href="https://gaugau.futabanet.jp/list/work/{work_id}/episodes/{order}">'
        f'<div class="episode__num">第{order}話</div></a></div>'
        for order in orders
    )
    return f"<html><body>{links}<a href='{WORK_URL}/comics'>x</a></body></html>"


def comics_html():
    return (
        f"<html><body><a class='button__link -reader' href='{WORK_URL}/reader/comics/{CONTENT_ID}'>試し読み</a>"
        f"<a href='{WORK_URL}/reader/comics/{CONTENT_ID}'>again</a>"
        f"<a href='{WORK_URL}/reader/novels/{WORK_ID}_001s-sample_1'>novel</a></body></html>"
    )


def content_js(srcs=("pages/a.jpg", "pages/b.jpg"), image_class="default"):
    imgs = "".join(
        f'<t-img src="{src}" a="0" orgwidth="392" orgheight="392" id="P{index:04d}"><t-pb>'
        for index, src in enumerate(srcs)
    )
    spreads = "".join(
        f'<t-img src="{src}" orgwidth="392" orgheight="392" id="L{index:04d}">' for index, src in enumerate(srcs)
    )
    ttx = f'<html><body><t-case screen.portrait="screen.portrait">{imgs}</t-case><t-nocase>{spreads}</t-nocase></body>'
    return (
        "DataGet_Content(" + json.dumps({"result": 1, "ttx": ttx, "ImageClass": image_class, "ContentDate": "1"}) + ")"
    )


class InfoResponse:
    """A `bibGetCntntInfo` answer that encrypts its tables with whatever `k` was sent."""

    def __init__(self, session, *, ctbl=(IDENTITY_CTBL,), ptbl=(SWAPPED_PTBL,), server_type=1, item=None):
        self.session = session
        self.ctbl = list(ctbl)
        self.ptbl = list(ptbl)
        self.server_type = server_type
        self.item = item
        self.url = None
        self.status_code = HTTPStatus.OK
        self.is_success = True

    def raise_for_status(self):
        pass

    def json(self):
        key = self.session.params_seen[-1]["k"]
        item = {
            "ContentID": CONTENT_ID,
            "ContentsServer": SERVER + "/",
            "ServerType": self.server_type,
            "Title": "公式-宝石の聖女 第1話(1)",
            "ViewMode": 2,
            "ctbl": encode_table(CONTENT_ID, key, self.ctbl),
            "ptbl": encode_table(CONTENT_ID, key, self.ptbl),
        }
        if self.item is not None:
            item = self.item
        return {"result": 1, "items": [item]}


@pytest.fixture
def client(fake_session, fake_response):
    """A Gaugau on a fake site: the episode page, the API, content.js and the listing."""

    def build(routes=None, **info):
        session = fake_session({})
        session.routes = {
            "bibGetCntntInfo": InfoResponse(session, **info),
            "content.js": fake_response(text=content_js()),
            f"/list/work/{WORK_ID}/episodes/": fake_response(text=episode_html()),
            f"/list/work/{WORK_ID}/episodes": fake_response(text=listing_html()),
            f"/list/work/{WORK_ID}/comics": fake_response(text=comics_html()),
            **(routes or {}),
        }
        return Gaugau(session), session

    return build


def served_image(order):
    """A 400x400 served image of a 2x2 grid, tile n painted COLOURS[order[n]] inside its padding."""
    image = Image.new("RGB", (400, 400), (0, 0, 0))
    for index, colour_index in enumerate(order):
        column, row = index % 2, index // 2
        image.paste(Image.new("RGB", (196, 196), COLOURS[colour_index]), (2 + column * 200, 2 + row * 200))
    return image


# --- urls -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (EPISODE_URL, True),
        (f"{WORK_URL}/episodes/173/", True),
        (f"{WORK_URL}/reader/comics/{CONTENT_ID}", True),
        (WORK_URL, True),
        (f"{WORK_URL}/episodes", True),
        (f"{WORK_URL}/comics", True),
        (f"http://gaugau.futabanet.jp/list/work/{WORK_ID}/episodes/1", False),
        ("https://futabanet.jp/list/work/x/episodes/1", False),
        (f"{WORK_URL}/reader/novels/{WORK_ID}_001s-sample_1", False),
        (f"{WORK_URL}/novels", False),
        ("https://gaugau.futabanet.jp/list/works", False),
        ("https://gaugau.futabanet.jp/", False),
    ],
)
def test_suitable(url, expected):
    assert Gaugau.suitable(url) is expected


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (WORK_URL, True),
        (f"{WORK_URL}/", True),
        (f"{WORK_URL}/episodes", True),
        (f"{WORK_URL}/comics", True),
        (EPISODE_URL, False),
        (f"{WORK_URL}/reader/comics/{CONTENT_ID}", False),
        ("https://example.com/list/work/x", False),
    ],
)
def test_is_series(url, expected):
    assert Gaugau.is_series(url) is expected


# --- reading an episode -----------------------------------------------------------------------


def test_episode_reads_the_titles_the_pages_and_the_next_episode(client):
    gaugau, _ = client()
    episode = gaugau.episode(EPISODE_URL)

    assert episode.series_title == "宝石の聖女"
    assert episode.episode_title == "第1話(1)"
    assert [page.url for page in episode.pages] == [f"{SERVER}/pages/a.jpg/M_H.jpg", f"{SERVER}/pages/b.jpg/M_H.jpg"]
    assert episode.pages[0].width == 392
    assert episode.pages[0].extra == {"ctbl": IDENTITY_CTBL, "ptbl": SWAPPED_PTBL}
    assert episode.next_url == f"{WORK_URL}/episodes/2"
    assert episode.metadata["content_id"] == CONTENT_ID
    assert episode.metadata["contents_server"] == SERVER
    json.dumps(episode.metadata)


def test_episode_calls_the_api_the_way_the_viewer_does(client):
    gaugau, session = client()
    gaugau.episode(EPISODE_URL)

    info_call = next(index for index, url in enumerate(session.calls) if "bibGetCntntInfo" in url)
    assert session.calls[info_call].startswith(
        f"https://gaugau.futabanet.jp/sws/bibGetCntntInfo?hash={WORK_ID}&display_order=1"
    )
    params = session.params_seen[info_call]
    assert params["cid"] == CONTENT_ID
    assert params["k"] == viewer_key(CONTENT_ID, params["k"][::2])
    assert isinstance(params["dmytime"], int)
    assert session.headers_seen[info_call]["Referer"] == EPISODE_URL

    content_call = next(index for index, url in enumerate(session.calls) if url.endswith("content.js"))
    assert session.calls[content_call] == f"{SERVER}/content.js"
    assert "dmytime" in session.params_seen[content_call]


def test_episode_at_the_end_of_the_list_has_no_next(client, fake_response):
    gaugau, _ = client({f"/list/work/{WORK_ID}/episodes/": fake_response(text=episode_html())})
    assert gaugau.episode(f"{WORK_URL}/episodes/3").next_url is None


def test_a_locked_episode_has_no_pages_but_still_a_next(client, fake_response):
    gaugau, session = client(
        {f"/list/work/{WORK_ID}/episodes/": fake_response(text=episode_html(viewer=False, title="第2話(3)"))}
    )
    episode = gaugau.episode(f"{WORK_URL}/episodes/2")

    assert episode.pages == ()
    assert episode.episode_title == "第2話(3)"
    assert episode.series_title == "宝石の聖女"
    assert episode.next_url == f"{WORK_URL}/episodes/3"
    assert episode.metadata == {"work_id": WORK_ID, "locked": True}
    assert not any("bibGetCntntInfo" in url for url in session.calls)


def test_a_page_without_a_viewer_or_a_heading_is_not_an_episode(client, fake_response):
    gaugau, _ = client({f"/list/work/{WORK_ID}/episodes/": fake_response(text="<html><body>nope</body></html>")})
    with pytest.raises(NotAnEpisodePageError):
        gaugau.episode(EPISODE_URL)


def test_a_404_is_not_an_episode(client, fake_response):
    gaugau, _ = client({f"/list/work/{WORK_ID}/episodes/": fake_response(text="", status_code=HTTPStatus.NOT_FOUND)})
    with pytest.raises(NotAnEpisodePageError):
        gaugau.episode(f"{WORK_URL}/episodes/999")


def test_series_title_falls_back_to_the_page_title(client, fake_response):
    gaugau, _ = client({f"/list/work/{WORK_ID}/episodes/": fake_response(text=episode_html(series=False))})
    assert gaugau.episode(EPISODE_URL).series_title == "宝石の聖女 第1話(1)"


def test_episode_refuses_a_work_url():
    with pytest.raises(UnsupportedUrlError):
        Gaugau().episode(WORK_URL)


def test_episode_refuses_another_server_type(client):
    gaugau, _ = client(server_type=0)
    with pytest.raises(GetjmangaError, match="ServerType"):
        gaugau.episode(EPISODE_URL)


def test_a_volume_trial_reads_like_an_episode(client, fake_response):
    reader_url = f"{WORK_URL}/reader/comics/{CONTENT_ID}"
    gaugau, _ = client({"/reader/comics/": fake_response(text=episode_html(title="宝石の聖女 1 【コミック】"))})
    episode = gaugau.episode(reader_url)

    assert episode.episode_title == "宝石の聖女 1 【コミック】"
    assert len(episode.pages) == 2
    assert episode.next_url is None


# --- listing a series ---------------------------------------------------------------------


def test_series_urls_lists_the_episodes_oldest_first(client):
    gaugau, session = client()
    expected = [f"{WORK_URL}/episodes/{order}" for order in (1, 2, 3)]
    assert gaugau.series_urls(WORK_URL) == expected
    assert gaugau.series_urls(f"{WORK_URL}/episodes") == expected
    # One request for the list however often it is asked for.
    assert session.calls == [f"{WORK_URL}/episodes"]


def test_series_urls_lists_the_volume_trials_once_each(client):
    gaugau, _ = client()
    assert gaugau.series_urls(f"{WORK_URL}/comics") == [f"{WORK_URL}/reader/comics/{CONTENT_ID}"]


def test_series_urls_ignores_another_works_links(client, fake_response):
    gaugau, _ = client({f"/list/work/{WORK_ID}/episodes": fake_response(text=listing_html(work_id="other"))})
    with pytest.raises(NotAnEpisodePageError):
        gaugau.series_urls(WORK_URL)


def test_series_urls_refuses_an_episode_url(client):
    gaugau, _ = client()
    with pytest.raises(UnsupportedUrlError):
        gaugau.series_urls(EPISODE_URL)


# --- downloading ---------------------------------------------------------------------------


def test_download_descrambles_every_page(client, fake_response, tmp_path):
    raw = BytesIO()
    served_image([1, 0, 3, 2]).save(raw, "PNG")
    gaugau, session = client({"/M_H.jpg": fake_response(raw.getvalue(), content_type="image/png")})

    result = Downloader(gaugau, tmp_path, save_metadata=True).download(EPISODE_URL)

    assert result.status == "saved"
    assert result.save_dir == tmp_path / "宝石の聖女" / "第1話(1)"
    assert sorted(path.name for path in result.save_dir.iterdir()) == ["0.jpg", "1.jpg", "metadata.json"]
    page = Image.open(result.save_dir / "0.jpg")
    assert page.size == (392, 392)
    assert page.convert("RGB").getpixel((5, 5)) == pytest.approx(COLOURS[0], abs=8)  # red, back in the top-left
    assert session.headers_seen[-1]["Referer"] == EPISODE_URL
    json.loads((result.save_dir / "metadata.json").read_text(encoding="utf-8"))


# --- the real site --------------------------------------------------------------------

# One episode per known host, free to read without an account.
TEST_URLS: dict[str, str] = {
    "gaugau.futabanet.jp": "https://gaugau.futabanet.jp/list/work/g5JBaom0KiedHfUAbnZwXURtl/episodes/1",
}


@pytest.mark.network
@pytest.mark.parametrize("host", TEST_URLS)
def test_site_download(tmp_path, host):
    result = Downloader(Gaugau(), tmp_path, only_first=True).download(TEST_URLS[host])
    assert result.status == "saved"
    assert (result.save_dir / "0.jpg").stat().st_size > 0
    # A page the tables did not put back together keeps its padding.
    assert Image.open(result.save_dir / "0.jpg").size == (result.episode.pages[0].width, result.episode.pages[0].height)


@pytest.mark.network
def test_work_page_lists_episodes():
    urls = Gaugau().series_urls("https://gaugau.futabanet.jp/list/work/g5JBaom0KiedHfUAbnZwXURtl")
    assert urls[0] == TEST_URLS["gaugau.futabanet.jp"]
    assert all(Gaugau.suitable(url) for url in urls)
