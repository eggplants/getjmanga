from __future__ import annotations

import json
from datetime import date
from http import HTTPStatus
from io import BytesIO

import pytest
from PIL import Image

from getjmanga.downloader import Downloader
from getjmanga.errors import GetjmangaError, NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractors.yomonga import Yomonga

TITLE_ID = "2570"
SERIES_URL = f"https://www.yomonga.com/titles/{TITLE_ID}/"
EPISODE_URL = f"{SERIES_URL}?episode=1&cid=10788"
NEXT_URL = f"{SERIES_URL}?episode=2&cid=10789"
INFO_URL = "https://www.yomonga.com/binb/sws/apis/bibGetCntntInfo.php"
SERVER = "https://www.yomonga.com/books/10788/1"

# A 2x2 grid, two pixels of padding, with the narrow column and the short row last
# on both sides: `BB` (short row per column) + `BB` (narrow column per row) + order.
IDENTITY_CTBL = "=2-2+2-BBBBABCD"
SWAPPED_PTBL = "=2-2-2-BBBBBADC"

COLOURS = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0)]

# The episodes a work page lists, newest first: (number, content id, name, end date).
LISTED = (
    (7, "10794", "Chapter.7 第4話-1", "2026/10/01に公開終了"),
    (6, "10793", "Chapter.6 第3話-2", "2026/10/01に公開終了"),
    (2, "10789", "Chapter.2 第1話-2", ""),
    (1, "10788", "Chapter.1 1巻_第1話-1", ""),
)


def work_html(episode_no=1, content_id="10788", episode_title="Chapter.1 1巻_第1話-1", *, viewer=True, listed=LISTED):
    """A work page: the viewer set to one episode, and the episode list newest first."""
    rows = "".join(
        f"""
    <div class="episode-list" data-episode_no="{number}">
        <div>
            <div class="update-date">2026/08/21</div>
            <div class="episode-name">{name}</div>
            <div class="publish-end-date{"" if end else " disable"}">{end or "に公開終了"}</div>
        </div>
        <a class="button-type1 episode-list-button"
           href="https://www.yomonga.com/titles/{TITLE_ID}/?episode={number}&cid={cid}#content"
           data-ga_event_name="read_on_viewer_clicked">
            読む
        </a>
    </div>"""
        for number, cid, name, end in listed
    )
    content = (
        '<div id="content" class="pages" data-ptbinb="/binb/sws/apis/bibGetCntntInfo.php"></div>' if viewer else ""
    )
    return f"""
<html><head><title>きらめきの大和くん☆{episode_title} | マンガよもんが</title>
<script>
    const my_url = new URL(window.location.href);
  const binb_cid = '{content_id}';
  const title_id = {TITLE_ID};
  const episode_no = {episode_no};
  window.title_id = title_id;
  window.episode_no = episode_no;
</script></head><body>
<div id="contents" class="cst_info"><div id="content_base">{content}<div id="content_filter"></div></div></div>
<main id="main" class="detail cst_info cst_on_normal">
    <div class="detail-title">{episode_title}</div>
    <div class="wrapper-1060px wrapper-episode-list">{rows}
    <div class="episode-list episode-list-button">
        <span>Chapter.3 第2話-1〜Chapter.5 第3話-1の公開は終了しました。</span>
        <a class="button-type-read-book episode-list-button" href="#comics-area">単行本で読む</a>
    </div>
    </div>
    <div class="main-wrapper" id="title-introduce">
        <div class="intr"><div class="intr-text">
            <div class="intr-title">きらめきの大和くん☆</div>
            <a href="https://www.yomonga.com/titles/?author_id=441"><span>バニラ梨央</span></a>
            <div class="intr-title2">売れっ子アイドル×お疲れOLのゆるゆるラブ（？）コメディー開幕！</div>
        </div></div>
    </div>
</main></body></html>
"""


