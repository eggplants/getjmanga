from __future__ import annotations

import json
from io import BytesIO

import pytest
from PIL import Image

from getjmanga.downloader import Downloader
from getjmanga.extractors.common import LoginError, NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractors.fullpercent import FullPercent, parse_viewer, parse_work, split_heading

WORK_URL = "https://fullpercent.net/comic/detail/2"
EPISODE_URL = "https://fullpercent.net/comic/view/2/2"
NEXT_URL = "https://fullpercent.net/comic/view/2/11809"

PAGE_1 = "https://fullpercent.net/img/episode/2/2_aaaaaa.jpg"
PAGE_2 = "https://fullpercent.net/img/episode/2/2_bbbbbb.jpg"
PAGE_3 = "https://fullpercent.net/img/episode/2/2_cccccc.jpg"

# The work title holds a full-width space itself, like the viewer's separator.
WORK_TITLE = "ぱんでみっく　ぞんびぃず！"


def viewer_html(heading, images, *, pagedata=True):
    data = "\n".join(f'<img data-src="{src}">' for src in images)
    block = f'<div id="pagedata">\n{data}\n</div>' if pagedata else ""
    return f"""<!DOCTYPE html><html lang="ja"><head><meta charset="UTF-8" />
<title>{heading}　｜　comic viewer</title>
<script src="//fullpercent.net/js/viewer.js?20200721"></script></head>
<body>
<div id="header"><p><span id="proceed"></span>
<div style="white-space:nowrap;">{heading}</div>
<input type="button" value="全画面" id="fullscreen" /><select id="page_select"></select></p></div>
<div id="content"><div id="contentin"><div id="single" class="page"><img class="single"></div></div></div>
<div id="viewerresource"><img src="//fullpercent.net/img/viewer/cover.png"></div>
{block}
</body></html>"""


EPISODE_HTML = viewer_html(
    f"{WORK_TITLE}　第１話",
    ["//fullpercent.net/img/episode/2/2_aaaaaa.jpg", "//fullpercent.net/img/episode/2/2_bbbbbb.jpg", PAGE_3],
)
EMPTY_HTML = viewer_html(f"{WORK_TITLE}　第１話", [])
ERROR_HTML = """<!DOCTYPE html><html lang="ja"><head><meta charset="UTF-8" /><title>エラー</title></head>
<body style="background:none;"><div id="main_content"><h2>閲覧できません</h2></div></body></html>"""


def entry(work, episode, title, *, image=True):
    picture = (
        f'<img src="//fullpercent.net/img/episode/{work}_{episode}.jpg" alt="{title}" title="{title}">' if image else ""
    )
    return f"""<li>
<div class="manga_list_view">
<a href="#" title="マンガを読む" onclick="return openViewer({work},{episode});">{picture}</a></div>
<div title="{title}" rel="tooltip"><div class="comic_manga_list_title"><h4><a>{title}</a></h4></div>
<div class="manga_list_text02"><p>...</p></div></div>
</li>"""


def work_html(entries, *, title=WORK_TITLE, epilist=True):
    lists = "".join(f'<ul class="comic_manga_list_box">{block}</ul>' for block in entries)
    listing = f'<div id="epilist" class="left_title"><h2>エピソード一覧</h2></div>{lists}' if epilist else ""
    return f"""<!doctype html><head><meta charset="utf-8"><title>漫画詳細</title>
<meta property="og:title" content="{title}">
<meta property="og:url" content="{WORK_URL}"></head>
<body><div id="main_content"><div class="content_left">
<div class="left_title_t">
<h2><span class="comic_title_icon01"><img src="/images/x.png">漫画・ノベル</span>{title}</h2></div>
{listing}
<section class="btn03 ac_button01"><p class="btn2">全部表示する▼</p></section>
</div>
<div class="content_right"><div class="right_user_box sp_none">
<h3><a href="//fullpercent.net/artist/detail/1">作者名</a></h3></div></div>
</div>
<script>function openViewer( comic_id, episode_id ) {{
window.open('//fullpercent.net/comic/view/' + comic_id + '/' + episode_id); }}</script>
</body></html>"""


