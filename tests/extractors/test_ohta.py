from __future__ import annotations

import json
from datetime import date
from http import HTTPStatus
from io import BytesIO

import pytest
from httpx import HTTPStatusError
from PIL import Image

from getjmanga.downloader import Downloader
from getjmanga.errors import NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractors.ohta import Ohta, parse_work

WORK_URL = "https://webcomic.ohtabooks.com/kishotenten/"
EPISODE_URL = "https://www.yondemill.jp/contents/64823"
NEXT_URL = "https://www.yondemill.jp/contents/71449"
BINB_ID = "3b4a9926-e2ce-429c-8e09-6f331ccfcf1d_1728028949"
TOKEN = "1b0aaf3c3085435bb47a4d4396b83390"
READER_URL = f"https://binb.bricks.pub/contents/{BINB_ID}/speed_reader?u0={TOKEN}&u1=redirect"
INFO_URL = f"https://console.binb.bricks.pub/bibGetCntntInfo?u0={TOKEN}"
SERVER = f"https://s3-ap-northeast-1.amazonaws.com/binb.bricks.pub/output/{BINB_ID}/member_trial"

# A 2x2 grid, two pixels of padding, with the narrow column and the short row last
# on both sides: `BB` (short row per column) + `BB` (narrow column per row) + order.
IDENTITY_CTBL = "=2-2+2-BBBBABCD"
SWAPPED_PTBL = "=2-2-2-BBBBBADC"

COLOURS = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0)]

# The work page as the publisher writes it: the title, the header buttons, and
# the backnumber list newest first -- the newest free, the middle ones sold on
# another store, two expired ones commented out, the first one free.
WORK_HTML = """
<html><head><title>起承転転／雁須磨子 - Ohta Web Comic [太田出版のウェブ漫画]</title>
<script>
function openBook(partid)
{
    var newWin = window.open("https://yondemill.jp/contents/" + partid + "?view=1&u0=1", this.target, param );
}
</script></head><body>
<h2 class="contentTitle titleBoader" itemprop="name">起承転転</h2>
<div class="list headBtnList clearfix"><ul>
  <li class="wide"><a onclick="return !openBook('71449')" href="" class="btn icon arrow new">
    最新話をよむ<small>＜LATEST EPISODE 9＞</small></a></li>
  <li ><a onclick="return !openBook('64823')" href="" class="btn icon arrow">
    初めから<small>＜FIRST EPISODE＞</small></a></li>
</ul></div>
<h3 class="titleBoader">リスト</h3>
<ul class="backnumberList">
  <li><a onClick="return !openBook('71449')" href=""><dl>
    <dt class="number btn new">16<small>＜期間限定無料＞</small></dt>
    <dd><div class="title">第9話　50歳のもらい泣き</div><div class="detail">30p　次回更新まで無料！</div></dd>
  </dl></a></li>
  <li><a href="https://www.cmoa.jp/title/324247/vol/16/" target="_blank"><dl>
    <dt class="number btn">15</dt>
    <dd><div class="title">第8話　50歳のなんらかの決意（まだ弱）　後半</div>
      <div class="detail">15p　コミックシーモアで配信中（単話版16）</div></dd>
  </dl></a></li>
<!-- 限定リンク
  <li><a onClick="return !openBook('66219')" href=""><dl>
    <dt class="number btn">3<small>＜期間限定無料＞</small></dt>
    <dd><div class="title">第3話　50歳の窮地</div><div class="detail">30p　12月14日まで無料公開中！</div></dd>
  </dl></a></li>
-->
  <li><dl>
    <dt class="number btn">2</dt>
    <dd><div class="title">第2話　50歳の職探し</div><div class="detail">無料公開終了</div></dd>
  </dl></li>
  <li><a onClick="return !openBook('64823')" href=""><dl>
    <dt class="number btn">1<small>＜無料＞</small></dt>
    <dd><div class="title">第1話　50歳の転機<small>(32p)</small></div><div class="detail">32p　無料公開中！</div></dd>
  </dl></a></li>
</ul>
</body></html>
"""

