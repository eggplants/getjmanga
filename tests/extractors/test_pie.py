from __future__ import annotations

import json
from http import HTTPStatus
from io import BytesIO

import pytest
from PIL import Image
from requests import HTTPError

from getjmanga.downloader import Downloader
from getjmanga.extractors.common import NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractors.pie import Pie, parse_story, parse_work

WORK_URL = "https://comics.pie.co.jp/series/poetic/"
STORY_URL = "https://comics.pie.co.jp/story/alicia"
NEXT_STORY_URL = "https://comics.pie.co.jp/story/berenice"
MANGA_WORK_URL = "https://comics.pie.co.jp/series/hoshitabi/"
CONTENT_URL = "https://www.yondemill.jp/contents/52480"
NEXT_CONTENT_URL = "https://www.yondemill.jp/contents/72309"
UPLOADS = "https://comics.pie.co.jp/wp/wp-content/uploads"

BINB_ID = "f176115d-b604-4cd2-bb7f-8064eca81a88_1625800000"
TOKEN = "1b0aaf3c3085435bb47a4d4396b83390"
READER_URL = f"https://binb.bricks.pub/contents/{BINB_ID}/speed_reader?u0={TOKEN}&u1=redirect"
INFO_URL = f"https://console.binb.bricks.pub/bibGetCntntInfo?u0={TOKEN}"
SERVER = f"https://s3-ap-northeast-1.amazonaws.com/binb.bricks.pub/output/{BINB_ID}/member_trial"

# A 2x2 grid, two pixels of padding, the page side in order and the served side swapped pairwise.
IDENTITY_CTBL = "=2-2+2-BBBBABCD"
SWAPPED_PTBL = "=2-2-2-BBBBBADC"
COLOURS = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0)]


def work_html(title="Poetic Horror", items=None, *, sections=None):
    """A work page: the title, the author, and `section.p-series` blocks of listed episodes."""
    if sections is None:
        sections = [("連載中", items if items is not None else [])]
    blocks = []
    for heading, entries in sections:
        rows = []
        for date, name, href in entries:
            if href is None:
                rows.append(
                    f'<li class="p-series_item"><span class="p-series_nolink"><time>{date}更新</time>'
                    f'<span class="p-series_itemTitle">{name}</span></span></li>',
                )
            else:
                rows.append(
                    f'<li class="p-series_item"><a href="{href}" class="p-series_link" target="_blank">'
                    f'<div class="p-series_item_text"><time>{date}更新</time>'
                    f'<span class="p-series_itemTitle">{name}</span></div>'
                    '<div class="p-series_item_btn"><span class="c-btn">読む</span></div></a></li>',
                )
        blocks.append(
            f'<section class="p-series"><header><h3 class="p-series_title">{heading}</h3></header>'
            f'<ol class="p-series_list">{"".join(rows)}</ol></section>',
        )
    return f"""
<html><head><title>{title} | PIE COMICS</title></head><body>
<h1 class="p-work_headerTitle"><span>作品詳細</span><span class="p-work_latestUpdate">26.09.02更新</span></h1>
<div class="p-work_detail"><h1 class="p-work_title">{title}</h1><p class="p-work_author">ねこ助</p>
<div class="p-work_buttons"><a href="https://comics.pie.co.jp/story/henry" class="c-btn">最新話を読む</a></div></div>
<section class="p-work_sec"><h2 class="p-work_secTitle">作品を読む</h2>{"".join(blocks)}</section>
<aside><div class="p-comicMedia_btn">
<a href="https://www.yondemill.jp/contents/99999?view=1" class="c-btn">ランキングの作品</a></div></aside>
</body></html>
"""


POETIC_ITEMS = [
    ("2026.09.02", "【Part 8】Henry", "https://comics.pie.co.jp/story/henry"),
    ("2025.09.03", "【Part 2】Berenice", NEXT_STORY_URL),
    ("2025.08.06", "【Part 1】Alicia", STORY_URL),
]

