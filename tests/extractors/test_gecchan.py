from __future__ import annotations

import json
from http import HTTPStatus
from io import BytesIO

import pytest
from PIL import Image

from getjmanga.downloader import Downloader
from getjmanga.errors import NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractors.gecchan import Gecchan, parse_work

WORK_URL = "https://nikkangecchan.jp/comics/hanikamu"
EPISODE_URL = f"{WORK_URL}/2"


def section(number: int, label: str, heading: str, *, image: bool = True) -> str:
    """One episode section the way the work page writes it."""
    img = (
        f'<img class="episode-page" data-src="/comics/hanikamu/{number}/image" '
        f'data-title="{heading} | ハニカム | 日刊月チャン" />'
        if image
        else '<img class="episode-page" />'
    )
    return f"""
<section><div class="content episodeBox">
<h4 class="episodeTitle">{label}</h4>
<div class="contentInner"><figure>{img}</figure>
<div class="snsBox"><a href="#comicDetail">作品詳細</a></div></div>
</div></section>"""


def work_html(*sections: str) -> str:
    return f"""<!DOCTYPE html><html><head>
<meta content="ハニカム | 日刊月チャン" property="og:title" />
<title>ハニカム | 日刊月チャン</title>
</head><body>
<header id="header"><h2><a href="/comics/hanikamu"><img alt="ハニカム" src="/comics/hanikamu/logo" /></a></h2></header>
<section id="comicDetail"><div class="content"><figure>
<div class="imgBox"><img src="/comics/hanikamu/image" alt="Image" /></div>
<div class="detailBox"><h3>ハニカム</h3><div class="author">まりぱか</div></div>
</figure><div class="btnBox">
<div class="button"><a href="/comics/hanikamu/3">最新話を読む</a></div>
<div class="button"><a href="/comics/hanikamu/1">1話から読む</a></div>
</div></div></section>
{"".join(sections)}
</body></html>"""


WORK_HTML = work_html(
    section(1, "プロローグの1", "東京都××高校"),
    section(2, "プロローグの2", " 誰とでも仲良くできる男"),
    section(3, "001", "人気者"),
)

# The middle section lost its image: the only shape a "not readable" episode could take here.
IMAGELESS_HTML = work_html(
    section(1, "プロローグの1", "東京都××高校"),
    section(2, "プロローグの2", "", image=False),
    section(3, "001", "人気者"),
)

NOT_FOUND_HTML = "<html><head><title>日刊月チャン</title></head><body><p>404</p></body></html>"


@pytest.fixture
def client(fake_session, fake_response):
    def build(routes=None):
        merged = dict(routes or {})
        merged.setdefault("/comics/hanikamu", fake_response(text=WORK_HTML))
        session = fake_session(merged)
        return Gecchan(session), session

    return build


# --- URLs ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        WORK_URL,
        f"{WORK_URL}/",
        EPISODE_URL,
        f"{EPISODE_URL}/",
        "https://nikkangecchan.jp/comics/04R1/237",
    ],
)
def test_suitable_accepts_work_and_episode_pages(url):
    assert Gecchan.suitable(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://nikkangecchan.jp/comics/hanikamu/1",
        "https://www.nikkangecchan.jp/comics/hanikamu/1",
        "https://example.com/comics/hanikamu/1",
        "https://nikkangecchan.jp/",
        "https://nikkangecchan.jp/news",
        "https://nikkangecchan.jp/comics/hanikamu/1/image",
        "https://nikkangecchan.jp/comics/hanikamu/ogp",
        "https://nikkangecchan.jp/comics",
    ],
)
def test_suitable_rejects_other_pages(url):
    assert not Gecchan.suitable(url)


def test_is_series_tells_a_work_page_from_an_episode():
    assert Gecchan.is_series(WORK_URL)
    assert Gecchan.is_series(f"{WORK_URL}/")
    assert not Gecchan.is_series(EPISODE_URL)


# --- parsing ------------------------------------------------------------------------------------


def test_parse_work_falls_back_on_the_og_title():
    html = WORK_HTML.replace("<h3>ハニカム</h3>", "")
    series_title, entries = parse_work(html, WORK_URL)

    assert series_title == "ハニカム"
    assert len(entries) == 3


# --- episodes -----------------------------------------------------------------------------------


def test_episode_reads_the_titles_the_page_and_the_next_episode(client):
    gecchan, session = client()
    episode = gecchan.episode(EPISODE_URL)

    assert episode.series_title == "ハニカム"
    assert episode.episode_title == "プロローグの2 誰とでも仲良くできる男"
    assert [page.url for page in episode.pages] == ["https://nikkangecchan.jp/comics/hanikamu/2/image"]
    assert episode.next_url == f"{WORK_URL}/3"
    assert episode.metadata == {
        "slug": "hanikamu",
        "number": 2,
        "label": "プロローグの2",
        "heading": "誰とでも仲良くできる男",
        "image": "https://nikkangecchan.jp/comics/hanikamu/2/image",
    }
    json.dumps(episode.metadata)
    assert session.calls == [EPISODE_URL]
    assert session.headers_seen[0]["User-Agent"].startswith("Mozilla/5.0")


def test_episode_has_no_next_url_at_the_end_of_the_work(client):
    gecchan, _ = client()
    episode = gecchan.episode(f"{WORK_URL}/3")

    assert episode.episode_title == "001 人気者"
    assert episode.next_url is None