def content_json(srcs=("images/a.jpg", "images/b.jpg")):
    """What `<ContentsServer>/content` answers: plain JSON, the pages as `<t-img>` tags."""
    imgs = "".join(
        f'<t-img src="{src}" a="0" height="100%" shrink="screen" orgwidth="392" orgheight="392" preview="false" '
        f'id="P{index:04d}"><t-pb>'
        for index, src in enumerate(srcs)
    )
    ttx = f'<html><body><t-case screen.portrait="screen.portrait">{imgs}</t-case></body></html>'
    return json.dumps(
        {"SBCVersion": "01.6890", "result": 1, "ttx": ttx, "ConverterType": "image+zip", "ImageClass": "default"},
    )


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
        self.is_success = True

    def raise_for_status(self):
        pass

    def json(self):
        if self.body is not None:
            return self.body
        params = self.session.params_seen[-1]
        item = {
            "ContentID": params["cid"],
            "ContentsServer": SERVER + "/",
            "ServerType": self.server_type,
            "Authors": [{"Name": "バニラ梨央", "Ruby": "バニラリオ", "Role": "漫画"}],
            "Publisher": "ぶんか社",
            "Title": "きらめきの大和くん☆ Chapter.1 1巻_第1話-1 | マンガよもんが",
            "ViewMode": 1,
            "ShopURL": "",
            "ctbl": encode_table(params["cid"], params["k"], self.ctbl),
            "ptbl": encode_table(params["cid"], params["k"], self.ptbl),
            **self.item,
        }
        return {"result": 1, "ShopUserID": "anomymous", "items": [item]}


@pytest.fixture
def client(fake_session, fake_response):
    """A Yomonga on a fake site: the work page for each listed episode, the API, the page list."""

    def build(routes=None, **info):
        session = fake_session({})
        # Routes match by substring, so the two-digit episode goes first.
        session.routes = {
            f"/titles/{TITLE_ID}/?episode=1": fake_response(text=work_html()),
            f"/titles/{TITLE_ID}/?episode=2": fake_response(text=work_html(2, "10789", "Chapter.2 第1話-2")),
            f"/titles/{TITLE_ID}/?episode=7": fake_response(text=work_html(7, "10794", "Chapter.7 第4話-1")),
            # An episode whose free run is over: the site shows the newest one instead.
            f"/titles/{TITLE_ID}/?episode=": fake_response(text=work_html(7, "10794", "Chapter.7 第4話-1")),
            f"/titles/{TITLE_ID}/": fake_response(text=work_html()),
            "bibGetCntntInfo": InfoResponse(session, **info),
            f"{SERVER}/content": fake_response(text=content_json()),
        }
        session.routes.update(routes or {})
        return Yomonga(session), session

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
        f"{EPISODE_URL}#content",
        f"{SERIES_URL}?episode=1",
        f"https://www.yomonga.com/titles/{TITLE_ID}?episode=1",
        SERIES_URL,
        f"https://www.yomonga.com/titles/{TITLE_ID}",
    ],
)
def test_suitable_accepts_work_and_episode_urls(url):
    assert Yomonga.suitable(url)


@pytest.mark.parametrize(
    "url",
    [
        f"http://www.yomonga.com/titles/{TITLE_ID}/?episode=1",
        f"https://yomonga.com/titles/{TITLE_ID}/?episode=1",
        "https://www.yomonga.com/",
        "https://www.yomonga.com/titles/",
        "https://www.yomonga.com/titles/?category_id=3",
        f"https://www.yomonga.com/titles/{TITLE_ID}/hyoushi.jpg",
        "https://www.yomonga.com/episode/",
        INFO_URL,
    ],
)
def test_suitable_rejects_other_urls(url):
    assert not Yomonga.suitable(url)


def test_is_series_is_true_without_an_episode_picked():
    assert Yomonga.is_series(SERIES_URL)
    assert Yomonga.is_series(f"https://www.yomonga.com/titles/{TITLE_ID}")
    assert not Yomonga.is_series(EPISODE_URL)
    assert not Yomonga.is_series(f"{SERIES_URL}?episode=1")
    assert not Yomonga.is_series("https://www.yomonga.com/titles/")


# --- parsing --------------------------------------------------------------------------


# --- episodes -------------------------------------------------------------------------