# A work page with no backnumber list, only the header's trial button.
TRIAL_WORK_HTML = """
<html><body>
<h2 class="contentTitle titleBoader" itemprop="name">無名のお笑い芸人がある日ゾンビに噛まれた結果</h2>
<div class="list headBtnList clearfix"><ul>
  <li class="wide"><a onclick="return !openBook('13821')" href="" class="btn icon arrow">
    お試しよみ<small>＜FIRST EPISODE＞</small></a></li>
</ul></div>
</body></html>
"""

EMPTY_WORK_HTML = """
<html><body><h2 class="contentTitle titleBoader" itemprop="name">まだ始まらない</h2>
<ul class="backnumberList"><li><dl><dt class="number btn">1</dt><dd><div class="title">第1話</div>
<div class="detail">無料公開終了</div></dd></dl></li></ul></body></html>
"""


def content_html(title="起承転転　第1話", *, work_link=True, sales="OFF"):
    """A YONDEMILL content page: the title, the author, the label, the link back to the work page."""
    back = (
        f'<div class="text-center mb-3"><a class="text-default" target="_blank" href="{WORK_URL}">'
        '<i class="fa fa-external-link text-default"></i> 「起承転転」作品TOPへ戻る</a></div>'
        if work_link
        else ""
    )
    return f"""
<html><head><title>{title} - つながりで読むWebの本 YONDEMILL（ヨンデミル）</title>
<script>var sales = '';
var read_right = '';
sales = '{sales}';read_right = 'no';layout_type = 'reflowable';reader_type = 'binb_infoview';
dataLayer = [{{'signed_in': "signed_out"}}];</script>
</head><body>
<a class="navbar-brand" href="/">YONDEMILL</a>
<a href="https://webcomic.ohtabooks.com/list/">archive, not a work</a>
<div class="card mt-3"><a class="button btn btn-block btn-info btn-lg" href="/contents/64823?view=1">読む</a></div>
<div class="card card-primary"><div class="card-summary closed" id="description">
<div class="card-summary-header clearfix"><h1 class="card-title h4">{title}</h1></div>
<div class="card-summary-block"><p>雁須磨子 著</p><p></p><p><a href="/labels/29">太田出版</a></p></div>
<div class="card-summary-block"><p>誰かの妻にもならず、誰かの母にもならず、娘のまま50才になった。</p></div>
</div></div>
{back}
</body></html>
"""


def stub_html(reader_url=READER_URL):
    """What `?view=1` serves: a script sending the browser to the reader, or nothing of the kind."""
    script = f"<script type=\"text/javascript\">\n    location.href='{reader_url}';\n  </script>" if reader_url else ""
    return f"<html><head><title>起承転転　第1話</title></head><body>{script}</body></html>"


def reader_html(*, viewer=True):
    content = (
        f'<div class="pages" id="content" data-ptbinb="{INFO_URL}" data-ptbinb-cid="{BINB_ID}"></div>' if viewer else ""
    )
    return (
        f'<html><head><base href="/speedreader/"><title>BinB Speed Reader</title></head><body>{content}</body></html>'
    )


def content_js(srcs=("pages/a.jpg", "pages/b.jpg"), image_class="default"):
    imgs = "".join(
        f'<t-img src="{src}" a="0" orgwidth="392" orgheight="392" id="P{index:04d}"><t-pb>'
        for index, src in enumerate(srcs)
    )
    ttx = f'<html><body><t-case screen.portrait="screen.portrait">{imgs}</t-case></body>'
    body = {"result": 1, "ttx": ttx, "ImageClass": image_class, "AddressList": [[0, len(srcs) - 1, 0, len(srcs) - 1]]}
    return "DataGet_Content(" + json.dumps(body) + ")"


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

    def __init__(self, session, *, ctbl=(IDENTITY_CTBL,), ptbl=(SWAPPED_PTBL,), server_type=1, body=None, **item):
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
        key = self.session.params_seen[-1]["k"]
        item = {
            "ContentID": BINB_ID,
            "ContentsServer": SERVER + "/",
            "ServerType": self.server_type,
            "Title": "起承転転　第1話",
            "ViewMode": 3,
            "ShopURL": None,
            "ctbl": encode_table(BINB_ID, key, self.ctbl),
            "ptbl": encode_table(BINB_ID, key, self.ptbl),
            **self.item,
        }
        return {"result": 1, "rurl": EPISODE_URL, "items": [item]}