def test_episode_without_an_image_is_not_readable_but_still_names_the_next(client, fake_response):
    gecchan, _ = client({"/comics/hanikamu": fake_response(text=IMAGELESS_HTML)})
    episode = gecchan.episode(EPISODE_URL)

    assert not episode.readable
    assert episode.pages == ()
    assert episode.episode_title == "プロローグの2"
    assert episode.next_url == f"{WORK_URL}/3"


def test_episode_the_page_does_not_list_is_not_an_episode(client):
    gecchan, _ = client()
    with pytest.raises(NotAnEpisodePageError, match="lists no episode 4"):
        gecchan.episode(f"{WORK_URL}/4")


def test_episode_answered_404_is_not_an_episode(client, fake_response):
    gecchan, _ = client({"/comics/hanikamu": fake_response(text=NOT_FOUND_HTML, status_code=HTTPStatus.NOT_FOUND)})
    with pytest.raises(NotAnEpisodePageError, match="404"):
        gecchan.episode(f"{WORK_URL}/999")


def test_episode_page_without_a_work_is_not_an_episode(client, fake_response):
    gecchan, _ = client({"/comics/hanikamu": fake_response(text="<html><body><p>meh</p></body></html>")})
    with pytest.raises(NotAnEpisodePageError, match="no work"):
        gecchan.episode(EPISODE_URL)


def test_episode_refuses_a_work_url(client):
    gecchan, _ = client()
    with pytest.raises(UnsupportedUrlError, match="series_urls"):
        gecchan.episode(WORK_URL)


# --- series -------------------------------------------------------------------------------------


def test_series_urls_lists_the_episodes_first_to_latest(client):
    gecchan, session = client()
    urls = gecchan.series_urls(WORK_URL)

    assert urls == [f"{WORK_URL}/1", f"{WORK_URL}/2", f"{WORK_URL}/3"]
    assert all(Gecchan.suitable(url) and not Gecchan.is_series(url) for url in urls)
    assert session.calls == [WORK_URL]


def test_series_urls_lists_the_imageless_episode_too(client, fake_response):
    gecchan, _ = client({"/comics/hanikamu": fake_response(text=IMAGELESS_HTML)})
    assert gecchan.series_urls(f"{WORK_URL}/") == [f"{WORK_URL}/1", f"{WORK_URL}/2", f"{WORK_URL}/3"]


def test_series_urls_raises_on_an_empty_listing(client, fake_response):
    gecchan, _ = client({"/comics/hanikamu": fake_response(text=work_html())})
    with pytest.raises(NotAnEpisodePageError, match="lists no episode"):
        gecchan.series_urls(WORK_URL)


def test_series_urls_raises_on_a_missing_work(client, fake_response):
    gecchan, _ = client({"/comics/hanikamu": fake_response(text=NOT_FOUND_HTML, status_code=HTTPStatus.NOT_FOUND)})
    with pytest.raises(NotAnEpisodePageError, match="404"):
        gecchan.series_urls(WORK_URL)


def test_series_urls_refuses_an_episode_url(client):
    gecchan, _ = client()
    with pytest.raises(UnsupportedUrlError):
        gecchan.series_urls(EPISODE_URL)


# --- downloading --------------------------------------------------------------------------------


def test_download_writes_the_page_as_served_and_the_metadata(client, fake_response, tmp_path):
    buffer = BytesIO()
    Image.new("RGB", (4, 6), (10, 20, 30)).save(buffer, "PNG")
    gecchan, session = client({"/comics/hanikamu/2/image": fake_response(buffer.getvalue(), content_type="image/jpeg")})

    result = Downloader(gecchan, tmp_path, save_metadata=True).download(EPISODE_URL)

    assert result.status == "saved"
    assert result.save_dir == tmp_path / "ハニカム" / "プロローグの2 誰とでも仲良くできる男"
    with Image.open(result.save_dir / "0.jpg") as saved:
        assert saved.size == (4, 6)
    assert session.calls[-1] == "https://nikkangecchan.jp/comics/hanikamu/2/image"
    assert session.headers_seen[-1]["Referer"] == EPISODE_URL
    metadata = json.loads((result.save_dir / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["metadata"]["number"] == 2
    assert metadata["next_url"] == f"{WORK_URL}/3"


# --- the real site --------------------------------------------------------------------


# One episode per known host, free to read without an account.
TEST_URLS: dict[str, str] = {
    "nikkangecchan.jp": "https://nikkangecchan.jp/comics/hanikamu/1",
}


@pytest.mark.network
@pytest.mark.parametrize("host", TEST_URLS)
def test_site_download(tmp_path, host):
    result = Downloader(Gecchan(), tmp_path, only_first=True).download(TEST_URLS[host])
    assert result.status == "saved"
    assert result.episode.series_title == "ハニカム"
    assert result.episode.next_url == "https://nikkangecchan.jp/comics/hanikamu/2"
    assert (result.save_dir / "0.jpg").stat().st_size > 0


@pytest.mark.network
def test_work_page_lists_episodes():
    urls = Gecchan().series_urls("https://nikkangecchan.jp/comics/hanikamu")
    assert urls[0] == "https://nikkangecchan.jp/comics/hanikamu/1"
    assert len(urls) >= 52
    assert all(Gecchan.suitable(url) for url in urls)