# Oldest first, as `?order=episode_id_asc` serves it, split over the two lists.
WORK_HTML = work_html(
    [
        entry(2, 2, "第１話") + entry(2, 11809, "第２話"),
        entry(2, 11809, "第２話") + entry(2, 12000, "第３話") + entry(99, 5, "別の作品"),
    ],
)


@pytest.fixture
def client(fake_session):
    def make(routes):
        session = fake_session(routes)
        return FullPercent(session), session

    return make


# --- URLs -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (EPISODE_URL, True),
        ("https://fullpercent.net/comic/view/154/468/", True),
        (WORK_URL, True),
        ("https://fullpercent.net/comic/detail/154?order=episode_id_asc&anchor=epilist", True),
        ("http://fullpercent.net/comic/view/2/2", False),
        ("https://www.fullpercent.net/comic/view/2/2", False),
        ("https://fullpercent.net/comic/plist/manga", False),
        ("https://fullpercent.net/comic/view/2", False),
        ("https://fullpercent.net/artist/detail/117", False),
        ("https://fullpercent.net/", False),
    ],
)
def test_suitable(url, expected):
    assert FullPercent.suitable(url) is expected


@pytest.mark.parametrize(
    ("url", "expected"),
    [(WORK_URL, True), (f"{WORK_URL}?order=episode_id_asc", True), (EPISODE_URL, False)],
)
def test_is_series(url, expected):
    assert FullPercent.is_series(url) is expected


# --- parsing --------------------------------------------------------------------------


def test_parse_viewer_falls_back_to_the_title_without_a_header():
    html = EPISODE_HTML.replace(f'<div style="white-space:nowrap;">{WORK_TITLE}　第１話</div>', "")
    viewer = parse_viewer(html, EPISODE_URL)
    assert viewer is not None
    assert viewer.heading == f"{WORK_TITLE}　第１話"


def test_parse_work_takes_the_title_off_the_heading_without_og_title():
    html = WORK_HTML.replace(f'<meta property="og:title" content="{WORK_TITLE}">', "")
    work = parse_work(html, WORK_URL)
    assert work is not None
    assert work.title == WORK_TITLE


def test_parse_work_takes_the_episode_title_off_the_entry_without_a_thumbnail():
    work = parse_work(work_html([entry(2, 2, "第１話", image=False)]), WORK_URL)
    assert work is not None
    assert [(episode.id, episode.title, episode.thumbnail) for episode in work.episodes] == [("2", "第１話", "")]


@pytest.mark.parametrize(
    ("heading", "work_title", "expected"),
    [
        (f"{WORK_TITLE}　第１話", WORK_TITLE, (WORK_TITLE, "第１話")),
        (f"{WORK_TITLE}　第１話", "", ("ぱんでみっく", "ぞんびぃず！　第１話")),
        ("ΚΥΡΙΕ—キリエ　第１話", "", ("ΚΥΡΙΕ—キリエ", "第１話")),
        ("ΚΥΡΙΕ—キリエ　第１話", "別の作品", ("ΚΥΡΙΕ—キリエ", "第１話")),
        ("ΚΥΡΙΕ—キリエ", "ΚΥΡΙΕ—キリエ", ("ΚΥΡΙΕ—キリエ", "")),
        ("なにか", "", ("なにか", "")),
    ],
)
def test_split_heading(heading, work_title, expected):
    assert split_heading(heading, work_title) == expected


# --- episode --------------------------------------------------------------------------


def test_episode_reads_the_titles_the_pages_and_the_next_episode(client, fake_response):
    fullpercent, session = client(
        {"/comic/view/2/2": fake_response(text=EPISODE_HTML), "/comic/detail/2": fake_response(text=WORK_HTML)},
    )
    episode = fullpercent.episode(EPISODE_URL)

    assert episode.url == EPISODE_URL
    assert episode.series_title == WORK_TITLE
    assert episode.episode_title == "第１話"
    assert [page.url for page in episode.pages] == [PAGE_1, PAGE_2, PAGE_3]
    assert episode.next_url == NEXT_URL
    assert episode.metadata["author"] == "作者名"
    assert episode.metadata["index"] == 0
    assert episode.metadata["episode_count"] == 3
    assert episode.metadata["page_count"] == 3
    assert episode.metadata["heading"] == f"{WORK_TITLE}　第１話"
    json.dumps(episode.metadata)
    # The work page is asked for oldest first, once per work.
    assert session.calls == [EPISODE_URL, WORK_URL]
    assert session.params_seen == [None, {"order": "episode_id_asc"}]


