from __future__ import annotations

from io import BytesIO

import pytest
from PIL import Image

from getjmanga.downloader import Downloader
from getjmanga.errors import NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractors.daysneo import DaysNeo

WORK_ID = "60093325b4c946ee5c1423cb25070d8d"
EPISODE_ID = "5ef17c434bcdfa03baea7952e2558498"
NEXT_ID = "03f7a1343a345c52e524220da412ae6e"
WORK_URL = f"https://daysneo.com/works/{WORK_ID}.html"
EPISODE_URL = f"https://daysneo.com/works/{WORK_ID}/episode/{EPISODE_ID}.html"
NEXT_URL = f"https://daysneo.com/works/{WORK_ID}/episode/{NEXT_ID}.html"

PAGE_1 = "https://img.daysneo.com/work/p_2608dd27d027aa8569518b26367c3bd201bc.png"
PAGE_2 = "https://img.daysneo.com/work/p_2608a608c3f4ddfbea44cb33f9f49b26871f.png"
PAGE_3 = "https://img.daysneo.com/work/p_260884f43bfe9e727b9ec4852e2c16358c8b.png"

END_BLOCK = f"""
<p><h1 class="f160"><a href="/works/{WORK_ID}.html">のじゃロリお稲荷様、バイクを拾う。</a></h1></p>
<p class="b f140">第1話</p>
<p class="color3">2026年08月20日 公開</p>
<p class="b mt40 f120"><a href="/author/KiwaMirai/">きわみらい</a></p>
<p class="btn type2 f86"><a href="/works/{WORK_ID}.html" class="p5 c">作品詳細ページへ</a></p>
{{next_link}}
<ul><li><a class="l" href="/works/e72e82830824bc5ea83f3a1e9df937be.html">人気のマンガ</a></li></ul>
"""
HEADER = f"""
<section class="carea"><div id="header"><div class="dspFbx"><div class="title">
<h1><a href="/works/{WORK_ID}.html">のじゃロリお稲荷様、バイクを拾う。</a></h1>　｜　第1話
<span>[</span><span id="page_idx">1</span><span>p ／ </span><span id="page_end">3</span><span>p]</span>
</div></div></div></section>
"""


NEXT_LINK = f'<p class="btn type7 mt30"><a href="/works/{WORK_ID}/episode/{NEXT_ID}.html"><span>次の話へ</span></a></p>'


def horizontal_episode(*, next_link=NEXT_LINK):
    """A right-to-left episode: empty `<img id="page_N">` filled in by a script, last page first in the DOM."""
    end_page = f'<div id="end_page" class="w100" style="display: none;">{END_BLOCK.format(next_link=next_link)}</div>'
    return f"""<html><head><script>
$(function() {{
        $('<img src="{PAGE_1}">');
        $('#page_' + 1).attr("src", "{PAGE_1}");
        $('<img src="{PAGE_2}">');
        $('#page_' + 2).attr("src", "{PAGE_2}");
        $('#page_' + 3).attr("src", "{PAGE_3}");
    locateCenter();
}});
</script></head>
<body id="viewer">
<div class="wide" id="fullpage"><div class="viewArea c section">
  <div class="dspFbx page slide">{end_page}
  <div class="r"><img id="page_3" src=""></div></div>
  <div class="dspFbx page slide"><div class="l"><img id="page_2" src=""></div></div>
  <div class="dspFbx page slide active"><div class="l"><img id="page_1" src=""></div></div>
</div></div>
{HEADER}
</body></html>"""


VERTICAL_EPISODE = f"""<html><body id="viewer">
<div class="wide" id="fullpage">
  <div class="section"><img id="view_1" src="{PAGE_1}" style="width: 690px;" class="lazy"></div>
  <div class="section"><img id="view_2" src="{PAGE_2}" style="width: 690px;" class="lazy"></div>
  <div class="section"><img id="view_3" src="{PAGE_3}" style="width: 690px;" class="lazy"></div>
  <div class="section"><div class="c"><div class="w100">{END_BLOCK.format(next_link="")}</div></div></div>
</div>
{HEADER}
</body></html>"""


