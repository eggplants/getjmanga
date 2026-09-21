from __future__ import annotations

import json
from datetime import date
from http import HTTPStatus
from io import BytesIO

import pytest
from PIL import Image

from getjmanga.downloader import Downloader
from getjmanga.errors import NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractors.mechacreators import MechaCreators, next_data, page_data

BASE = "https://creators.mechacomic.jp"
SERIES_URL = f"{BASE}/title/18433"
EPISODE_URL = f"{SERIES_URL}/chapter/50158"
ASSETS = "https://api.creators.mechacomic.jp/assets"


def chapter(chapter_id: int, name: str, **extra) -> dict:
    return {
        "id": chapter_id,
        "name": name,
        "imgUrl": f"{ASSETS}/1/chapter/{chapter_id}.webp?h=x&e=1",
        "published": "2026年4月1日 01:15",
        "pageBegin": 1,
        **extra,
    }


TITLE = {
    "tags": [{"id": 114, "code": "fantasy", "name": "ファンタジー"}],
    "index": {"id": 18433, "name": "ELDER ONE", "author": {"id": 66312, "name": "Ai Otsuki"}},
    "description": "...",
    "genre": {"id": 3, "code": "shonen", "name": "少年漫画"},
    "chapterCount": 3,
}

# Newest first, the way the work page lists them; the two 第1話 halves keep their posting order.
CHAPTERS = [
    chapter(50161, "第2話 予兆"),
    chapter(50155, "第1話 黒腕-①"),
    chapter(50158, "第1話 黒腕-②"),
]


def page_html(page: str, query: dict, page_props: dict) -> str:
    blob = {"props": {"pageProps": page_props}, "page": page, "query": query, "buildId": "IbQ4YbJrvdrW33lMwpvwd"}
    return (
        "<!DOCTYPE html><html><head><title>めちゃコミック クリエイターズ</title></head><body>"
        '<div id="__next"><div class="viewer_wrapper"></div></div>'
        f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(blob, ensure_ascii=False)}</script>'
        "</body></html>"
    )


def episode_html(chapter_id: int, name: str, pages: list[dict], following: dict | None = None) -> str:
    data = {
        "pageImgUrl": [page["imgUrl"] for page in pages],
        "recommendTitles": [],
        "pages": pages,
        "chapter": chapter(chapter_id, name, viewCount=34),
        "title": TITLE,
        "user": {"id": 66312, "name": "Ai Otsuki"},
        "tweetText": "Web漫画投稿サイト #めちゃクリ",
        "prComment": "...",
        "viewerMessage": "「いいね」「シェア」で作家を応援しよう！",
    }
    if following is not None:
        data["nextChapter"] = following
    return page_html(
        "/title/[titleId]/chapter/[chapterId]",
        {"titleId": "18433", "chapterId": str(chapter_id)},
        {"data": data, "error": None, "titleId": "18433", "chapterId": str(chapter_id)},
    )


def series_html(chapters: list[dict]) -> str:
    return page_html(
        "/title/[titleId]",
        {"titleId": "18433"},
        {"data": {"chapters": chapters, "contests": [], "recommendTitles": [], "title": TITLE}, "error": None},
    )


PAGES = [{"imgUrl": f"{ASSETS}/1774973840/manga/63994/{n}_0.webp?h=Geq2eo1H&e=1790812800"} for n in (1, 2, 3)]
EPISODE_HTML = episode_html(50158, "第1話 黒腕-②", PAGES, following=chapter(50155, "第1話 黒腕-①"))
LAST_HTML = episode_html(50161, "第2話 予兆", PAGES)
PAGELESS_HTML = episode_html(50158, "第1話 黒腕-②", [], following=chapter(50155, "第1話 黒腕-①"))
SERIES_HTML = series_html(CHAPTERS)
NOT_FOUND_HTML = page_html("/404", {}, {})
ERROR_HTML = page_html(
    "/title/[titleId]/chapter/[chapterId]",
    {"titleId": "18433", "chapterId": "50158"},
    {"data": None, "error": {"code": 500}, "titleId": "18433", "chapterId": "50158"},
)


