from __future__ import annotations

import json
from http import HTTPStatus
from io import BytesIO

import pytest
from httpx import HTTPStatusError
from PIL import Image

from getjmanga.downloader import Downloader
from getjmanga.errors import LoginError, NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractors.gigaviewer import DIV, MUL, GigaViewer, descramble

# One episode per known host, free to read without an account.
TEST_URLS: dict[str, str] = {
    "comic-action.com": "https://comic-action.com/episode/13933686331621197279",
    "comic-days.com": "https://comic-days.com/episode/10834108156631495205",
    "comic-earthstar.com": "https://comic-earthstar.com/episode/14079602755509007065",
    "comic-gardo.com": "https://comic-gardo.com/episode/3269754496561198488",
    "comic-ogyaaa.com": "https://comic-ogyaaa.com/episode/3269754496829572092",
    "comic-seasons.com": "https://comic-seasons.com/episode/2550912964824975149",
    "comic-trail.com": "https://comic-trail.com/episode/3269632237330707078",
    "comic-y-ours.com": "https://comic-y-ours.com/episode/12207421983499079730",
    "comic-zenon.com": "https://comic-zenon.com/episode/10834108156688950516",
    "comicborder.com": "https://comicborder.com/episode/3269632237287061913",
    "feelweb.jp": "https://feelweb.jp/episode/3269754496367124953",
    "ichicomi.com": "https://ichicomi.com/episode/2550912965919401629",
    "kuragebunch.com": "https://kuragebunch.com/episode/3269754496410437550",
    "magcomi.com": "https://magcomi.com/episode/4856001361341293045",
    "mangatime-square.com": "https://mangatime-square.com/episode/12207421983667738694",
    "ourfeel.jp": "https://ourfeel.jp/episode/2550689798581262904",
    "shonenjumpplus.com": "https://shonenjumpplus.com/episode/10834108156648240735",
    "tonarinoyj.jp": "https://tonarinoyj.jp/episode/10834108156765668108",
    "www.sunday-webry.com": "https://www.sunday-webry.com/episode/3269754496551508334",
}

EPISODE_URL = "https://shonenjumpplus.com/episode/10834108156648240735"
NEXT_URL = "https://shonenjumpplus.com/episode/10834108156648240736"
PREV_URL = "https://shonenjumpplus.com/episode/10834108156648240734"


def episode_json(**overrides):
    product = {
        "typeName": "episode",
        "title": "第1話",
        "series": {"title": "SPY×FAMILY"},
        "isPublic": True,
        "hasPurchased": False,
        "prevReadableProductUri": PREV_URL,
        "nextReadableProductUri": NEXT_URL,
        "pageStructure": {
            "pages": [
                {"type": "main", "src": "https://cdn.example/1.jpg", "width": 64, "height": 64},
                {"type": "backMatter"},
                {"type": "main", "src": "https://cdn.example/2.jpg", "width": 64, "height": 64},
            ],
        },
    }
    return {"readableProduct": {**product, **overrides}}


def episode_html(payload):
    value = json.dumps(payload, ensure_ascii=False).replace("&", "&amp;").replace('"', "&quot;")
    return f'<html><body><script id="episode-json" type="text/json" data-value="{value}"></script></body></html>'


def tiled_image(side=DIV * MUL):
    """A DIV x DIV grid of tiles, tile (column, row) painted a colour of its own."""
    image = Image.new("RGB", (side, side))
    tile = side // DIV
    for column in range(DIV):
        for row in range(DIV):
            colour = (column * 60, row * 60, (column + row) * 30)
            image.paste(Image.new("RGB", (tile, tile), colour), (column * tile, row * tile))
    return image


# --- descrambling ---------------------------------------------------------------


def test_descramble_transposes_the_tile_grid():
    original = tiled_image()
    transposed = original.transpose(Image.Transpose.TRANSPOSE)
    assert transposed.tobytes() != original.tobytes()
    assert descramble(transposed).tobytes() == original.tobytes()


def test_descramble_leaves_the_uneven_edge_alone():
    # 35 = 4 * 8 + 3, so a three pixel strip falls outside the shuffled grid.
    image = Image.new("RGB", (35, 35), (10, 20, 30))
    image.putpixel((34, 34), (200, 100, 50))
    assert descramble(image).getpixel((34, 34)) == (200, 100, 50)


# --- urls ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://comic-days.com/series/2550912964574304403/first_episode", True),
        ("https://comic-days.com/episode/2550912964611244527", True),
        ("https://comic-days.com/episode/2550912964611244527.json", True),
        ("https://shonenjumpplus.com/magazine/13932016480028799982", True),
        ("https://shonenjumpplus.com/volume/13932016480028799982", True),
        ("https://shonenjumpplus.com/rss/series/3269632237310729745", True),
        ("https://comic-days.com/series/2550912964574304403", False),
        ("https://comic-days.com/series/first_episode", False),
        ("https://shonenjumpplus.com/rss/series/", False),
        ("http://comic-days.com/episode/2550912964611244527", False),
        ("https://example.com/series/2550912964574304403/first_episode", False),
    ],
)
def test_suitable(url, expected):
    assert GigaViewer.suitable(url) is expected