# A manga: free episodes on YONDEMILL, the rest sold on Amazon or not public, over two volumes.
HOSHITABI_SECTIONS = [
    (
        "連載中",
        [
            ("2026.07.31", "【無料】第40話 PGT児童宿舎", f"{NEXT_CONTENT_URL}?view=1"),
            ("2026.06.19", "【非公開】第39話 宵越しラジオ", None),
            ("2026.05.15", "【単話購入】第38話 となり縫い", "https://www.amazon.co.jp/dp/B0H8RGJCDW/"),
        ],
    ),
    (
        "＜1巻＞収録",
        [
            ("2021.08.09", "【単話購入】第2話", "https://amzn.asia/d/jbOTOwg"),
            ("2021.07.09", "【無料】第1話 まどろみの星", f"{CONTENT_URL}?view=1"),
        ],
    ),
]


def story_html(
    title="【Part 1】Alicia",
    heading="Alicia",
    images=(f"{UPLOADS}/2025/07/01_A_1.jpg",),
    *,
    prev_url=None,
    next_url=NEXT_STORY_URL,
    breadcrumb=True,
):
    """A story page: the header, the breadcrumb, the post with its images, the prev/next links."""
    crumb = f'<li><a href="{WORK_URL.rstrip("/")}">Poetic Horror</a><i>></i></li>' if breadcrumb else ""
    imgs = "".join(f'<p><img class="size-full" src="{src}" alt="" width="1204" height="1700" /></p>' for src in images)
    prev = f'<a href="{prev_url}">前へ</a>' if prev_url else "<span>前へ</span>"
    nxt = f'<a href="{next_url}">次へ</a>' if next_url else "<span>次へ</span>"
    return f"""
<html><head><title>{title} | PIE COMICS</title>
<link rel="canonical" href="{STORY_URL}/" /></head><body>
<header class="p-work_header">
<h1 class="p-work_headerTitle is-2row">Poetic Horror<span class="p-work_latestUpdate">26.02.04更新</span></h1>
<div class="p-work_nav"><ul><li><a href="https://comics.pie.co.jp/comicart/illustration/">イラスト</a><i>></i></li>
{crumb}<li>{heading}</li></ul></div></header>
<div class="c-contentBlock c-contentBlock-single"><div class="c-contentBlock_body">
<div class="c-contentBlock_header"><h2 class="c-contentBlock_title">{heading}</h2></div>
<div class="c-content js-content"><h3>{heading}</h3>{imgs}<p>わたしあのこになりたかった</p></div></div>
<div class="c-contentBlock_footer"><div class="c-prevNext">
<div class="c-prevNext_prev">{prev}</div><div class="c-prevNext_back">{heading}</div>
<div class="c-prevNext_next">{nxt}</div></div></div></div>
<section class="p-work_sec"><div class="p-work_image"><a href="{WORK_URL.rstrip("/")}"><img src="x.jpg"></a></div>
</section></body></html>
"""


def content_html(title="星旅少年 第1話", *, back_link="https://pie.co.jp/series/4858311/"):
    """A YONDEMILL content page: the title and the link back to the publisher."""
    back = (
        f'<div class="text-center mb-3"><a target="_blank" href="{back_link}">パイコミックスに戻る</a></div>'
        if back_link
        else ""
    )
    return f"""
<html><head><title>{title} - YONDEMILL</title>
<script>sales = 'OFF';read_right = 'no';layout_type = 'reflowable';reader_type = 'binb_infoview';</script></head>
<body><a class="navbar-brand" href="/">YONDEMILL</a>
<div class="card mt-3"><a class="button" href="/contents/52480?view=1">読む</a></div>
<div class="card-summary-header"><h1 class="card-title h4">{title}</h1></div>
<div class="card-summary-block"><p>坂月さかな 著</p><p><a href="/labels/257">パイ インターナショナル</a></p></div>
{back}
</body></html>
"""