def test_episode_reads_the_titles_the_pages_and_the_next_episode(client):
    yomonga, session = client()
    episode = yomonga.episode(EPISODE_URL)

    assert episode.url == EPISODE_URL
    assert episode.series_title == "きらめきの大和くん☆"
    assert (episode.writer, episode.publisher) == ("バニラ梨央 (漫画)", "ぶんか社")
    assert (episode.published, episode.number) == (date(2026, 8, 21), 1)
    assert episode.episode_title == "Chapter.1 1巻_第1話-1"
    assert [page.url for page in episode.pages] == [f"{SERVER}/img/images/a.jpg", f"{SERVER}/img/images/b.jpg"]
    assert episode.pages[0].width == 392
    assert episode.pages[0].extra == {"ctbl": IDENTITY_CTBL, "ptbl": SWAPPED_PTBL}
    assert episode.next_url == NEXT_URL
    assert episode.readable
    assert episode.metadata["title_id"] == TITLE_ID
    assert episode.metadata["episode_no"] == 1
    assert episode.metadata["content_id"] == "10788"
    assert episode.metadata["contents_server"] == SERVER
    assert episode.metadata["publish_end"] == ""
    assert episode.metadata["publisher"] == "ぶんか社"
    json.dumps(episode.metadata)

    # The work page, the API, the page list -- in that order, nothing else.
    assert session.calls == [EPISODE_URL, INFO_URL, f"{SERVER}/content"]


def test_episode_calls_the_api_the_way_the_viewer_does(client):
    yomonga, session = client()
    yomonga.episode(EPISODE_URL)

    params = session.params_seen[1]
    assert params["cid"] == "10788"
    assert len(params["k"]) == 32
    assert "dmytime" in params
    assert session.headers_seen[1]["Referer"] == EPISODE_URL
    assert session.headers_seen[2]["Referer"] == EPISODE_URL
    assert session.params_seen[2] is None


@pytest.mark.parametrize(
    "url",
    [
        f"{SERIES_URL}?episode=1",
        f"{EPISODE_URL}#content",
        f"https://www.yomonga.com/titles/{TITLE_ID}?episode=1&cid=10788",
    ],
)
def test_episode_takes_every_spelling_of_an_episode_url(client, url):
    yomonga, session = client()
    episode = yomonga.episode(url)
    assert episode.episode_title == "Chapter.1 1巻_第1話-1"
    assert session.calls[0] in (EPISODE_URL, f"{SERIES_URL}?episode=1")


def test_the_next_episode_skips_the_ones_no_longer_listed(client):
    yomonga, _ = client()
    episode = yomonga.episode(NEXT_URL)
    assert episode.episode_title == "Chapter.2 第1話-2"
    assert episode.next_url == f"{SERIES_URL}?episode=6&cid=10793"


def test_the_newest_episode_has_no_next(client):
    yomonga, _ = client()
    episode = yomonga.episode(f"{SERIES_URL}?episode=7&cid=10794")
    assert episode.readable
    assert episode.metadata["publish_end"] == "2026/10/01に公開終了"
    assert (episode.prev_url, episode.next_url) == (f"{SERIES_URL}?episode=6&cid=10793", None)


def test_an_episode_whose_free_run_is_over_is_locked_but_still_has_a_next(client):
    yomonga, session = client()
    episode = yomonga.episode(f"{SERIES_URL}?episode=4&cid=10791")

    assert episode.pages == ()
    assert not episode.readable
    assert episode.url == f"{SERIES_URL}?episode=4&cid=10791"
    assert episode.series_title == "きらめきの大和くん☆"
    assert episode.episode_title == "Chapter.4"
    # Episodes 3 and 5 are not listed any more, so the neighbours are 2 and 6.
    assert (episode.prev_url, episode.next_url) == (NEXT_URL, f"{SERIES_URL}?episode=6&cid=10793")
    assert episode.metadata == {"title_id": TITLE_ID, "episode_no": 4, "locked": True}
    # No viewer handshake for an episode the site did not show.
    assert session.calls == [f"{SERIES_URL}?episode=4&cid=10791"]


def test_an_episode_after_the_newest_one_is_not_an_episode(client):
    yomonga, _ = client()
    with pytest.raises(NotAnEpisodePageError, match="lists none after 99"):
        yomonga.episode(f"{SERIES_URL}?episode=99")


def test_a_404_is_not_an_episode(client, fake_response):
    yomonga, _ = client({"/titles/999999/": fake_response(text="not found", status_code=HTTPStatus.NOT_FOUND)})
    with pytest.raises(NotAnEpisodePageError, match="404"):
        yomonga.episode("https://www.yomonga.com/titles/999999/?episode=1")


def test_a_work_page_without_a_viewer_is_not_an_episode(client, fake_response):
    yomonga, _ = client({f"/titles/{TITLE_ID}/?episode=1": fake_response(text=work_html(viewer=False))})
    with pytest.raises(NotAnEpisodePageError, match="viewer"):
        yomonga.episode(EPISODE_URL)


def test_episode_refuses_a_series_url(client):
    yomonga, _ = client()
    with pytest.raises(UnsupportedUrlError):
        yomonga.episode(SERIES_URL)