@pytest.fixture
def client(fake_session, fake_response):
    """An Ohta on a fake site: the work page, the content page, its stub, the reader, the API, content.js."""

    def build(routes=None, **info):
        session = fake_session({})
        # The stub route must come before the content page's: routes match by
        # substring. An override keeps the position of the route it replaces.
        session.routes = {
            "/contents/64823?view=1": fake_response(text=stub_html()),
            "/contents/64823": fake_response(text=content_html()),
            "/speed_reader": fake_response(text=reader_html()),
            "bibGetCntntInfo": InfoResponse(session, **info),
            "content.js": fake_response(text=content_js()),
            "webcomic.ohtabooks.com/kishotenten/": fake_response(text=WORK_HTML),
        }
        session.routes.update(routes or {})
        return Ohta(session), session

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
        "https://www.yondemill.jp/contents/64823/",
        "https://www.yondemill.jp/contents/64823?view=1&u0=1",
        "https://yondemill.jp/contents/64823?view=1&u0=1",
        WORK_URL,
        "https://webcomic.ohtabooks.com/kishotenten",
        "https://webcomic.ohtabooks.com/koi-to-batsu/",
    ],
)
def test_suitable_accepts_content_and_work_urls(url):
    assert Ohta.suitable(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://www.yondemill.jp/contents/64823",
        "https://www.yondemill.jp/",
        "https://www.yondemill.jp/contents/",
        "https://www.yondemill.jp/labels/29",
        "https://www.yondemill.jp/ebooks/72644/order",
        "https://www.yondemill.jp/readers/sign_in",
        "https://webcomic.ohtabooks.com/",
        "https://webcomic.ohtabooks.com/list/",
        "https://webcomic.ohtabooks.com/kishotenten/attachment/x/",
        "https://webcomic.ohtabooks.com/contents/64823",
        "https://www.ohtabooks.com/sp/kishotenten/",
        READER_URL,
    ],
)
def test_suitable_rejects_other_urls(url):
    assert not Ohta.suitable(url)


def test_is_series_is_true_for_a_work_page_only():
    assert Ohta.is_series(WORK_URL)
    assert Ohta.is_series("https://webcomic.ohtabooks.com/kishotenten")
    assert not Ohta.is_series(EPISODE_URL)
    assert not Ohta.is_series("https://webcomic.ohtabooks.com/list/")


# --- parsing --------------------------------------------------------------------------


def test_parse_work_falls_back_on_the_header_buttons():
    work = parse_work(TRIAL_WORK_HTML, "https://webcomic.ohtabooks.com/zombie/")
    assert work.title == "無名のお笑い芸人がある日ゾンビに噛まれた結果"
    assert work.episodes == {"13821": ""}


# --- episode --------------------------------------------------------------------------


def test_episode_reads_the_titles_the_pages_and_the_next_episode(client):
    ohta, session = client()
    episode = ohta.episode(EPISODE_URL)

    assert episode.url == EPISODE_URL
    # The work page names the series and the episode; YONDEMILL's title is only metadata.
    assert episode.series_title == "起承転転"
    assert (episode.writer, episode.publisher) == ("雁須磨子 (著)", "太田出版")
    assert episode.episode_title == "第1話　50歳の転機(32p)"
    assert [page.url for page in episode.pages] == [f"{SERVER}/pages/a.jpg/M_H.jpg", f"{SERVER}/pages/b.jpg/M_H.jpg"]
    assert episode.pages[0].width == 392
    assert episode.pages[0].extra == {"ctbl": IDENTITY_CTBL, "ptbl": SWAPPED_PTBL}
    assert episode.next_url == NEXT_URL
    assert episode.readable
    assert episode.metadata["content_id"] == "64823"
    assert episode.metadata["binb_id"] == BINB_ID
    assert episode.metadata["contents_server"] == SERVER
    assert episode.metadata["title"] == "起承転転　第1話"
    assert episode.metadata["work_url"] == WORK_URL
    assert episode.metadata["shop_url"] is None
    assert episode.metadata["address_list"] == [[0, 1, 0, 1]]
    json.dumps(episode.metadata)

    # The content page, the stub, the reader, the API, content.js, then the work page -- in that order.
    assert session.calls == [
        EPISODE_URL,
        f"{EPISODE_URL}?view=1",
        READER_URL,
        INFO_URL,
        f"{SERVER}/content.js",
        WORK_URL,
    ]