def stub_html(reader_url=READER_URL):
    script = f"<script>location.href='{reader_url}';</script>" if reader_url else ""
    return f"<html><head><title>星旅少年 第1話</title></head><body>{script}</body></html>"


def reader_html():
    return (
        '<html><body><div class="pages" id="content" '
        f'data-ptbinb="{INFO_URL}" data-ptbinb-cid="{BINB_ID}"></div></body></html>'
    )


def content_js(srcs=("pages/a.jpg", "pages/b.jpg")):
    imgs = "".join(
        f'<t-img src="{src}" a="0" orgwidth="392" orgheight="392" id="P{index:04d}"><t-pb>'
        for index, src in enumerate(srcs)
    )
    ttx = f'<html><body><t-case screen.portrait="screen.portrait">{imgs}</t-case></body>'
    body = {"result": 1, "ttx": ttx, "ImageClass": "default", "AddressList": [[0, len(srcs) - 1, 0, len(srcs) - 1]]}
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

    def __init__(self, session):
        self.session = session
        self.url = None
        self.status_code = HTTPStatus.OK
        self.ok = True

    def raise_for_status(self):
        pass

    def json(self):
        key = self.session.params_seen[-1]["k"]
        item = {
            "ContentID": BINB_ID,
            "ContentsServer": SERVER + "/",
            "ServerType": 1,
            "Title": "星旅少年 第1話",
            "ViewMode": 3,
            "ShopURL": None,
            "ctbl": encode_table(BINB_ID, key, [IDENTITY_CTBL]),
            "ptbl": encode_table(BINB_ID, key, [SWAPPED_PTBL]),
        }
        return {"result": 1, "rurl": CONTENT_URL, "items": [item]}


def served_image(order):
    """A 400x400 served image of a 2x2 grid, tile n painted COLOURS[order[n]] inside its padding."""
    image = Image.new("RGB", (400, 400), (0, 0, 0))
    for index, colour_index in enumerate(order):
        column, row = index % 2, index // 2
        image.paste(Image.new("RGB", (196, 196), COLOURS[colour_index]), (2 + column * 200, 2 + row * 200))
    return image


def png(colour, size=(8, 8)):
    raw = BytesIO()
    Image.new("RGB", size, colour).save(raw, "PNG")
    return raw.getvalue()


@pytest.fixture
def client(fake_session, fake_response):
    """A Pie on a fake site: two work pages, a story, a YONDEMILL content with its reader."""

    def build(routes=None):
        session = fake_session({})
        raw = BytesIO()
        served_image([1, 0, 3, 2]).save(raw, "PNG")
        # Routes match by substring: the stub and the more specific paths come first.
        session.routes = {
            "/series/poetic/": fake_response(text=work_html(items=POETIC_ITEMS)),
            "/series/hoshitabi/": fake_response(text=work_html("星旅少年", sections=HOSHITABI_SECTIONS)),
            "pie.co.jp/series/4858311/": fake_response(
                text=work_html("星旅少年", sections=HOSHITABI_SECTIONS),
                url=MANGA_WORK_URL,
            ),
            "/story/alicia": fake_response(text=story_html()),
            "/contents/52480?view=1": fake_response(text=stub_html()),
            "/contents/52480": fake_response(text=content_html()),
            "/speed_reader": fake_response(text=reader_html()),
            "bibGetCntntInfo": InfoResponse(session),
            "content.js": fake_response(text=content_js()),
            "/M_H.jpg": fake_response(raw.getvalue(), content_type="image/png"),
            "01_A_1.jpg": fake_response(png((1, 2, 3)), content_type="image/jpeg"),
        }
        # An override keeps the position of the route it replaces; a new route goes last.
        session.routes.update(routes or {})
        return Pie(session), session

    return build


# --- URLs -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        STORY_URL,
        f"{STORY_URL}/",
        "https://comics.pie.co.jp/story/%e5%af%ba%e7%94%b0%e5%85%8b%e4%b9%9f%e3%80%90%e7%ac%ac1%e5%9b%9e%e3%80%91",
        WORK_URL,
        "https://comics.pie.co.jp/series/poetic",
    ],
)
def test_suitable_accepts_story_and_work_urls(url):
    assert Pie.suitable(url)


