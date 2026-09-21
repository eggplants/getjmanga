from __future__ import annotations

import json
from datetime import date
from http import HTTPStatus
from io import BytesIO

import pytest
from PIL import Image

from getjmanga.downloader import Downloader
from getjmanga.errors import NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractors.fleur import Fleur, original_url

ORIGIN = "https://comic.mf-fleur.jp"
WORK_URL = f"{ORIGIN}/lineup_magazine/cb264/"
EPISODE_URL = f"{ORIGIN}/manga/cb264_01.html"
NEXT_URL = f"{ORIGIN}/manga/cb264_02_01.html"
MEDIA = f"{ORIGIN}/media/020/202607"

# The page images as the entry carries them: the resized copy, the original next to it.
SERVED = [
    f"{MEDIA}/mode3_w1200-cb264_tobira.jpg?v=20260728205539",
    f"{MEDIA}/mode3_w1200-%E3%82%86%E3%81%8D01_02.jpg?v=20260728205721",
    f"{MEDIA}/mode3_w1512-%E3%82%86%E3%81%8D01_03.jpg?v=20260728205720",
]
ORIGINALS = [
    f"{MEDIA}/cb264_tobira.jpg?v=20260728205539",
    f"{MEDIA}/%E3%82%86%E3%81%8D01_02.jpg?v=20260728205721",
    f"{MEDIA}/%E3%82%86%E3%81%8D01_03.jpg?v=20260728205720",
]


def page_image(src: str) -> str:
    """One page the way the CMS entry writes it."""
    return f"""
<div class="manga-content__image column-media-center js_notStyle acms-col-sm-12 _l">
<img src="{src}" alt="" width="1200" onselectstart="return false;" oncontextmenu="return false;">
</div>
<hr class="clearHidden">"""


def episode_html(
    *images: str,
    series: str = "ゆきあいの青",
    title: str = "第1話",
    next_url: str | None = NEXT_URL,
    prev_url: str | None = None,
    header: bool = True,
) -> str:
    pager = ""
    if prev_url:
        pager += f'<a class="manga-pager__btn _prev" href="{prev_url}">前の話</a>'
    if next_url:
        pager += f'<a class="manga-pager__btn _next" href="{next_url}">次の話</a>'
    head = (
        f"""<div class="manga-header">
<div class="manga-header__name"><a href="{WORK_URL}" class="manga-header__name--link">{series}</a></div>
<div class="manga-header__title"> {title} </div>
</div>"""
        if header
        else ""
    )
    return f"""<!DOCTYPE html><html lang="ja"><head>
<title>{series}　{title} | COMICフルール</title>
<meta property="og:title" content="{series}　{title} | COMICフルール">
</head><body>
<div class="manga"><div class="inner-width"><div class="manga-inner">
{head}
<div class="manga-pager js-child-not-del">{pager}</div>
<div class="acms-entry manga-content"><div class="acms-grid">
<div class="column-media-">
<a href="{MEDIA}/cb264_tobira.jpg?v=20260728205539" data-rel="SmartPhoto" data-group="32458">
<img class="js-lazy-load columnImage unit-id-" data-src="{ORIGIN}/" width="" height="" alt="">
</a>
</div>
{"".join(page_image(src) for src in images)}
</div></div>
<div class="manga-info"><h2 class="manga-info__head"> コミックス情報 </h2>
<div class="manga-info__image"><img alt="既刊" src="/media/020/202607/9784046852588.jpg?v=1" /></div></div>
<div class="manga-pager">{pager}</div>
</div></div></div>
</body></html>"""


EPISODE_HTML = episode_html(*SERVED)


def story_link(href: str, title: str) -> str:
    return f"""<li class="cb-story-links__item">
<a href="{href}" target="_blank" class="cb-story-links__item--link">
<div class="cb-story-links__item--image"><img alt="{title}" src="/media/016/x.jpg" /></div>
<div class="cb-story-links__item--description"><div class="cb-story-links__item--description__head">
<div class="cb-story-links__item--date"> 2026/07/{href.rsplit("_", 1)[-1][:2]} 更新 </div></div>
<div class="cb-story-links__item--title"> {title} </div></div>
</a></li>"""


def work_html(*links: tuple[str, str]) -> str:
    return f"""<!DOCTYPE html><html lang="ja"><head><title>ゆきあいの青 | COMICフルール</title></head><body>
<div class="cb-author"><div class="cb-author__item">
<a class="cb-author__link" href="{ORIGIN}/search/keyword/たつもとみお/?type=lineup&blog=magazine">たつもとみお</a>
</div></div>
<div class="cb-story-links"><ul class="cb-story-links__list">
{"".join(story_link(href, title) for href, title in links)}
</ul></div>
<a href="{ORIGIN}/manga/cb264_99.html">読者ページに出てこない別のリンク</a>
</body></html>"""


