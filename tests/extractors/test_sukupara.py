from __future__ import annotations

from io import BytesIO

import pytest
from PIL import Image

from getjmanga.downloader import Downloader
from getjmanga.errors import NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractors import sukupara
from getjmanga.extractors.sukupara import Sukupara, episode_url, series_url

MANGA = "180"
SERIES_URL = series_url(MANGA)
FIRST_URL = episode_url(MANGA, "2160")
SIXTH_URL = episode_url(MANGA, "2173")
NEWEST_URL = episode_url(MANGA, "2174")
GONE_URL = episode_url(MANGA, "2172")


def button(element_id, href=None, image="btn", alt=""):
    """One `<li>` of the button bar: a link when `href` is given, a greyed-out image otherwise."""
    img = f'<img src="/plus/images/{image}.gif" alt="{alt}" />'
    if href is None:
        return f'<li id="{element_id}">{img}</li>'
    return f'<li id="{element_id}"><a href="{href}">{img}</a></li>'


def page_href(story_id, page_no):
    return f"mag_detail.php?manga_id={MANGA}&story_id={story_id}&page_no={page_no}"


def page_html(
    story_id="2160",
    page_no=1,
    *,
    series="マダムはあきらめない",
    title="第1話-老後資金の増やし方を知りたい！",
    image=True,
    next_page=True,
    next_story=SIXTH_URL,
    prev_story=None,
):
    """A `mag_detail.php` page cut down to what `episode()` reads: one image and the buttons."""
    img = f'<img src="/plus/manga/{MANGA}/{story_id}/{page_no}.jpg" alt="" title="" view_id="" />' if image else ""
    if page_no > 1:
        buttons = button("prev-page-btn", page_href(story_id, page_no - 1), alt="前のページへ")
    elif prev_story:
        buttons = button("before-story-btn", prev_story.split("/plus/")[1], alt="前の話へ")
    else:
        buttons = button("prev-page-off-btn", alt="前のページへ")
    buttons += f'<li><a href="mag_top.php?manga_id={MANGA}"><img src="/plus/images/btn-storytop.gif" /></a></li>'
    if next_page:
        buttons += button("next-page-btn", page_href(story_id, page_no + 1), alt="次のページへ")
    elif next_story:
        buttons += button("after-story-btn", next_story.split("/plus/")[1], alt="次の話へ")
    else:
        buttons += button("after-story-off-btn", alt="次の話へ")
    return f"""<?xml version="1.0" encoding="utf-8" ?>
<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="ja" lang="ja" dir="ltr"><head>
<title>「{series}」{title}-{page_no}｜青沼貴子｜すくすくパラダイスぷらす｜すくパラ倶楽部</title>
<meta property="og:title" content="{series}｜すくパラぷらす" />
</head><body>
<div id="contents_plus"><form action="/plus/mag_detail.php" id="f1" method="post" name="f1"><div id="webmag">
<div class="webmag-box-contents">
<a name="page-top" id="page-top"></a>
<h3 id="story_title">「{series}」{title}-{page_no}</h3>
<div class="menulist-wrap clearfix"><ul class="menulist clearfix">
<li class="first"><a href="index.php">すくパラぷらすTOP</a></li>
<li><a href="mag_top.php?manga_id={MANGA}">作品紹介</a></li>
<li><a href="mag_detail.php?manga_id={MANGA}&story_id={story_id}">この話TOP</a></li>
</ul></div>
<div id="box-main">
<div style="text-align: center;" class="magarea">{img}</div>
<div class="clearfix"><p class="page"><span id="curr-page-no">{page_no}</span>&nbsp;ページ</p></div>
<div class="btnarea"><ul class="clearfix">{buttons}</ul></div>
</div></div>
<input type="hidden" name="end-flg" value="1" />
<div id="box-arrivals"><ul id="box-arrivals-main" class="lt">
<li><a href="mag_detail.php?manga_id=186&story_id=2182">「認知症の夫と暮らす」第1話-初恋:08/20</a></li>
</ul></div>
</div></form></div>
</body></html>"""


def story_pages(story_id, count, **kwargs):
    """The reading pages of one story, in order, the last one without a next-page button."""
    return [page_html(story_id, page_no, next_page=page_no < count, **kwargs) for page_no in range(1, count + 1)]