@pytest.mark.parametrize(
    "url",
    [
        f"http://{STORY_URL.removeprefix('https://')}",
        "https://comics.pie.co.jp/",
        "https://comics.pie.co.jp/story/",
        "https://comics.pie.co.jp/series/",
        "https://comics.pie.co.jp/comicart/manga/",
        "https://comics.pie.co.jp/list/comicart/",
        "https://comics.pie.co.jp/story/alicia/attachment/x/",
        "https://pie.co.jp/series/4858311/",
        # YONDEMILL contents are Ohta's on the command line; a work page hands them to `episode()` itself.
        CONTENT_URL,
    ],
)
def test_suitable_rejects_other_urls(url):
    assert not Pie.suitable(url)


def test_is_series_is_true_for_a_work_page_only():
    assert Pie.is_series(WORK_URL)
    assert Pie.is_series("https://comics.pie.co.jp/series/poetic")
    assert not Pie.is_series(STORY_URL)
    assert not Pie.is_series("https://comics.pie.co.jp/comicart/manga/")


# --- parsing --------------------------------------------------------------------------


def test_parse_work_keeps_a_listing_that_already_runs_oldest_first():
    items = [
        ("2024.11.22", "【しろと子猫、そして花】第0回", "https://comics.pie.co.jp/story/noranekoshiro01"),
        ("2024.12.20", "【ノラ猫 しろ】第1回", "https://comics.pie.co.jp/story/noranekoshiro02"),
        ("2025.03.21", "【魚を狙う2匹】第4回", None),
    ]
    work = parse_work(work_html("ノラ猫しろの町めぐり", items), "https://comics.pie.co.jp/series/noranekoshiro/")
    assert work.urls == [
        "https://comics.pie.co.jp/story/noranekoshiro01",
        "https://comics.pie.co.jp/story/noranekoshiro02",
    ]


def test_parse_story_falls_back_on_the_header_for_the_series_title():
    story = parse_story(story_html(breadcrumb=False, prev_url=STORY_URL, next_url=None), NEXT_STORY_URL)
    assert story.series_title == "Poetic Horror"
    assert story.series_url is None
    assert story.prev_url == STORY_URL
    assert story.next_url is None


# --- stories --------------------------------------------------------------------------


def test_episode_reads_a_story(client):
    pie, session = client()
    episode = pie.episode(STORY_URL)

    assert episode.url == STORY_URL
    assert episode.series_title == "Poetic Horror"
    assert episode.episode_title == "【Part 1】Alicia"
    assert [page.url for page in episode.pages] == [f"{UPLOADS}/2025/07/01_A_1.jpg"]
    assert episode.pages[0].extra == {}
    assert episode.next_url == NEXT_STORY_URL
    assert episode.readable
    assert episode.metadata == {
        "kind": "story",
        "title": "【Part 1】Alicia",
        "heading": "Alicia",
        "series_url": "https://comics.pie.co.jp/series/poetic",
        "updated": "26.02.04",
        "prev_url": None,
    }
    json.dumps(episode.metadata)
    # A story needs no work page.
    assert session.calls == [STORY_URL]


def test_episode_remembers_the_original_of_a_scaled_image(client, fake_response):
    scaled = f"{UPLOADS}/2026/08/08_H_1-scaled.jpg"
    pie, _ = client(
        {"/story/henry": fake_response(text=story_html("【Part 8】Henry", "Henry", (scaled,), next_url=None))}
    )
    episode = pie.episode("https://comics.pie.co.jp/story/henry")

    assert episode.pages[0].url == scaled
    assert episode.pages[0].extra == {"original": f"{UPLOADS}/2026/08/08_H_1.jpg"}
    assert episode.next_url is None