@pytest.fixture
def client(fake_session, fake_response):
    def build(routes=None):
        merged = dict(routes or {})
        merged.setdefault("/title/18433/chapter/50158", fake_response(text=EPISODE_HTML))
        merged.setdefault("/title/18433/chapter/50161", fake_response(text=LAST_HTML))
        merged.setdefault("/title/18433", fake_response(text=SERIES_HTML))
        session = fake_session(merged)
        return MechaCreators(session), session

    return build


# --- URLs ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        SERIES_URL,
        f"{SERIES_URL}/",
        EPISODE_URL,
        f"{EPISODE_URL}/",
        f"{BASE}/title/45/chapter/48",
    ],
)
def test_suitable_accepts_work_and_chapter_pages(url):
    assert MechaCreators.suitable(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://creators.mechacomic.jp/title/18433/chapter/50158",
        "https://mechacomic.jp/title/18433/chapter/50158",
        "https://sp.mechacomic.jp/title/18433",
        "https://example.com/title/18433/chapter/50158",
        f"{BASE}/",
        f"{BASE}/title",
        f"{BASE}/title/abc",
        f"{BASE}/title/18433/chapter",
        f"{BASE}/title/18433/chapter/50158/comment",
        f"{BASE}/novel/18433/chapter/50158",
        f"{BASE}/illust/18433",
        f"{BASE}/author/66312",
        f"{BASE}/genre/shonen",
    ],
)
def test_suitable_rejects_other_pages(url):
    assert not MechaCreators.suitable(url)


def test_is_series_tells_a_work_page_from_a_chapter():
    assert MechaCreators.is_series(SERIES_URL)
    assert MechaCreators.is_series(f"{SERIES_URL}/")
    assert not MechaCreators.is_series(EPISODE_URL)


# --- parsing ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "html",
    [
        '<script id="__NEXT_DATA__" type="application/json">{not json</script>',
        '<script id="__NEXT_DATA__" type="application/json">[1, 2]</script>',
    ],
)
def test_next_data_rejects_a_page_without_a_usable_blob(html):
    with pytest.raises(NotAnEpisodePageError, match="__NEXT_DATA__"):
        next_data(html)


def test_page_data_rejects_a_page_that_carries_an_error_instead_of_data():
    with pytest.raises(NotAnEpisodePageError, match="no data"):
        page_data(ERROR_HTML, "/title/[titleId]/chapter/[chapterId]")


# --- episodes -----------------------------------------------------------------------------------


def test_episode_reads_the_titles_the_pages_and_the_next_chapter(client):
    mecha, session = client()
    episode = mecha.episode(EPISODE_URL)

    assert episode.series_title == "ELDER ONE"
    assert (episode.writer, episode.publisher) == ("Ai Otsuki", "アムタス")
    assert (episode.published, episode.number) == (date(2026, 4, 1), 1)
    assert episode.episode_title == "第1話 黒腕-②"
    assert [page.url for page in episode.pages] == [page["imgUrl"] for page in PAGES]
    assert episode.pages[0].width == 0
    assert (episode.prev_url, episode.next_url) == (None, f"{SERIES_URL}/chapter/50155")
    assert episode.metadata["chapter"]["id"] == 50158
    assert episode.metadata["nextChapter"]["name"] == "第1話 黒腕-①"
    assert episode.metadata["title"]["index"]["name"] == "ELDER ONE"
    assert episode.metadata["titleId"] == "18433"
    json.dumps(episode.metadata)
    # The chapter page, then the work page for the chapter before, which only that lists.
    assert session.calls == [EPISODE_URL, SERIES_URL]
    assert session.headers_seen[0]["User-Agent"].startswith("Mozilla/5.0")