def test_episode_reads_the_work_page_once_per_work(client, fake_response):
    fullpercent, session = client(
        {
            "/comic/view/2/2": fake_response(text=EPISODE_HTML),
            "/comic/view/2/11809": fake_response(text=viewer_html(f"{WORK_TITLE}　第２話", [PAGE_1])),
            "/comic/view/2/12000": fake_response(text=viewer_html(f"{WORK_TITLE}　第３話", [PAGE_1])),
            "/comic/detail/2": fake_response(text=WORK_HTML),
        },
    )
    first = fullpercent.episode(EPISODE_URL)
    second = fullpercent.episode(first.next_url)
    third = fullpercent.episode(second.next_url)

    assert (second.episode_title, second.next_url) == ("第２話", "https://fullpercent.net/comic/view/2/12000")
    assert (third.episode_title, third.next_url) == ("第３話", None)
    assert session.calls.count(WORK_URL) == 1


def test_episode_normalises_a_trailing_slash(client, fake_response):
    fullpercent, session = client(
        {"/comic/view/2/2": fake_response(text=EPISODE_HTML), "/comic/detail/2": fake_response(text=WORK_HTML)},
    )
    episode = fullpercent.episode(f"{EPISODE_URL}/")
    assert episode.url == EPISODE_URL
    assert session.calls[0] == EPISODE_URL


def test_episode_splits_the_heading_itself_when_the_work_page_is_gone(client, fake_response):
    fullpercent, _ = client(
        {
            "/comic/view/2/2": fake_response(text=viewer_html("ΚΥΡΙΕ—キリエ　第１話", [PAGE_1])),
            "/comic/detail/2": fake_response(text=work_html([], epilist=False), url="https://fullpercent.net/404.html"),
        },
    )
    episode = fullpercent.episode(EPISODE_URL)
    assert (episode.series_title, episode.episode_title) == ("ΚΥΡΙΕ—キリエ", "第１話")
    assert episode.next_url is None
    assert episode.metadata["index"] is None
    assert episode.metadata["episode_count"] is None


def test_episode_not_on_the_work_page_has_no_next(client, fake_response):
    fullpercent, _ = client(
        {
            "/comic/view/2/777": fake_response(text=viewer_html(f"{WORK_TITLE}　番外編", [PAGE_1])),
            "/comic/detail/2": fake_response(text=WORK_HTML),
        },
    )
    episode = fullpercent.episode("https://fullpercent.net/comic/view/2/777")
    assert (episode.series_title, episode.episode_title) == (WORK_TITLE, "番外編")
    assert episode.next_url is None
    assert episode.metadata["index"] is None


def test_episode_without_pages_is_locked_not_gone(client, fake_response):
    fullpercent, _ = client(
        {"/comic/view/2/2": fake_response(text=EMPTY_HTML), "/comic/detail/2": fake_response(text=WORK_HTML)},
    )
    episode = fullpercent.episode(EPISODE_URL)
    assert episode.pages == ()
    assert not episode.readable
    assert episode.next_url == NEXT_URL


def test_episode_raises_on_the_error_page(client, fake_response):
    fullpercent, session = client({"/comic/view/": fake_response(text=ERROR_HTML)})
    with pytest.raises(NotAnEpisodePageError, match="no viewer"):
        fullpercent.episode("https://fullpercent.net/comic/view/154/9999999")
    assert len(session.calls) == 1


def test_episode_raises_on_a_work_url(client):
    fullpercent, session = client({})
    with pytest.raises(NotAnEpisodePageError, match="not an episode page"):
        fullpercent.episode(WORK_URL)
    assert session.calls == []


# --- series ---------------------------------------------------------------------------


def test_series_urls_lists_oldest_first_and_deduplicates(client, fake_response):
    fullpercent, session = client({"/comic/detail/2": fake_response(text=WORK_HTML)})
    urls = fullpercent.series_urls(f"{WORK_URL}?anchor=epilist")
    assert urls == [EPISODE_URL, NEXT_URL, "https://fullpercent.net/comic/view/2/12000"]
    assert all(FullPercent.suitable(url) for url in urls)
    assert session.calls == [WORK_URL]
    assert session.params_seen == [{"order": "episode_id_asc"}]