def test_episode_is_dated_by_its_first_page_upload(client, fake_response, uploaded):
    ohta, _ = client({"/pages/a.jpg/M_H.jpg": fake_response(b"", headers=uploaded)})
    assert ohta.episode(EPISODE_URL).published == date(2025, 8, 21)


@pytest.mark.parametrize(
    "url",
    [
        "https://yondemill.jp/contents/64823?view=1&u0=1",
        "https://www.yondemill.jp/contents/64823/",
    ],
)
def test_episode_takes_the_url_the_work_page_opens(client, url):
    ohta, session = client()
    episode = ohta.episode(url)
    assert episode.url == EPISODE_URL
    assert session.calls[0] == EPISODE_URL


def test_last_episode_has_no_next(client, fake_response):
    ohta, _ = client(
        {
            "/contents/71449?view=1": fake_response(text=stub_html()),
            "/contents/71449": fake_response(text=content_html("起承転転　第9話")),
        },
    )
    episode = ohta.episode(NEXT_URL)
    assert episode.episode_title == "第9話　50歳のもらい泣き"
    assert (episode.prev_url, episode.next_url) == (EPISODE_URL, None)


def test_episode_the_work_page_does_not_list_splits_the_content_title(client, fake_response):
    ohta, _ = client(
        {
            "/contents/60000?view=1": fake_response(text=stub_html()),
            "/contents/60000": fake_response(text=content_html("起承転転　番外編")),
        },
    )
    episode = ohta.episode("https://www.yondemill.jp/contents/60000")
    assert episode.series_title == "起承転転"
    assert episode.episode_title == "番外編"
    assert episode.next_url is None


def test_content_without_a_work_page_is_titled_off_yondemill(client, fake_response):
    ohta, session = client(
        {
            "/contents/72644?view=1": fake_response(text=stub_html()),
            "/contents/72644": fake_response(text=content_html("DP　DOG's DAY　下　分冊版1", work_link=False)),
        },
        ShopURL="https://www.yondemill.jp/ebooks/72644/order",
    )
    episode = ohta.episode("https://www.yondemill.jp/contents/72644")

    assert episode.series_title == "DP"
    assert episode.episode_title == "DOG's DAY　下　分冊版1"
    assert episode.next_url is None
    assert episode.metadata["work_url"] is None
    assert episode.metadata["shop_url"] == "https://www.yondemill.jp/ebooks/72644/order"
    assert WORK_URL not in session.calls


def test_content_that_does_not_redirect_to_the_reader_is_locked(client, fake_response):
    ohta, session = client({"/contents/64823?view=1": fake_response(text=stub_html(reader_url=""))})
    episode = ohta.episode(EPISODE_URL)

    assert episode.series_title == "起承転転"
    assert episode.episode_title == "第1話　50歳の転機(32p)"
    assert episode.pages == ()
    assert not episode.readable
    assert episode.next_url == NEXT_URL
    assert episode.metadata["locked"] is True
    assert not any("speed_reader" in call for call in session.calls)


def test_missing_content_is_not_an_episode(client, fake_response):
    ohta, _ = client(
        {
            "/contents/66219?view=1": fake_response(text="404", status_code=HTTPStatus.NOT_FOUND),
            "/contents/66219": fake_response(text="404", status_code=HTTPStatus.NOT_FOUND),
        },
    )
    with pytest.raises(NotAnEpisodePageError, match="404"):
        ohta.episode("https://www.yondemill.jp/contents/66219")


def test_content_page_without_a_content_is_not_an_episode(client, fake_response):
    html = "<html><body><h1>お探しのページが見つかりません。</h1></body></html>"
    ohta, _ = client({"/contents/64823": fake_response(text=html)})
    with pytest.raises(NotAnEpisodePageError, match="no content"):
        ohta.episode(EPISODE_URL)


def test_other_http_errors_propagate(client, fake_response):
    ohta, _ = client({"/contents/64823": fake_response(text="", status_code=HTTPStatus.SERVICE_UNAVAILABLE)})
    with pytest.raises(HTTPStatusError):
        ohta.episode(EPISODE_URL)