# Newest first, the latest repeated in a banner, a news post and a video in between.
WORK_HTML = work_html(
    (f"{ORIGIN}/manga/cb264_02_02.html", "第2話後編"),
    (f"{ORIGIN}/manga/cb264_02_02.html", "第2話後編"),
    (f"{ORIGIN}/manga/cb264_02_01.html?utm=x", "第2話前編"),
    (f"{ORIGIN}/special/info_026.html", "実写ドラマ化決定"),
    ("https://youtu.be/NYVPajfElsM", "PV"),
    (f"{ORIGIN}/manga/cb264_01.html", "第1話"),
)

NOT_FOUND_HTML = (
    "<html><head><title>お探しのページは見つかりませんでした漫画ページ | COMICフルール</title></head></html>"
)


def jpeg(size=(8, 8), colour=(10, 20, 30)) -> bytes:
    raw = BytesIO()
    Image.new("RGB", size, colour).save(raw, "JPEG")
    return raw.getvalue()


@pytest.fixture
def client(fake_session, fake_response):
    def build(routes=None):
        merged = dict(routes or {})
        merged.setdefault("/manga/cb264_01.html", fake_response(text=EPISODE_HTML))
        merged.setdefault("/lineup_magazine/cb264/", fake_response(text=WORK_HTML))
        merged.setdefault("/media/", fake_response(jpeg(), content_type="image/jpeg"))
        session = fake_session(merged)
        return Fleur(session), session

    return build


# --- URLs ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "url",
    [
        EPISODE_URL,
        f"{ORIGIN}/manga/cb146_02_01.html",
        WORK_URL,
        f"{ORIGIN}/lineup_magazine/cb264",
    ],
)
def test_suitable_accepts_episode_and_work_pages(url):
    assert Fleur.suitable(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://comic.mf-fleur.jp/manga/cb264_01.html",
        "https://comic.mf-fleur.jp/",
        "https://comic.mf-fleur.jp/manga/",
        "https://comic.mf-fleur.jp/manga/cb264_01",
        "https://comic.mf-fleur.jp/lineup_magazine/",
        "https://comic.mf-fleur.jp/lineup_ebook/eb266/",
        "https://comic.mf-fleur.jp/product/322607000041.html",
        "https://comic.mf-fleur.jp/special/info_026.html",
        "https://mf-fleur.jp/manga/cb264_01.html",
        "https://comic-walker.com/manga/cb264_01.html",
    ],
)
def test_suitable_rejects_other_pages(url):
    assert not Fleur.suitable(url)


def test_is_series_tells_a_work_page_from_an_episode():
    assert Fleur.is_series(WORK_URL)
    assert Fleur.is_series(f"{ORIGIN}/lineup_magazine/cb264")
    assert not Fleur.is_series(EPISODE_URL)


@pytest.mark.parametrize(
    ("served", "original"),
    [
        (SERVED[0], ORIGINALS[0]),
        (SERVED[2], ORIGINALS[2]),
        (f"{MEDIA}/mode3_w800-7ba64961.jpg?v=20200129191518", f"{MEDIA}/7ba64961.jpg?v=20200129191518"),
        # Only the file name carries the prefix; a directory that looks like one is left alone.
        (f"{ORIGIN}/mode3_w1200-dir/plain.jpg", f"{ORIGIN}/mode3_w1200-dir/plain.jpg"),
        (ORIGINALS[1], ORIGINALS[1]),
    ],
)
def test_original_url_strips_the_resize_prefix_off_the_file_name(served, original):
    assert original_url(served) == original


# --- episode() ------------------------------------------------------------------------
def test_episode_reads_the_titles_the_pages_and_the_next_episode(client):
    fleur, session = client()
    episode = fleur.episode(EPISODE_URL)

    assert episode.url == EPISODE_URL
    assert episode.series_title == "ゆきあいの青"
    assert episode.episode_title == "第1話"
    assert (episode.writer, episode.publisher) == ("たつもとみお", "KADOKAWA")
    assert episode.published == date(2026, 7, 1)
    assert [page.url for page in episode.pages] == ORIGINALS
    assert [page.extra["served"] for page in episode.pages] == SERVED
    assert episode.next_url == NEXT_URL
    assert episode.metadata == {"id": "cb264_01", "images": SERVED, "prev_url": None}
    json.dumps(episode.metadata)
    # The credits come off the work page, read once.
    assert session.calls == [EPISODE_URL, WORK_URL]
    assert "User-Agent" in session.headers_seen[0]


def test_episode_at_the_end_of_the_work_has_no_next_url(client, fake_response):
    html = episode_html(*SERVED, title="第2話後編", next_url=None, prev_url=EPISODE_URL)
    fleur, _ = client({"/manga/cb264_02_02.html": fake_response(text=html)})
    episode = fleur.episode(f"{ORIGIN}/manga/cb264_02_02.html")

    assert episode.episode_title == "第2話後編"
    assert (episode.prev_url, episode.next_url) == (EPISODE_URL, None)
    assert episode.metadata["prev_url"] == EPISODE_URL