def test_series_urls_raises_on_an_empty_listing(client, fake_response):
    fullpercent, _ = client({"/comic/detail/2": fake_response(text=work_html([]))})
    with pytest.raises(NotAnEpisodePageError, match="lists no episode"):
        fullpercent.series_urls(WORK_URL)


def test_series_urls_raises_on_a_missing_work(client, fake_response):
    fullpercent, _ = client(
        {"/comic/detail/": fake_response(text=work_html([], epilist=False), url="https://fullpercent.net/404.html")},
    )
    with pytest.raises(NotAnEpisodePageError, match="no work page"):
        fullpercent.series_urls("https://fullpercent.net/comic/detail/9999999")


def test_series_urls_raises_on_an_episode_url(client):
    fullpercent, _ = client({})
    with pytest.raises(UnsupportedUrlError):
        fullpercent.series_urls(EPISODE_URL)


# --- download -------------------------------------------------------------------------


def test_download_writes_the_first_page(client, fake_response, tmp_path):
    raw = BytesIO()
    Image.new("RGB", (6, 9), (10, 20, 30)).save(raw, "PNG")
    fullpercent, session = client(
        {
            "/comic/view/2/2": fake_response(text=EPISODE_HTML),
            "/comic/detail/2": fake_response(text=WORK_HTML),
            "/img/episode/": fake_response(raw.getvalue(), content_type="image/png"),
        },
    )
    result = Downloader(fullpercent, tmp_path, only_first=True).download(EPISODE_URL)

    assert result.status == "saved"
    assert result.save_dir == tmp_path / WORK_TITLE / "第１話"
    with Image.open(result.save_dir / "0.jpg") as saved:
        assert saved.size == (6, 9)
        assert saved.getpixel((0, 0)) == (10, 20, 30)
    assert session.calls[-1] == PAGE_1
    assert session.headers_seen[-1]["Referer"] == EPISODE_URL


# --- login ----------------------------------------------------------------------------


def test_login_posts_the_form_fields(client, fake_response):
    fullpercent, session = client({"/user/login_ajax": fake_response(payload={"result": True})})
    fullpercent.login(EPISODE_URL, "someone@example.com", "hunter2")
    assert session.posts == [
        ("https://fullpercent.net/user/login_ajax", {"loginid": "someone@example.com", "password": "hunter2"}),
    ]


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        ({"result": False, "reason": "auth_failed"}, "auth_failed"),
        ({"result": False, "reason": "require"}, "require"),
        ({"result": False}, "no reason given"),
        ("nonsense", "no reason given"),
    ],
)
def test_login_raises_with_the_site_reason(client, fake_response, payload, reason):
    fullpercent, _ = client({"/user/login_ajax": fake_response(payload=payload)})
    with pytest.raises(LoginError, match=reason):
        fullpercent.login(EPISODE_URL, "someone@example.com", "wrong")


# --- the real site --------------------------------------------------------------------

# One free episode per known host (the first episode of a long-running series).
TEST_URLS: dict[str, str] = {
    "fullpercent.net": "https://fullpercent.net/comic/view/154/468",
}


@pytest.mark.network
@pytest.mark.parametrize("host", TEST_URLS)
def test_site_download(tmp_path, host):
    result = Downloader(FullPercent(), tmp_path, only_first=True).download(TEST_URLS[host])
    assert result.status == "saved"
    assert result.episode.series_title == "ΚΥΡΙΕ—キリエ"
    assert result.episode.episode_title == "第１話"
    assert (result.save_dir / "0.jpg").stat().st_size > 0


@pytest.mark.network
def test_work_page_lists_episodes_oldest_first():
    urls = FullPercent().series_urls("https://fullpercent.net/comic/detail/110")
    assert urls[0] == "https://fullpercent.net/comic/view/110/3509"
    assert len(urls) > 1
    assert all(FullPercent.suitable(url) for url in urls)


@pytest.mark.network
def test_missing_episode_is_not_an_episode():
    with pytest.raises(NotAnEpisodePageError):
        FullPercent().episode("https://fullpercent.net/comic/view/154/9999999")