def test_is_series_means_a_feed():
    assert GigaViewer.is_series("https://shonenjumpplus.com/rss/series/3269632237310729745")
    assert not GigaViewer.is_series(EPISODE_URL)


# --- reading an episode -------------------------------------------------------------


def test_episode_reads_the_titles_the_pages_and_the_next_url(fake_session, fake_response):
    session = fake_session({"/episode/": fake_response(text=episode_html(episode_json()))})
    episode = GigaViewer(session).episode(EPISODE_URL)

    assert episode.series_title == "SPY×FAMILY"
    assert episode.episode_title == "第1話"
    assert [page.url for page in episode.pages] == ["https://cdn.example/1.jpg", "https://cdn.example/2.jpg"]
    assert episode.pages[0].width == 64
    assert (episode.prev_url, episode.next_url) == (PREV_URL, NEXT_URL)
    assert episode.metadata["readableProduct"]["typeName"] == "episode"


def test_episode_strips_a_json_suffix(fake_session, fake_response):
    session = fake_session({"/episode/": fake_response(text=episode_html(episode_json()))})
    episode = GigaViewer(session).episode(EPISODE_URL + ".json")
    assert session.calls == [EPISODE_URL]
    assert episode.url == EPISODE_URL


def test_a_locked_episode_has_no_pages_but_still_names_the_next(fake_session, fake_response):
    payload = episode_json(isPublic=False, hasPurchased=False, pageStructure=None)
    session = fake_session({"/episode/": fake_response(text=episode_html(payload))})
    episode = GigaViewer(session).episode(EPISODE_URL)
    assert episode.pages == ()
    assert episode.next_url == NEXT_URL


def test_a_purchased_episode_is_readable(fake_session, fake_response):
    payload = episode_json(isPublic=False, hasPurchased=True)
    session = fake_session({"/episode/": fake_response(text=episode_html(payload))})
    assert len(GigaViewer(session).episode(EPISODE_URL).pages) == 2


@pytest.mark.parametrize("heading", ['<h1 class="series-header-title">週刊少年ジャンプ</h1>', ""])
def test_a_magazine_is_named_by_the_page_heading_or_its_title(fake_session, fake_response, heading):
    payload = episode_json(typeName="magazine", title="週刊少年ジャンプ 2024年10号", series=None)
    html = episode_html(payload).replace("<body>", f"<body>{heading}")
    session = fake_session({"/magazine/": fake_response(text=html)})
    episode = GigaViewer(session).episode("https://shonenjumpplus.com/magazine/1")
    assert episode.series_title == "週刊少年ジャンプ"
    assert episode.episode_title == "2024年10号"


def test_an_unknown_product_type_is_refused(fake_session, fake_response):
    session = fake_session({"/episode/": fake_response(text=episode_html(episode_json(typeName="mystery")))})
    with pytest.raises(NotAnEpisodePageError, match="unknown typeName"):
        GigaViewer(session).episode(EPISODE_URL)


def test_episode_refuses_a_feed_or_a_series_page(fake_session):
    extractor = GigaViewer(fake_session({}))
    with pytest.raises(UnsupportedUrlError):
        extractor.episode("https://shonenjumpplus.com/rss/series/1")
    with pytest.raises(UnsupportedUrlError):
        extractor.episode("https://shonenjumpplus.com/series/1")


def test_episode_reads_an_unlisted_host_when_forced(fake_session, fake_response):
    # `--extractor gigaviewer` bypasses `suitable()`, so only the path shape is checked here.
    session = fake_session({"/episode/": fake_response(text=episode_html(episode_json()))})
    assert GigaViewer(session).episode("https://unlisted.example/episode/1").episode_title == "第1話"


def test_episode_retries_a_page_without_the_json(fake_session, fake_response, monkeypatch):
    naps = []
    monkeypatch.setattr("getjmanga.extractors.gigaviewer.time.sleep", naps.append)
    session = fake_session(
        {
            "/episode/": [
                fake_response(text="<html><body>placeholder</body></html>"),
                fake_response(text=episode_html(episode_json())),
            ],
        },
    )
    episode = GigaViewer(session).episode(EPISODE_URL)
    assert episode.episode_title == "第1話"
    assert naps == [3]


def test_episode_gives_up_after_three_placeholders(fake_session, fake_response, monkeypatch):
    monkeypatch.setattr("getjmanga.extractors.gigaviewer.time.sleep", lambda _seconds: None)
    session = fake_session({"/episode/": fake_response(text="<html><body>placeholder</body></html>")})
    with pytest.raises(NotAnEpisodePageError, match="temporarily unavailable"):
        GigaViewer(session).episode(EPISODE_URL)
    assert len(session.calls) == 3