def work_page(*, episodes=True, editors_only=False):
    """A work page, its `もくじ` linking the episodes or (editors only) merely naming them."""
    label = (
        '<div class="mt5" style="color: red;border: 1px solid red;font-weight: bold;">編集者のみ閲覧可能</div>'
        if editors_only
        else ""
    )

    def item(number, episode_id):
        anchor = f'<a href="/works/{WORK_ID}/episode/{episode_id}.html">' if episodes else ""
        close = "</a>" if episodes else ""
        return f"""<li class="border06 dspFbx top">
          <div class="img"><p>{anchor}<img src="https://img.daysneo.com/work/thumb.png">{close}</p></div>
          <div class="ml20"><dl><dt><strong class="f130">{anchor}第{number}話{close}</strong></dt>
          <dd><p class="date">公開日：2026年08月20日</p></dd></dl></div></li>"""

    return f"""<html><body>
<h2 class="mt60"><span>作品詳細ページ</span></h2>
<a href="/works/{WORK_ID}/episode/{EPISODE_ID}.html"><div class="tile"></div></a>
<p class="f150 b">のじゃロリお稲荷様、バイクを拾う。</p>
<p class="author"><a href="/author/KiwaMirai/">きわみらい</a></p>
{label}
<div class="frame01"><h3><span>もくじ</span></h3><ul class="ul01">
{item(1, EPISODE_ID)}
{item(2, NEXT_ID)}
{item(1, EPISODE_ID)}
</ul></div>
<ul class="dspFbx"><li><a class="l" href="/works/e72e82830824bc5ea83f3a1e9df937be.html">other</a></li></ul>
</body></html>"""


NOT_FOUND_HTML = "<html><body><h1>404 Not Found</h1></body></html>"


@pytest.mark.parametrize(
    ("url", "ok"),
    [
        (EPISODE_URL, True),
        (WORK_URL, True),
        (f"https://daysneo.com/sp/works/{WORK_ID}/episode/{EPISODE_ID}.html", True),
        (f"https://daysneo.com/sp/works/{WORK_ID}.html", True),
        (f"http://daysneo.com/works/{WORK_ID}.html", False),
        (f"https://novel.daysneo.com/works/{WORK_ID}.html", False),
        (f"https://illust.daysneo.com/works/{WORK_ID}.html", False),
        ("https://daysneo.com/works/", False),
        (f"https://daysneo.com/works/{WORK_ID}/episode/{EPISODE_ID}/violation_report.html", False),
        ("https://daysneo.com/ranking/", False),
        (f"https://comic-days.com/works/{WORK_ID}.html", False),
    ],
)
def test_suitable(url, ok):
    assert DaysNeo.suitable(url) is ok


@pytest.mark.parametrize(
    ("url", "series"),
    [
        (WORK_URL, True),
        (f"https://daysneo.com/sp/works/{WORK_ID}.html", True),
        (EPISODE_URL, False),
    ],
)
def test_is_series(url, series):
    assert DaysNeo.is_series(url) is series


def test_episode_reads_a_horizontal_viewer(fake_session, fake_response):
    session = fake_session(
        {"/episode/": fake_response(text=horizontal_episode()), "/works/": fake_response(text=work_page())}
    )
    episode = DaysNeo(session).episode(EPISODE_URL)

    assert episode.series_title == "のじゃロリお稲荷様、バイクを拾う。"
    assert episode.episode_title == "第1話"
    assert [page.url for page in episode.pages] == [PAGE_1, PAGE_2, PAGE_3]
    assert (episode.prev_url, episode.next_url) == (None, NEXT_URL)
    assert episode.metadata["direction"] == "horizontal"
    assert episode.metadata["author"] == "きわみらい"
    assert (episode.writer, episode.publisher) == ("きわみらい", "講談社")
    assert episode.metadata["published"] == "2026年08月20日 公開"
    assert episode.metadata["page_count"] == 3
    # The page, then the work page for the episode before this one, which the page does not link.
    assert session.calls == [EPISODE_URL, WORK_URL]
    assert session.params_seen == [None, None]