def series_html(stories, *, first=None, newest=None):
    """A `mag_top.php` page: the first-episode button, the newest story and the back numbers."""
    header = ""
    if first:
        header = (
            f'<p><a href="mag_detail.php?manga_id={MANGA}&story_id={first}"><img alt="第1回はコチラから" /></a></p>'
        )
    latest = ""
    if newest:
        href = f"mag_detail.php?manga_id={MANGA}&story_id={newest}"
        latest = f"""<div class="newest-story">
<p class="flo_r pad-all5"><a href="{href}"><img src="/plus/images/btn-read.gif" alt="" /></a></p>
<p class="newest-story-tit"><a href="{href}">「マダムはあきらめない」&nbsp;ひょっこりおばさん</a></p>
</div>"""
    backnumber = "".join(
        f'<li><a href="mag_detail.php?manga_id={MANGA}&amp;story_id={story_id}">第{index}話　…</a></li>'
        for index, story_id in stories
    )
    return f"""<html><head><title>マダムはあきらめない｜青沼貴子｜すくすくパラダイスぷらす｜すくパラ倶楽部</title>
<meta property="og:title" content="マダムはあきらめない｜すくパラぷらす" /></head><body>
<div id="contents_plus"><div id="webmag">
<h2 class="mag-btm5"><img src="/plus/manga/{MANGA}/title.jpg" alt="" title="" width="630" /></h2>
<div id="series-list-header" class="clearfix"><h3>マダムはあきらめない&nbsp;作品情報</h3>{header}</div>
<div id="series-list-intro" class="clearfix">
<p class="phtarea"><a href="mag_detail.php?manga_id={MANGA}&amp;story_id={newest or ""}" id="img-link-1">
<img src="/plus/manga/{MANGA}/bnr.jpg" /></a></p>
{latest}
</div>
<div id="backnumber" class="clearfix"><dl><dt>バックナンバー</dt><dd><ul>{backnumber}</ul></dd></dl></div>
<div class="webmg-list"><ul class="clearfix">
<li class="first"><a href="mag_top.php?manga_id=178">松本ぷりっつのダンナ50歳、楽しく健康イケオジ部！</a></li>
</ul></div>
</div></div></body></html>"""


ERROR_HTML = """<html><head><title>すくパラ倶楽部</title></head><body>
<div id="center-popup"><div id="popup"><form action="/index.php" id="f1" method="post" name="f1">
<div class="g-border fff"><h2 class="mag-btm10">エラー</h2>
<img src="/images/ill-logout.gif" alt="" title="" />
<p class="fnt-140 mag-btm10">ご指定の話は削除されたか、URLが間違っているためアクセス出来ません。<br /></p>
</div></form></div></div></body></html>"""


def jpeg_bytes(size=(8, 8), color=(10, 20, 30)):
    buffer = BytesIO()
    Image.new("RGB", size, color).save(buffer, format="JPEG")
    return buffer.getvalue()


@pytest.fixture
def client(fake_session, fake_response):
    """A `Sukupara` on a scripted session; `extra` routes take precedence."""

    def build(extra=None):
        pages = story_pages("2160", 3)
        routes = {
            **(extra or {}),
            # The numbered pages must be matched before the bare story URL.
            **{f"story_id=2160&page_no={number}": fake_response(text=html) for number, html in enumerate(pages, 1)},
            "story_id=2160": fake_response(text=pages[0]),
            "/plus/manga/": fake_response(jpeg_bytes(), content_type="image/jpeg"),
        }
        session = fake_session(routes)
        return Sukupara(session), session

    return build


# --- URLs ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        FIRST_URL,
        f"{FIRST_URL}&page_no=3",
        "https://sukupara.jp/plus/mag_detail.php?story_id=4&manga_id=2",
        SERIES_URL,
        "https://sukupara.jp/plus/mag_top.php?manga_id=2",
    ],
)
def test_suitable_accepts_episode_and_series_urls(url):
    assert Sukupara.suitable(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://sukupara.jp/plus/mag_detail.php?manga_id=180&story_id=2160",
        "https://www.sukupara.jp/plus/mag_detail.php?manga_id=180&story_id=2160",
        "https://news.sukupara.jp/plus/mag_detail.php?manga_id=180&story_id=2160",
        "https://sukupara.jp/",
        "https://sukupara.jp/plus/",
        "https://sukupara.jp/plus/index.php",
        "https://sukupara.jp/plus/mag_detail.php",
        "https://sukupara.jp/plus/mag_detail.php?manga_id=180",
        "https://sukupara.jp/plus/mag_detail.php?story_id=2160",
        "https://sukupara.jp/plus/mag_detail.php?manga_id=abc&story_id=2160",
        "https://sukupara.jp/plus/mag_top.php",
        "https://sukupara.jp/plus/mag_top.php?manga_id=",
        "https://sukupara.jp/mag_detail.php?manga_id=180&story_id=2160",
        "https://sukupara.jp/present_list.php",
        "https://sukupara.jp/plus/manga/180/2160/1.jpg",
    ],
)
def test_suitable_rejects_other_urls(url):
    assert not Sukupara.suitable(url)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (SERIES_URL, True),
        ("https://sukupara.jp/plus/mag_top.php?manga_id=2&x=1", True),
        (FIRST_URL, False),
        ("https://sukupara.jp/plus/mag_top.php", False),
        ("https://sukupara.jp/plus/", False),
    ],
)
def test_is_series(url, expected):
    assert Sukupara.is_series(url) is expected