def test_episode_refuses_a_non_html_answer(fake_session, fake_response):
    session = fake_session({"/episode/": fake_response(text="{}", content_type="application/json")})
    with pytest.raises(NotAnEpisodePageError, match="not a page"):
        GigaViewer(session).episode(EPISODE_URL)


# --- the feed -------------------------------------------------------------------------

FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>SPY×FAMILY</title>
<item><link> https://shonenjumpplus.com/episode/1 </link></item>
<item><link>https://shonenjumpplus.com/episode/2</link></item>
</channel></rss>
"""


def test_series_urls_reads_the_feed(fake_session, fake_response):
    session = fake_session({"/rss/series/": fake_response(text=FEED)})
    urls = GigaViewer(session).series_urls("https://shonenjumpplus.com/rss/series/1")
    assert urls == ["https://shonenjumpplus.com/episode/1", "https://shonenjumpplus.com/episode/2"]


def test_series_urls_rejects_an_empty_feed(fake_session, fake_response):
    empty = FEED[: FEED.index("<item>")] + "</channel></rss>"
    session = fake_session({"/rss/series/": fake_response(text=empty)})
    with pytest.raises(NotAnEpisodePageError, match="lists no episode"):
        GigaViewer(session).series_urls("https://shonenjumpplus.com/rss/series/1")


def test_series_urls_refuses_an_episode_url(fake_session):
    with pytest.raises(UnsupportedUrlError, match="not a series feed"):
        GigaViewer(fake_session({})).series_urls(EPISODE_URL)


# --- downloading -----------------------------------------------------------------------


def test_download_writes_transposed_pages(fake_session, fake_response, tmp_path):
    raw = BytesIO()
    tiled_image().transpose(Image.Transpose.TRANSPOSE).save(raw, "PNG")
    session = fake_session(
        {
            "/episode/": fake_response(text=episode_html(episode_json())),
            "cdn.example": fake_response(raw.getvalue()),
        },
    )

    result = Downloader(GigaViewer(session), tmp_path).download(EPISODE_URL)

    assert result.status == "saved"
    assert result.save_dir == tmp_path / "shonenjumpplus.com" / "SPY×FAMILY" / "第1話"
    with Image.open(result.save_dir / "0.jpg") as saved:
        # JPEG is lossy, so compare a tile's colour loosely.
        assert saved.getpixel((4, 4)) == pytest.approx((0, 0, 0), abs=8)
        assert saved.getpixel((28, 4)) == pytest.approx((180, 0, 90), abs=8)
    assert session.headers_seen[-1]["Referer"] == EPISODE_URL


# --- logging in -------------------------------------------------------------------------


def test_login_posts_the_credentials_once_per_site(fake_session, fake_response):
    session = fake_session({"/user_account/login": fake_response()})
    extractor = GigaViewer(session)

    extractor.login(EPISODE_URL, "me@example.com", "pw")
    extractor.login(NEXT_URL, "me@example.com", "pw")

    assert len(session.posts) == 1
    url, data = session.posts[0]
    assert url == "https://shonenjumpplus.com/user_account/login"
    assert data["email_address"] == "me@example.com"
    assert data["password"] == "pw"


def test_login_raises_when_the_site_says_no(fake_session, fake_response):
    session = fake_session({"/user_account/login": fake_response(status_code=HTTPStatus.UNAUTHORIZED)})
    with pytest.raises(LoginError, match="refused the credentials"):
        GigaViewer(session).login(EPISODE_URL, "me@example.com", "pw")


# --- the real sites --------------------------------------------------------------------


@pytest.mark.network
@pytest.mark.parametrize("host", TEST_URLS)
def test_site_download(tmp_path, host):
    try:
        result = Downloader(GigaViewer(), tmp_path, only_first=True).download(TEST_URLS[host])
    except HTTPStatusError as error:
        response = error.response
        if response is not None and response.status_code == HTTPStatus.FORBIDDEN:
            pytest.skip(f"{host} refuses requests from this network ({response.status_code}).")
        raise

    assert result.status == "saved"
    assert (result.save_dir / "0.jpg").exists()


@pytest.mark.network
def test_first_episode_download(tmp_path):
    result = Downloader(GigaViewer(), tmp_path, only_first=True).download(
        "https://comic-days.com/series/2550912964574304403/first_episode",
    )
    assert result.status == "saved"


@pytest.mark.network
def test_rss_lists_episodes():
    urls = GigaViewer().series_urls("https://shonenjumpplus.com/rss/series/3269632237310729745")
    assert len(urls) > 1
    assert all(GigaViewer.suitable(url) for url in urls)