def test_episode_refuses_a_work_url(client):
    ohta, session = client()
    with pytest.raises(UnsupportedUrlError):
        ohta.episode(WORK_URL)
    assert session.calls == []


# --- series ---------------------------------------------------------------------------


@pytest.mark.parametrize("url", [WORK_URL, "https://webcomic.ohtabooks.com/kishotenten"])
def test_series_urls_lists_the_episodes_oldest_first(client, url):
    ohta, session = client()
    urls = ohta.series_urls(url)

    assert urls == [EPISODE_URL, NEXT_URL]
    assert all(Ohta.suitable(url) for url in urls)
    assert session.calls == [WORK_URL]


def test_series_urls_raises_on_an_empty_listing(client, fake_response):
    ohta, _ = client({"webcomic.ohtabooks.com/nothing/": fake_response(text=EMPTY_WORK_HTML)})
    with pytest.raises(NotAnEpisodePageError, match="no readable episode"):
        ohta.series_urls("https://webcomic.ohtabooks.com/nothing/")


def test_series_urls_raises_on_a_missing_work(client, fake_response):
    ohta, _ = client({"webcomic.ohtabooks.com/gone/": fake_response(text="", status_code=HTTPStatus.NOT_FOUND)})
    with pytest.raises(NotAnEpisodePageError, match="404"):
        ohta.series_urls("https://webcomic.ohtabooks.com/gone/")


def test_series_urls_refuses_a_url_of_the_wrong_shape(client):
    ohta, session = client()
    with pytest.raises(UnsupportedUrlError):
        ohta.series_urls("https://webcomic.ohtabooks.com/list/")
    assert session.calls == []


# --- images and downloading -----------------------------------------------------------


def test_image_puts_the_tiles_back(client, fake_response):
    raw = BytesIO()
    served_image([1, 0, 3, 2]).save(raw, "PNG")
    ohta, session = client({"/M_H.jpg": fake_response(raw.getvalue(), content_type="image/png")})
    episode = ohta.episode(EPISODE_URL)

    page = ohta.image(episode.pages[0], episode)

    assert page.size == (392, 392)
    assert [page.getpixel((column * 196 + 5, row * 196 + 5)) for row in range(2) for column in range(2)] == COLOURS
    assert session.calls[-1] == episode.pages[0].url
    assert session.headers_seen[-1]["Referer"] == EPISODE_URL


# --- logging in -----------------------------------------------------------------------


# --- the real site --------------------------------------------------------------------

# One URL per known host, free to read without an account: the first episode
# of 起承転転 on YONDEMILL (`yondemill.jp` redirects to `www.`), and the work
# page on the publisher's site, which has no episode pages of its own -- its
# first listed episode is what gets downloaded.
TEST_URLS: dict[str, str] = {
    "webcomic.ohtabooks.com": WORK_URL,
    "www.yondemill.jp": EPISODE_URL,
    "yondemill.jp": "https://yondemill.jp/contents/64823?view=1&u0=1",
}


@pytest.mark.network
@pytest.mark.parametrize("host", TEST_URLS)
def test_site_download(tmp_path, host):
    ohta = Ohta()
    url = TEST_URLS[host]
    if ohta.is_series(url):
        url = ohta.series_urls(url)[0]
    result = Downloader(ohta, tmp_path, only_first=True).download(url)
    assert result.status == "saved"
    assert result.episode.url == EPISODE_URL
    assert result.episode.series_title == "起承転転"
    assert result.episode.episode_title == "第1話　50歳の転機"
    assert result.episode.next_url is not None
    assert (result.save_dir / "0.jpg").stat().st_size > 0
    # A page the tables did not put back together keeps its padding.
    assert Image.open(result.save_dir / "0.jpg").size == (987, 1400)


@pytest.mark.network
def test_work_page_lists_episodes_oldest_first():
    ohta = Ohta()
    assert ohta.is_series(WORK_URL)
    urls = ohta.series_urls(WORK_URL)
    assert urls[0] == EPISODE_URL
    assert all(Ohta.suitable(url) for url in urls)


@pytest.mark.network
def test_taken_down_content_is_not_an_episode_on_the_site():
    with pytest.raises(NotAnEpisodePageError, match="404"):
        Ohta().episode("https://www.yondemill.jp/contents/66219")