def test_story_without_an_image_is_not_readable(client, fake_response):
    pie, _ = client({"/story/alicia": fake_response(text=story_html(images=()))})
    episode = pie.episode(STORY_URL)
    assert episode.pages == ()
    assert not episode.readable
    assert episode.next_url == NEXT_STORY_URL


def test_missing_story_is_not_an_episode(client, fake_response):
    pie, _ = client({"/story/alicia": fake_response(text="404", status_code=HTTPStatus.NOT_FOUND)})
    with pytest.raises(NotAnEpisodePageError, match="404"):
        pie.episode(STORY_URL)


def test_other_http_errors_propagate(client, fake_response):
    pie, _ = client({"/story/alicia": fake_response(text="", status_code=HTTPStatus.SERVICE_UNAVAILABLE)})
    with pytest.raises(HTTPError):
        pie.episode(STORY_URL)


def test_episode_refuses_other_urls(client):
    pie, session = client()
    for url in (WORK_URL, "https://www.yondemill.jp/labels/257", "https://comics.pie.co.jp/comicart/manga/"):
        with pytest.raises(UnsupportedUrlError):
            pie.episode(url)
    assert session.calls == []


# --- YONDEMILL contents ---------------------------------------------------------------


def test_content_is_titled_after_the_work_page_that_lists_it(client):
    pie, session = client()
    pie.series_urls(MANGA_WORK_URL)
    episode = pie.episode(f"{CONTENT_URL}?view=1")

    assert episode.url == CONTENT_URL
    assert episode.series_title == "星旅少年"
    assert episode.episode_title == "【無料】第1話 まどろみの星"
    assert [page.url for page in episode.pages] == [f"{SERVER}/pages/a.jpg/M_H.jpg", f"{SERVER}/pages/b.jpg/M_H.jpg"]
    assert episode.pages[0].extra == {"ctbl": IDENTITY_CTBL, "ptbl": SWAPPED_PTBL}
    # The next free episode, over the ones sold elsewhere.
    assert episode.next_url == NEXT_CONTENT_URL
    assert episode.metadata["kind"] == "yondemill"
    assert episode.metadata["work_url"] == MANGA_WORK_URL
    assert episode.metadata["author"] == "ねこ助"
    assert episode.metadata["content_id"] == "52480"
    assert episode.metadata["binb_id"] == BINB_ID
    json.dumps(episode.metadata)
    # The work page once, then Ohta's dance: the content page, the stub, the reader, the API, content.js.
    assert session.calls == [
        MANGA_WORK_URL,
        CONTENT_URL,
        f"{CONTENT_URL}?view=1",
        READER_URL,
        INFO_URL,
        f"{SERVER}/content.js",
    ]


def test_a_bare_content_finds_its_work_page_through_the_publisher_link(client):
    pie, session = client()
    episode = pie.episode(CONTENT_URL)

    assert episode.series_title == "星旅少年"
    assert episode.episode_title == "【無料】第1話 まどろみの星"
    assert episode.next_url == NEXT_CONTENT_URL
    # The content page, the publisher link (which lands on the work page), then Ohta's dance.
    assert session.calls[:3] == [CONTENT_URL, "https://pie.co.jp/series/4858311/", CONTENT_URL]
    assert session.calls.count("https://pie.co.jp/series/4858311/") == 1

    # The work page is remembered for the episodes it lists.
    pie.episode(CONTENT_URL)
    assert session.calls.count("https://pie.co.jp/series/4858311/") == 1
    assert pie.series_urls(MANGA_WORK_URL) == [CONTENT_URL, NEXT_CONTENT_URL]
    assert MANGA_WORK_URL not in session.calls


def test_a_content_without_a_publisher_link_keeps_ohta_titles(client, fake_response):
    pie, session = client({"/contents/52480": fake_response(text=content_html(back_link=""))})
    episode = pie.episode(CONTENT_URL)

    assert episode.series_title == "星旅少年 第1話"
    assert episode.episode_title == "星旅少年 第1話"
    assert episode.next_url is None
    assert episode.metadata["kind"] == "yondemill"
    assert episode.metadata["work_url"] is None
    assert not any("pie.co.jp/series" in call for call in session.calls)