def test_episode_keeps_the_page_size_when_the_site_names_one(client, fake_response):
    sized = [{"imgUrl": f"{ASSETS}/1/manga/1/1_0.webp?h=a&e=1", "width": 800, "height": 1113}]
    mecha, _ = client({"/title/18433/chapter/50158": fake_response(text=episode_html(50158, "x", sized))})
    episode = mecha.episode(EPISODE_URL)

    assert (episode.pages[0].width, episode.pages[0].height) == (800, 1113)


def test_episode_has_no_next_url_at_the_end_of_the_work(client):
    mecha, _ = client()
    episode = mecha.episode(f"{SERIES_URL}/chapter/50161")

    assert episode.episode_title == "第2話 予兆"
    assert (episode.prev_url, episode.next_url) == (f"{SERIES_URL}/chapter/50155", None)


def test_episode_names_the_next_chapter_under_the_work_the_site_says_not_the_url(client, fake_response):
    # The server keys on the chapter id alone, so the title id in the URL may be anything.
    mecha, _ = client({"/title/999/chapter/50158": fake_response(text=EPISODE_HTML)})
    episode = mecha.episode(f"{BASE}/title/999/chapter/50158")

    assert episode.next_url == f"{SERIES_URL}/chapter/50155"
    assert episode.metadata["titleId"] == "18433"


def test_episode_without_pages_is_not_readable_but_still_names_the_next(client, fake_response):
    mecha, _ = client({"/title/18433/chapter/50158": fake_response(text=PAGELESS_HTML)})
    episode = mecha.episode(EPISODE_URL)

    assert not episode.readable
    assert episode.pages == ()
    assert episode.episode_title == "第1話 黒腕-②"
    assert episode.next_url == f"{SERIES_URL}/chapter/50155"


def test_episode_answered_404_is_not_an_episode(client, fake_response):
    mecha, _ = client(
        {"/title/18433/chapter/50158": fake_response(text=NOT_FOUND_HTML, status_code=HTTPStatus.NOT_FOUND)}
    )
    with pytest.raises(NotAnEpisodePageError, match="404"):
        mecha.episode(EPISODE_URL)


def test_episode_served_the_404_page_with_200_is_not_an_episode(client, fake_response):
    mecha, _ = client({"/title/18433/chapter/50158": fake_response(text=NOT_FOUND_HTML)})
    with pytest.raises(NotAnEpisodePageError, match="'/404'"):
        mecha.episode(EPISODE_URL)


def test_episode_page_without_data_is_not_an_episode(client, fake_response):
    mecha, _ = client({"/title/18433/chapter/50158": fake_response(text="<html><body><p>meh</p></body></html>")})
    with pytest.raises(NotAnEpisodePageError, match="__NEXT_DATA__"):
        mecha.episode(EPISODE_URL)


def test_episode_refuses_a_work_url(client):
    mecha, _ = client()
    with pytest.raises(UnsupportedUrlError, match="series_urls"):
        mecha.episode(SERIES_URL)


# --- series -------------------------------------------------------------------------------------


def test_series_urls_lists_the_chapters_the_way_the_next_button_walks_them(client):
    mecha, session = client()
    urls = mecha.series_urls(SERIES_URL)

    assert urls == [
        f"{SERIES_URL}/chapter/50158",
        f"{SERIES_URL}/chapter/50155",
        f"{SERIES_URL}/chapter/50161",
    ]
    assert all(MechaCreators.suitable(url) and not MechaCreators.is_series(url) for url in urls)
    assert session.calls == [SERIES_URL]


def test_series_urls_deduplicates_and_skips_a_chapter_without_an_id(client, fake_response):
    listing = [chapter(50161, "第2話 予兆"), {"name": "?"}, chapter(50161, "第2話 予兆"), chapter(50155, "第1話")]
    mecha, _ = client({"/title/18433": fake_response(text=series_html(listing))})

    assert mecha.series_urls(f"{SERIES_URL}/") == [f"{SERIES_URL}/chapter/50155", f"{SERIES_URL}/chapter/50161"]