def test_episode_without_images_is_not_readable_but_still_names_the_next(client, fake_response):
    fleur, _ = client({"/manga/cb264_01.html": fake_response(text=episode_html())})
    episode = fleur.episode(EPISODE_URL)

    assert episode.pages == ()
    assert not episode.readable
    assert episode.next_url == NEXT_URL


def test_episode_falls_back_on_the_page_title_without_an_entry_header(client, fake_response):
    fleur, _ = client({"/manga/cb264_01.html": fake_response(text=episode_html(*SERVED, header=False))})
    episode = fleur.episode(EPISODE_URL)

    assert episode.series_title == "ゆきあいの青"
    assert episode.episode_title == "第1話"


def test_episode_answered_404_is_not_an_episode(client, fake_response):
    fleur, _ = client({"/manga/cb264_01.html": fake_response(text=NOT_FOUND_HTML, status_code=HTTPStatus.NOT_FOUND)})
    with pytest.raises(NotAnEpisodePageError, match="404"):
        fleur.episode(EPISODE_URL)


def test_episode_page_without_an_entry_is_not_an_episode(client, fake_response):
    fleur, _ = client({"/manga/cb264_01.html": fake_response(text="<html><body><p>hi</p></body></html>")})
    with pytest.raises(NotAnEpisodePageError, match="no manga entry"):
        fleur.episode(EPISODE_URL)


def test_episode_refuses_a_work_url(client):
    fleur, session = client()
    with pytest.raises(UnsupportedUrlError):
        fleur.episode(WORK_URL)
    assert session.calls == []


# --- series_urls() ----------------------------------------------------------------------
def test_series_urls_lists_the_episodes_oldest_first_without_repeats_or_other_links(client):
    fleur, session = client()
    assert fleur.series_urls(WORK_URL) == [
        EPISODE_URL,
        f"{ORIGIN}/manga/cb264_02_01.html",
        f"{ORIGIN}/manga/cb264_02_02.html",
    ]
    assert session.calls == [WORK_URL]


def test_series_urls_raises_on_an_empty_listing(client, fake_response):
    fleur, _ = client({"/lineup_magazine/cb264/": fake_response(text=work_html())})
    with pytest.raises(NotAnEpisodePageError, match="lists no episode"):
        fleur.series_urls(WORK_URL)


def test_series_urls_raises_on_a_missing_work(client, fake_response):
    fleur, _ = client({"/lineup_magazine/cb264/": fake_response(text=NOT_FOUND_HTML, status_code=HTTPStatus.NOT_FOUND)})
    with pytest.raises(NotAnEpisodePageError, match="404"):
        fleur.series_urls(WORK_URL)


def test_series_urls_refuses_an_episode_url(client):
    fleur, _ = client()
    with pytest.raises(UnsupportedUrlError):
        fleur.series_urls(EPISODE_URL)


# --- image() and download --------------------------------------------------------------
def test_image_fetches_the_original_with_the_episode_as_referer(client):
    fleur, session = client()
    episode = fleur.episode(EPISODE_URL)
    image = fleur.image(episode.pages[0], episode)

    assert image.size == (8, 8)
    assert session.calls[-1] == ORIGINALS[0]
    assert session.headers_seen[-1]["Referer"] == EPISODE_URL


def test_image_falls_back_on_the_served_copy_when_the_original_is_gone(client, fake_response):
    fleur, session = client(
        {
            "/media/020/202607/cb264_tobira.jpg": fake_response(b"", status_code=HTTPStatus.NOT_FOUND),
            "/media/020/202607/mode3_w1200-cb264_tobira.jpg": fake_response(
                jpeg(size=(4, 6)), content_type="image/jpeg"
            ),
        },
    )
    episode = fleur.episode(EPISODE_URL)
    image = fleur.image(episode.pages[0], episode)

    assert image.size == (4, 6)
    assert session.calls[-2:] == [ORIGINALS[0], SERVED[0]]


# --- the real site --------------------------------------------------------------------

TEST_URLS: dict[str, str] = {
    "comic.mf-fleur.jp": "https://comic.mf-fleur.jp/manga/cb264_01.html",
}


@pytest.mark.network
@pytest.mark.parametrize("host", TEST_URLS)
def test_site_download(tmp_path, host):
    result = Downloader(Fleur(), tmp_path, only_first=True).download(TEST_URLS[host])
    assert result.status == "saved"
    assert (result.save_dir / "0.jpg").stat().st_size > 0


@pytest.mark.network
def test_work_page_lists_episodes():
    urls = Fleur().series_urls("https://comic.mf-fleur.jp/lineup_magazine/cb264/")
    assert urls[0] == "https://comic.mf-fleur.jp/manga/cb264_01.html"
    assert all(Fleur.suitable(url) for url in urls)


@pytest.mark.network
def test_taken_down_episode_is_not_an_episode():
    with pytest.raises(NotAnEpisodePageError):
        Fleur().episode("https://comic.mf-fleur.jp/manga/cb146_02.html")