def test_episode_reads_a_vertical_viewer(fake_session, fake_response):
    session = fake_session(
        {"/episode/": fake_response(text=VERTICAL_EPISODE), "/works/": fake_response(text=work_page())}
    )
    episode = DaysNeo(session).episode(EPISODE_URL)

    assert [page.url for page in episode.pages] == [PAGE_1, PAGE_2, PAGE_3]
    assert episode.episode_title == "第1話"
    assert episode.next_url is None
    assert episode.metadata["direction"] == "vertical"


def test_episode_has_no_next_on_the_last_one(fake_session, fake_response):
    session = fake_session(
        {"/episode/": fake_response(text=horizontal_episode(next_link="")), "/works/": fake_response(text=work_page())}
    )
    episode = DaysNeo(session).episode(EPISODE_URL)

    assert episode.readable
    assert episode.next_url is None


def test_episode_takes_the_previous_one_off_the_work_page(fake_session, fake_response):
    session = fake_session(
        {"/episode/": fake_response(text=horizontal_episode()), "/works/": fake_response(text=work_page())}
    )
    episode = DaysNeo(session).episode(NEXT_URL)
    assert episode.prev_url == EPISODE_URL
    assert session.calls == [NEXT_URL, WORK_URL]


def test_episode_folds_a_mobile_url_into_the_desktop_one(fake_session, fake_response):
    session = fake_session(
        {"/episode/": fake_response(text=horizontal_episode()), "/works/": fake_response(text=work_page())}
    )
    episode = DaysNeo(session).episode(f"https://daysneo.com/sp/works/{WORK_ID}/episode/{EPISODE_ID}.html")

    assert episode.url == EPISODE_URL
    assert session.calls[0] == EPISODE_URL


def test_episode_falls_back_to_the_header_title(fake_session, fake_response):
    html = horizontal_episode().replace('<p class="b f140">第1話</p>', "").replace('<h1 class="f160">', "<h2>")
    session = fake_session({"/episode/": fake_response(text=html), "/works/": fake_response(text=work_page())})
    episode = DaysNeo(session).episode(EPISODE_URL)

    assert episode.series_title == "のじゃロリお稲荷様、バイクを拾う。"
    assert episode.episode_title == "第1話"


def test_episode_of_an_editors_only_work_is_locked(fake_session, fake_response):
    # The site answers the episode URL with a redirect to the work page.
    session = fake_session(
        {"/episode/": fake_response(text=work_page(episodes=False, editors_only=True), url=WORK_URL)}
    )
    episode = DaysNeo(session).episode(EPISODE_URL)

    assert not episode.readable
    assert episode.url == EPISODE_URL
    assert episode.series_title == "のじゃロリお稲荷様、バイクを拾う。"
    assert episode.next_url is None
    assert episode.metadata["editors_only"] is True


@pytest.mark.parametrize(
    ("html", "landed"), [(work_page(), WORK_URL), (NOT_FOUND_HTML, "https://daysneo.com/404error.html")]
)
def test_episode_redirected_elsewhere_is_not_an_episode(fake_session, fake_response, html, landed):
    # A public work page, or the 404 page.
    session = fake_session({"/episode/": fake_response(text=html, url=landed)})
    with pytest.raises(NotAnEpisodePageError, match="no viewer"):
        DaysNeo(session).episode(EPISODE_URL)


def test_episode_rejects_a_work_url(fake_session):
    with pytest.raises(NotAnEpisodePageError, match="not an episode page"):
        DaysNeo(fake_session({})).episode(WORK_URL)