def test_series_urls_raises_on_an_empty_listing(client, fake_response):
    mecha, _ = client({"/title/18433": fake_response(text=series_html([]))})
    with pytest.raises(NotAnEpisodePageError, match="lists no chapter"):
        mecha.series_urls(SERIES_URL)


def test_series_urls_raises_on_a_missing_work(client, fake_response):
    mecha, _ = client({"/title/18433": fake_response(text=NOT_FOUND_HTML, status_code=HTTPStatus.NOT_FOUND)})
    with pytest.raises(NotAnEpisodePageError, match="404"):
        mecha.series_urls(SERIES_URL)


def test_series_urls_refuses_a_chapter_url(client):
    mecha, _ = client()
    with pytest.raises(UnsupportedUrlError):
        mecha.series_urls(EPISODE_URL)


# --- downloading --------------------------------------------------------------------------------


def test_download_writes_the_pages_as_served_and_the_metadata(client, fake_response, tmp_path):
    buffer = BytesIO()
    Image.new("RGB", (4, 6), (10, 20, 30)).save(buffer, "WEBP")
    mecha, session = client({"/manga/63994/": fake_response(buffer.getvalue(), content_type="image/webp")})

    result = Downloader(mecha, tmp_path, save_metadata=True).download(EPISODE_URL)

    assert result.status == "saved"
    assert result.save_dir == tmp_path / "creators.mechacomic.jp" / "ELDER ONE" / "第1話 黒腕-②"
    assert sorted(path.name for path in result.save_dir.iterdir()) == ["0.jpg", "1.jpg", "2.jpg", "metadata.json"]
    with Image.open(result.save_dir / "0.jpg") as saved:
        assert saved.size == (4, 6)
    assert session.calls[2:] == [page["imgUrl"] for page in PAGES]
    assert session.headers_seen[-1]["Referer"] == EPISODE_URL
    metadata = json.loads((result.save_dir / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["metadata"]["chapter"]["name"] == "第1話 黒腕-②"
    assert metadata["next_url"] == f"{SERIES_URL}/chapter/50155"


# --- the real site --------------------------------------------------------------------


# One episode per known host, free to read without an account.
TEST_URLS: dict[str, str] = {
    "creators.mechacomic.jp": "https://creators.mechacomic.jp/title/45/chapter/48",
}


@pytest.mark.network
@pytest.mark.parametrize("host", TEST_URLS)
def test_site_download(tmp_path, host):
    result = Downloader(MechaCreators(), tmp_path, only_first=True).download(TEST_URLS[host])
    assert result.status == "saved"
    assert result.episode.series_title == "これはきっと満点の"
    assert result.episode.episode_title == "第1話 これはきっと満点の"
    assert len(result.episode.pages) == 16
    assert (result.save_dir / "0.jpg").stat().st_size > 0


@pytest.mark.network
def test_work_page_lists_chapters_in_reading_order():
    mecha = MechaCreators()
    urls = mecha.series_urls("https://creators.mechacomic.jp/title/18433")
    assert urls[:3] == [
        "https://creators.mechacomic.jp/title/18433/chapter/50158",
        "https://creators.mechacomic.jp/title/18433/chapter/50155",
        "https://creators.mechacomic.jp/title/18433/chapter/50161",
    ]
    assert len(urls) >= 9
    assert all(MechaCreators.suitable(url) for url in urls)
    # The site's own "next" button agrees with the listing.
    assert mecha.episode(urls[0]).next_url == urls[1]


@pytest.mark.network
def test_missing_chapter_is_not_an_episode():
    with pytest.raises(NotAnEpisodePageError, match="404"):
        MechaCreators().episode("https://creators.mechacomic.jp/title/45/chapter/999999999")