def test_a_publisher_link_that_does_not_land_on_a_work_page_is_ignored(client, fake_response):
    pie, _ = client({"pie.co.jp/series/4858311/": fake_response(text="", status_code=HTTPStatus.NOT_FOUND)})
    episode = pie.episode(CONTENT_URL)
    assert episode.series_title == "星旅少年 第1話"
    assert episode.next_url is None


def test_content_that_does_not_open_the_reader_is_locked(client, fake_response):
    pie, _ = client({"/contents/52480?view=1": fake_response(text=stub_html(reader_url=""))})
    episode = pie.episode(CONTENT_URL)

    assert episode.series_title == "星旅少年"
    assert episode.episode_title == "【無料】第1話 まどろみの星"
    assert episode.pages == ()
    assert not episode.readable
    assert episode.next_url == NEXT_CONTENT_URL
    assert episode.metadata["locked"] is True


def test_missing_content_is_not_an_episode(client, fake_response):
    pie, _ = client(
        {
            "/contents/52480?view=1": fake_response(text="404", status_code=HTTPStatus.NOT_FOUND),
            "/contents/52480": fake_response(text="404", status_code=HTTPStatus.NOT_FOUND),
        },
    )
    with pytest.raises(NotAnEpisodePageError, match="404"):
        pie.episode(CONTENT_URL)


# --- series ---------------------------------------------------------------------------


def test_series_urls_lists_the_stories_oldest_first(client):
    pie, session = client()
    urls = pie.series_urls(WORK_URL)

    assert urls == [STORY_URL, NEXT_STORY_URL, "https://comics.pie.co.jp/story/henry"]
    assert all(Pie.suitable(url) for url in urls)
    assert session.calls == [WORK_URL]


def test_series_urls_lists_the_free_contents_of_a_manga(client):
    pie, _ = client()
    urls = pie.series_urls(MANGA_WORK_URL)
    assert urls == [CONTENT_URL, NEXT_CONTENT_URL]
    # Not `suitable()`, but `episode()` takes them.
    assert all(pie.episode(url).series_title == "星旅少年" for url in urls[:1])


def test_series_urls_raises_on_an_empty_listing(client, fake_response):
    items = [("2026.01.01", "【非公開】第1話", None), ("2025.12.01", "【単話購入】第0話", "https://amzn.asia/d/x")]
    pie, _ = client({"/series/nothing/": fake_response(text=work_html("まだ", items))})
    with pytest.raises(NotAnEpisodePageError, match="no readable episode"):
        pie.series_urls("https://comics.pie.co.jp/series/nothing/")


def test_series_urls_raises_on_a_missing_work(client, fake_response):
    pie, _ = client({"/series/gone/": fake_response(text="", status_code=HTTPStatus.NOT_FOUND)})
    with pytest.raises(NotAnEpisodePageError, match="404"):
        pie.series_urls("https://comics.pie.co.jp/series/gone/")


def test_series_urls_refuses_a_url_of_the_wrong_shape(client):
    pie, session = client()
    with pytest.raises(UnsupportedUrlError):
        pie.series_urls(STORY_URL)
    assert session.calls == []


# --- images and downloading -----------------------------------------------------------


def test_image_fetches_a_story_image_as_served(client):
    pie, session = client()
    episode = pie.episode(STORY_URL)
    image = pie.image(episode.pages[0], episode)

    assert image.getpixel((0, 0)) == (1, 2, 3)
    assert session.calls[-1] == episode.pages[0].url
    assert session.headers_seen[-1]["Referer"] == STORY_URL