def test_episode_refuses_an_api_error(client):
    yomonga, _ = client(body={"result": 0, "items": []})
    with pytest.raises(NotAnEpisodePageError):
        yomonga.episode(EPISODE_URL)


def test_episode_refuses_another_server_type(client):
    yomonga, _ = client(server_type=1)
    with pytest.raises(GetjmangaError, match="ServerType 1"):
        yomonga.episode(EPISODE_URL)


def test_episode_refuses_a_rejected_key(client):
    # What the server hands a client whose `k` carried no checksum: decoy tables in the clear.
    yomonga, _ = client(
        body={
            "result": 1,
            "items": [{"ContentsServer": SERVER, "ServerType": 2, "ctbl": "8-8-4-A", "ptbl": "8-8-4-A"}],
        },
    )
    with pytest.raises(GetjmangaError, match="key was rejected"):
        yomonga.episode(EPISODE_URL)


# --- series ---------------------------------------------------------------------------


def test_series_urls_lists_the_readable_episodes_oldest_first(client):
    yomonga, session = client()
    urls = yomonga.series_urls(SERIES_URL)
    assert urls == [
        EPISODE_URL,
        NEXT_URL,
        f"{SERIES_URL}?episode=6&cid=10793",
        f"{SERIES_URL}?episode=7&cid=10794",
    ]
    assert all(Yomonga.suitable(url) and not Yomonga.is_series(url) for url in urls)
    assert session.calls == [SERIES_URL]


def test_series_urls_with_nothing_listed(client, fake_response):
    yomonga, _ = client({f"/titles/{TITLE_ID}/": fake_response(text=work_html(listed=()))})
    with pytest.raises(NotAnEpisodePageError):
        yomonga.series_urls(SERIES_URL)


def test_series_urls_refuses_an_episode_url(client):
    yomonga, _ = client()
    with pytest.raises(UnsupportedUrlError):
        yomonga.series_urls(EPISODE_URL)
    with pytest.raises(UnsupportedUrlError):
        yomonga.series_urls("https://www.yomonga.com/titles/")


# --- downloading ---------------------------------------------------------------------------


def test_download_descrambles_every_page(client, fake_response, tmp_path):
    raw = BytesIO()
    served_image([1, 0, 3, 2]).save(raw, "PNG")
    yomonga, session = client({f"{SERVER}/img/": fake_response(raw.getvalue(), content_type="image/png")})

    result = Downloader(yomonga, tmp_path, save_metadata=True).download(EPISODE_URL)

    assert result.status == "saved"
    assert result.save_dir == tmp_path / "www.yomonga.com" / "きらめきの大和くん☆" / "Chapter.1 1巻_第1話-1"
    assert sorted(path.name for path in result.save_dir.iterdir()) == ["0.jpg", "1.jpg", "metadata.json"]
    page = Image.open(result.save_dir / "0.jpg")
    assert page.size == (392, 392)
    assert page.convert("RGB").getpixel((5, 5)) == pytest.approx(COLOURS[0], abs=8)  # red, back in the top-left
    assert page.convert("RGB").getpixel((390, 390)) == pytest.approx(COLOURS[3], abs=8)
    assert session.calls[-1] == f"{SERVER}/img/images/b.jpg"
    assert session.headers_seen[-1]["Referer"] == EPISODE_URL
    json.loads((result.save_dir / "metadata.json").read_text(encoding="utf-8"))


# --- the real site --------------------------------------------------------------------

# One episode per known host, free to read without an account.
TEST_URLS: dict[str, str] = {
    "www.yomonga.com": "https://www.yomonga.com/titles/2570/?episode=1&cid=10788",
}


@pytest.mark.network
@pytest.mark.parametrize("host", TEST_URLS)
def test_site_download(tmp_path, host):
    result = Downloader(Yomonga(), tmp_path, only_first=True).download(TEST_URLS[host])
    assert result.status == "saved"
    assert (result.save_dir / "0.jpg").stat().st_size > 0
    assert result.episode.next_url == "https://www.yomonga.com/titles/2570/?episode=2&cid=10789"


@pytest.mark.network
def test_work_page_lists_episodes():
    urls = Yomonga().series_urls("https://www.yomonga.com/titles/2570/")
    assert urls[0] == TEST_URLS["www.yomonga.com"]
    assert all(Yomonga.suitable(url) for url in urls)