# --- episodes ---------------------------------------------------------------------------


def test_episode_walks_the_pages_and_reads_the_titles(client):
    sukupara, session = client()
    episode = sukupara.episode(FIRST_URL)

    assert episode.url == FIRST_URL
    assert episode.series_title == "マダムはあきらめない"
    assert episode.episode_title == "第1話-老後資金の増やし方を知りたい！"
    assert [page.url for page in episode.pages] == [
        f"https://sukupara.jp/plus/manga/{MANGA}/2160/{number}.jpg" for number in (1, 2, 3)
    ]
    assert all(page.extra == {} for page in episode.pages)
    assert episode.next_url == SIXTH_URL
    assert episode.metadata == {
        "manga_id": MANGA,
        "story_id": "2160",
        "heading": "「マダムはあきらめない」第1話-老後資金の増やし方を知りたい！-1",
        "series_url": SERIES_URL,
        "prev_url": None,
        "page_count": 3,
        "images": [f"https://sukupara.jp/plus/manga/{MANGA}/2160/{number}.jpg" for number in (1, 2, 3)],
    }
    # Page 1 is asked for without a page number, the rest by the next-page links.
    assert session.calls == [FIRST_URL, f"{FIRST_URL}&page_no=2", f"{FIRST_URL}&page_no=3"]
    assert session.params_seen == [None, None, None]
    assert "User-Agent" in session.headers_seen[0]


def test_episode_ignores_the_page_number_in_the_url(client):
    sukupara, session = client()
    episode = sukupara.episode(f"{FIRST_URL}&page_no=3")

    assert episode.url == FIRST_URL
    assert len(episode.pages) == 3
    assert session.calls[0] == FIRST_URL


def test_episode_at_the_end_of_a_series_has_no_next_but_a_previous(client, fake_response):
    pages = story_pages("2174", 2, title="第7話-ひょっこりおばさん", next_story=None, prev_story=SIXTH_URL)
    sukupara, _ = client(
        {
            "story_id=2174&page_no=2": fake_response(text=pages[1]),
            "story_id=2174": fake_response(text=pages[0]),
        },
    )
    episode = sukupara.episode(NEWEST_URL)

    assert episode.episode_title == "第7話-ひょっこりおばさん"
    assert len(episode.pages) == 2
    assert episode.next_url is None
    assert episode.metadata["prev_url"] == SIXTH_URL


def test_episode_takes_a_heading_without_a_subtitle(client, fake_response):
    sukupara, _ = client(
        {"manga_id=2&story_id=4": fake_response(text=page_html("4", title="第1話", next_page=False, next_story=None))},
    )
    episode = sukupara.episode(episode_url("2", "4"))

    assert episode.episode_title == "第1話"
    assert episode.series_title == "マダムはあきらめない"


def test_episode_falls_back_to_the_heading_for_the_series_title(client, fake_response):
    html = page_html("4", next_page=False, next_story=None).replace('<meta property="og:title"', "<meta")
    sukupara, _ = client({"manga_id=2&story_id=4": fake_response(text=html)})
    episode = sukupara.episode(episode_url("2", "4"))

    assert episode.series_title == "マダムはあきらめない"
    assert episode.episode_title == "第1話-老後資金の増やし方を知りたい！"


def test_episode_without_an_image_has_no_pages_but_still_a_next(client, fake_response):
    sukupara, _ = client({"story_id=2173": fake_response(text=page_html("2173", image=False, next_page=False))})
    episode = sukupara.episode(SIXTH_URL)

    assert episode.pages == ()
    assert not episode.readable
    assert episode.next_url == SIXTH_URL


def test_episode_stops_walking_when_a_next_page_link_loops(client, fake_response):
    looping = page_html("2173", 1).replace("page_no=2", "page_no=1")
    sukupara, session = client({"story_id=2173": fake_response(text=looping)})
    episode = sukupara.episode(SIXTH_URL)

    assert len(episode.pages) == 1
    assert session.calls == [SIXTH_URL]