def test_image_prefers_the_original_upload(client, fake_response):
    scaled = f"{UPLOADS}/2026/08/08_H_1-scaled.jpg"
    pie, session = client(
        {
            "/story/henry": fake_response(text=story_html("【Part 8】Henry", "Henry", (scaled,))),
            "08_H_1-scaled.jpg": fake_response(png((9, 9, 9)), content_type="image/jpeg"),
            "08_H_1.jpg": fake_response(png((7, 7, 7), (16, 16)), content_type="image/jpeg"),
        },
    )
    episode = pie.episode("https://comics.pie.co.jp/story/henry")
    image = pie.image(episode.pages[0], episode)

    assert image.size == (16, 16)
    assert image.getpixel((0, 0)) == (7, 7, 7)
    assert session.calls[-1] == f"{UPLOADS}/2026/08/08_H_1.jpg"


@pytest.mark.parametrize(
    "answer",
    [
        {"status_code": HTTPStatus.NOT_FOUND, "text": "not found"},
        {"content_type": "text/html; charset=utf-8", "text": "<html>an error page that says 200</html>"},
    ],
)
def test_image_falls_back_on_the_scaled_copy(client, fake_response, answer):
    scaled = f"{UPLOADS}/2026/08/08_H_1-scaled.jpg"
    pie, session = client(
        {
            "/story/henry": fake_response(text=story_html("【Part 8】Henry", "Henry", (scaled,))),
            "08_H_1-scaled.jpg": fake_response(png((9, 9, 9)), content_type="image/jpeg"),
            "08_H_1.jpg": fake_response(**answer),
        },
    )
    episode = pie.episode("https://comics.pie.co.jp/story/henry")
    image = pie.image(episode.pages[0], episode)

    assert image.getpixel((0, 0)) == (9, 9, 9)
    assert session.calls[-2:] == [f"{UPLOADS}/2026/08/08_H_1.jpg", scaled]


def test_image_puts_a_reader_page_back_together(client):
    pie, session = client()
    episode = pie.episode(CONTENT_URL)
    page = pie.image(episode.pages[0], episode)

    assert page.size == (392, 392)
    assert [page.getpixel((column * 196 + 5, row * 196 + 5)) for row in range(2) for column in range(2)] == COLOURS
    assert session.calls[-1] == episode.pages[0].url
    assert session.headers_seen[-1]["Referer"] == CONTENT_URL


# --- logging in -----------------------------------------------------------------------


# --- the real site --------------------------------------------------------------------

# One free episode per known host: the first story of Poetic Horror, an
# illustration series read on the site itself.
TEST_URLS: dict[str, str] = {
    "comics.pie.co.jp": STORY_URL,
}


@pytest.mark.network
@pytest.mark.parametrize("host", TEST_URLS)
def test_site_download(tmp_path, host):
    result = Downloader(Pie(), tmp_path, only_first=True).download(TEST_URLS[host])
    assert result.status == "saved"
    assert result.episode.series_title == "Poetic Horror"
    assert result.episode.episode_title == "【Part 1】Alicia"
    assert result.episode.next_url == NEXT_STORY_URL
    assert (result.save_dir / "0.jpg").stat().st_size > 0
    assert Image.open(result.save_dir / "0.jpg").size == (1204, 1700)


@pytest.mark.network
def test_work_page_lists_stories_oldest_first():
    pie = Pie()
    assert pie.is_series(WORK_URL)
    urls = pie.series_urls(WORK_URL)
    assert urls[:2] == [STORY_URL, NEXT_STORY_URL]
    assert all(Pie.suitable(url) for url in urls)


@pytest.mark.network
def test_manga_work_page_lists_yondemill_contents_that_episode_reads(tmp_path):
    pie = Pie()
    urls = pie.series_urls(MANGA_WORK_URL)
    assert urls[0] == CONTENT_URL
    result = Downloader(pie, tmp_path, only_first=True).download(urls[0])
    assert result.status == "saved"
    assert result.episode.series_title == "星旅少年"
    assert result.episode.episode_title == "【無料】第1話 まどろみの星"
    assert result.episode.next_url is not None
    assert Image.open(result.save_dir / "0.jpg").size == (1127, 1600)