def test_series_urls_lists_the_episodes_oldest_first_without_duplicates(fake_session, fake_response):
    session = fake_session({"/works/": fake_response(text=work_page())})
    urls = DaysNeo(session).series_urls(f"https://daysneo.com/sp/works/{WORK_ID}.html")

    assert urls == [EPISODE_URL, NEXT_URL]
    assert all(DaysNeo.suitable(url) and not DaysNeo.is_series(url) for url in urls)
    assert session.calls == [WORK_URL]


def test_series_urls_raises_on_an_editors_only_work(fake_session, fake_response):
    session = fake_session({"/works/": fake_response(text=work_page(episodes=False, editors_only=True))})
    with pytest.raises(NotAnEpisodePageError, match="editors only"):
        DaysNeo(session).series_urls(WORK_URL)


def test_series_urls_raises_on_an_empty_listing(fake_session, fake_response):
    session = fake_session({"/works/": fake_response(text=work_page(episodes=False))})
    with pytest.raises(NotAnEpisodePageError, match="lists no episode"):
        DaysNeo(session).series_urls(WORK_URL)


def test_series_urls_raises_when_the_work_is_missing(fake_session, fake_response):
    session = fake_session({"/works/": fake_response(text=NOT_FOUND_HTML, url="https://daysneo.com/404error.html")})
    with pytest.raises(NotAnEpisodePageError, match="no work page"):
        DaysNeo(session).series_urls(WORK_URL)


def test_series_urls_rejects_an_episode_url(fake_session):
    with pytest.raises(UnsupportedUrlError):
        DaysNeo(fake_session({})).series_urls(EPISODE_URL)


def test_download_writes_the_pages(tmp_path, fake_session, fake_response):
    buffer = BytesIO()
    Image.new("RGBA", (12, 16), (200, 30, 30, 255)).save(buffer, format="PNG")
    session = fake_session(
        {
            "/episode/": fake_response(text=horizontal_episode()),
            "img.daysneo.com/work/": fake_response(buffer.getvalue(), content_type="image/png"),
            "/works/": fake_response(text=work_page()),
        }
    )
    result = Downloader(DaysNeo(session), tmp_path).download(EPISODE_URL)

    assert result.status == "saved"
    assert result.save_dir == tmp_path / "daysneo.com" / "のじゃロリお稲荷様、バイクを拾う。" / "第1話"
    assert sorted(path.name for path in result.save_dir.iterdir()) == ["0.jpg", "1.jpg", "2.jpg"]
    assert Image.open(result.save_dir / "0.jpg").size == (12, 16)
    assert session.headers_seen[-1]["Referer"] == EPISODE_URL


# --- the real site --------------------------------------------------------------------

# One episode per known host, free to read without an account.
TEST_URLS: dict[str, str] = {
    "daysneo.com": "https://daysneo.com/works/0ccce3a53bfa190c486f73f0daa62be3/episode/82da6f9149d5926de6a9835c4047cd1a.html",
}


@pytest.mark.network
@pytest.mark.geoblocked
@pytest.mark.parametrize("host", TEST_URLS)
def test_site_download(tmp_path, host):
    result = Downloader(DaysNeo(), tmp_path, only_first=True).download(TEST_URLS[host])
    assert result.status == "saved"
    assert (result.save_dir / "0.jpg").stat().st_size > 0


@pytest.mark.network
@pytest.mark.geoblocked
def test_site_horizontal_episode_lists_its_pages_and_the_next_one():
    episode = DaysNeo().episode(EPISODE_URL)
    assert episode.series_title == "のじゃロリお稲荷様、バイクを拾う。"
    assert episode.episode_title == "第1話"
    assert len(episode.pages) == 12
    assert episode.next_url == NEXT_URL


@pytest.mark.network
@pytest.mark.geoblocked
def test_site_work_page_lists_episodes():
    urls = DaysNeo().series_urls(WORK_URL)
    assert urls[0] == EPISODE_URL
    assert urls[1] == NEXT_URL
    assert all(DaysNeo.suitable(url) for url in urls)