def test_episode_gives_up_on_an_endless_chain_of_pages(fake_session, fake_response, monkeypatch):
    monkeypatch.setattr(sukupara, "MAX_PAGES", 5)

    class EndlessSession(fake_session):
        """A site whose every page links to one more."""

        def _route(self, url):
            page_no = int(url.rsplit("page_no=", 1)[1]) if "page_no=" in url else 1
            return fake_response(text=page_html("2173", page_no), url=url)

    session = EndlessSession({})
    episode = Sukupara(session).episode(SIXTH_URL)

    assert len(episode.pages) == 5
    assert len(session.calls) == 5


def test_episode_rejects_a_taken_down_story(client, fake_response):
    sukupara, _ = client({"story_id=2172": fake_response(text=ERROR_HTML)})
    with pytest.raises(NotAnEpisodePageError, match="no story"):
        sukupara.episode(GONE_URL)


def test_episode_rejects_a_work_page(client, fake_response):
    sukupara, _ = client({"mag_top.php": fake_response(text=series_html([]))})
    with pytest.raises(NotAnEpisodePageError, match="not a reading page"):
        sukupara.episode(SERIES_URL)


# --- series -----------------------------------------------------------------------------


def test_series_urls_lists_the_stories_oldest_first_without_duplicates(client, fake_response):
    html = series_html([(6, "2173"), (1, "2160")], first="2160", newest="2174")
    sukupara, session = client({"mag_top.php": fake_response(text=html)})

    assert sukupara.series_urls(SERIES_URL) == [FIRST_URL, SIXTH_URL, NEWEST_URL]
    assert session.calls == [SERIES_URL]


def test_series_urls_ignores_stories_of_other_works(client, fake_response):
    html = series_html([(1, "2160")]).replace(
        "</body>",
        '<a href="mag_detail.php?manga_id=186&story_id=2182">other</a></body>',
    )
    sukupara, _ = client({"mag_top.php": fake_response(text=html)})

    assert sukupara.series_urls(f"{SERIES_URL}&from=index") == [FIRST_URL]


def test_series_urls_raises_on_an_empty_listing(client, fake_response):
    sukupara, _ = client({"mag_top.php": fake_response(text=series_html([]))})
    with pytest.raises(NotAnEpisodePageError, match="lists no episode"):
        sukupara.series_urls(SERIES_URL)


def test_series_urls_raises_on_an_unknown_work(client, fake_response):
    sukupara, _ = client({"mag_top.php": fake_response(text=ERROR_HTML)})
    with pytest.raises(NotAnEpisodePageError, match="lists no episode"):
        sukupara.series_urls(series_url("99999"))


def test_series_urls_rejects_an_episode_url(client):
    sukupara, _ = client()
    with pytest.raises(UnsupportedUrlError, match="not a work page"):
        sukupara.series_urls(FIRST_URL)


# --- downloading ------------------------------------------------------------------------


def test_download_writes_the_pages_as_served(client, tmp_path):
    sukupara, session = client()
    result = Downloader(sukupara, tmp_path).download(FIRST_URL)

    assert result.status == "saved"
    assert result.save_dir == tmp_path / "マダムはあきらめない" / "第1話-老後資金の増やし方を知りたい！"
    assert sorted(path.name for path in result.save_dir.iterdir()) == ["0.jpg", "1.jpg", "2.jpg"]
    with Image.open(result.save_dir / "0.jpg") as saved:
        assert saved.size == (8, 8)
        assert saved.getpixel((4, 4)) == pytest.approx((10, 20, 30), abs=8)
    assert session.calls[3:] == [f"https://sukupara.jp/plus/manga/{MANGA}/2160/{number}.jpg" for number in (1, 2, 3)]
    assert session.headers_seen[-1]["Referer"] == FIRST_URL


# --- the real site ----------------------------------------------------------------------

# One episode per known host, free to read without an account.
TEST_URLS: dict[str, str] = {
    "sukupara.jp": "https://sukupara.jp/plus/mag_detail.php?manga_id=2&story_id=4",
}


@pytest.mark.network
@pytest.mark.parametrize("host", TEST_URLS)
def test_site_download(tmp_path, host):
    result = Downloader(Sukupara(), tmp_path, only_first=True).download(TEST_URLS[host])
    assert result.status == "saved"
    assert (result.save_dir / "0.jpg").stat().st_size > 0


@pytest.mark.network
def test_work_page_lists_episodes():
    urls = Sukupara().series_urls("https://sukupara.jp/plus/mag_top.php?manga_id=2")
    assert urls[0] == TEST_URLS["sukupara.jp"]
    assert all(Sukupara.suitable(url) for url in urls)


@pytest.mark.network
def test_taken_down_story_is_not_an_episode():
    with pytest.raises(NotAnEpisodePageError, match="no story"):
        Sukupara().episode("https://sukupara.jp/plus/mag_detail.php?manga_id=180&story_id=2172")
