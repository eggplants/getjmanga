from __future__ import annotations

from datetime import date
from http import HTTPStatus
from io import BytesIO

import pytest
from httpx2 import HTTPStatusError
from PIL import Image

from getjmanga.downloader import Downloader
from getjmanga.errors import NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractors.nora import Nora
from getjmanga.viewers.kmanga import GRID, UNIT, service_hash, tile_order

WORK_URL = "https://nora.gakken.jp/comic/page-buttigumi/"
EPISODE_URL = f"{WORK_URL}?episode_id=142"
NEXT_URL = f"{WORK_URL}?episode_id=319"
HIDDEN_URL = f"{WORK_URL}?episode_id=700"
SERIES_TITLE = (
    "ブッチ組 ～夜露死苦異世界 我等真武親友 悪鬼滅殺仏血義礼組 "
    "（よろしくいせかい われらまぶだち あっきめっさつぶっちぎれぐみ）～"
)
SEED = 284797878

VIEWER_API = "https://api.nora.gakken.jp/web/episode/viewer"
PAGE_1 = "https://cdn.nora.gakken.jp/static/web_titles/20/episodes/142/3e1e6e0b.jpg?Expires=1789672044&Signature=x"
PAGE_2 = "https://cdn.nora.gakken.jp/static/web_titles/20/episodes/142/cd2e3a29.jpg?Expires=1789672044&Signature=y"


def work_page(*, episode_id=142, sub="", main="特別予告編", date="2026/05/28", published=True, listing=True):
    """A work page as the WordPress theme writes it, embedding one episode's viewer."""
    viewer = (
        f"<iframe id=\"viewer-iframe\" src='/comics/minfunction/title/00020/episode/{episode_id}/embedviewer'"
        " class='page-comic-detail-viewer-iframe' title='コミックビューワー'></iframe>"
        if published
        else ""
    )
    items = (
        """
        <li class="article-list-item"><a href="?episode_id=142&#038;orderby=asc"><article>
          <p class="article-list-item-episode-number-name"></p><h2 class="article-list-item-heading">特別予告編</h2>
        </article></a></li>
        <li class="article-list-item"><a href="?episode_id=319&#038;orderby=asc"><article>
          <h2 class="article-list-item-heading">一発目</h2></article></a></li>
        <li class="article-list-item article-list-item--disabled"><a ><article>
          <ul class="article-list-item-chips"><li class="article-list-item-chip">
            <p class="chip chip--disabled chip--sm">非公開</p></li></ul>
          <h2 class="article-list-item-heading">二発目</h2></article></a></li>
        <li class="article-list-item"><a href="?episode_id=553&#038;orderby=asc"><article>
          <h2 class="article-list-item-heading">設定資料</h2></article></a></li>
        <li class="article-list-item"><a href="?episode_id=142&#038;orderby=asc"><article>
          <h2 class="article-list-item-heading">特別予告編 (listed twice)</h2></article></a></li>
        """
        if listing
        else ""
    )
    return f"""
<html><head><title>{SERIES_TITLE} | コミックノーラ</title></head><body>
<article class="page-comic-detail">
  <div class='page-comic-detail-viewer' data-current-episode-published="{int(published)}">{viewer}</div>
  <div class="page-comic-detail-episode-title"><h1 class="page-comic-detail-episode-title-container"><div>
    <span class="page-comic-detail-episode-title-text">
      <span class="page-comic-detail-episode-title-text__sub">
        {sub}          </span>
      <span class="page-comic-detail-episode-title-text__main">
        {main}          </span>
    </span>
    <span class="page-comic-detail-episode-title__date">
      {date}        </span>
  </div></h1></div>
  <div class='page-comic-detail-container'>
    <header class="page-comic-detail-content-header">
      <p class="page-comic-detail-content-header-title">
        {SERIES_TITLE}          </p>
      <dl class="page-comic-detail-content-header__name">
        <dt>作家名：</dt>
        <dd>
                          林家志弦（漫画）                        </dd>
      </dl>
    </header>
    <div class='page-comic-detail-content'>
      <nav aria-label="ソート順" class="page-comic-detail-sort"><ul class="page-comic-detail-sort-list">
        <li><a href="?episode_id={episode_id}&#038;orderby=asc" class="button button--sort">1話から</a></li>
        <li><a href="?episode_id={episode_id}&#038;orderby=desc" class="button button--sort">最新話から</a></li>
      </ul></nav>
      <section class="contents-container contents-container--article-list">
        <ul class="article-list">{items}</ul>
      </section>
    </div>
  </div>
</article>
</body></html>
"""


def viewer_payload(*, seed=SEED, next_id=319, pages=(PAGE_1, PAGE_2)):
    """What `/web/episode/viewer` answers for a readable episode."""
    return {
        "status": "success",
        "response_code": 0,
        "error_message": "",
        "title_id": 20,
        "episode_id": 142,
        "scramble_seed": seed,
        "page_list": list(pages),
        "previous_episode": {"title_id": 20, "episode_id": 100, "age_rating": None},
        "next_episode": {"title_id": 20, "episode_id": next_id, "age_rating": None} if next_id else None,
        "title_name": "ブッチ組～夜露死苦異世界 我等真武親友 悪鬼滅殺仏血義礼組",
        "episode_name": "特別予告編",
    }


UNRELEASED_PAYLOAD = {
    "status": "error",
    "response_code": 3104,
    "server_time": "2026-09-18 03:58:48",
    "error_message": "episode unreleased.",
}


@pytest.fixture
def client(fake_session):
    def make(routes):
        session = fake_session(routes)
        return Nora(session), session

    return make


def tile_image(order, tile=(UNIT * 3, UNIT * 2), extra=(5, 3)):
    """A `GRID x GRID` image of flat-coloured tiles laid out in `order`, plus an edge strip."""
    width, height = tile[0] * GRID + extra[0], tile[1] * GRID + extra[1]
    image = Image.new("L", (width, height), 255)
    for destination, source in enumerate(order):
        x, y = destination % GRID * tile[0], destination // GRID * tile[1]
        image.paste(source * 10 + 10, (x, y, x + tile[0], y + tile[1]))
    return image


def scrambled_layout(seed=SEED):
    """Where the served page keeps each tile: the inverse of `tile_order()`.

    The viewer draws served tile `order[d]` at destination `d`, so the served page
    holds the page's tile `d` at position `order[d]`.
    """
    layout = [0] * (GRID * GRID)
    for destination, source in enumerate(tile_order(seed)):
        layout[source] = destination
    return layout


# --- URLs ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        WORK_URL,
        EPISODE_URL,
        "https://nora.gakken.jp/comic/page-buttigumi",
        "https://nora.gakken.jp/comic/page-buttigumi/?episode_id=142&orderby=desc",
        "https://nora.gakken.jp/comic/page-1960714400/?orderby=asc",
    ],
)
def test_suitable_accepts_work_page_urls(url):
    assert Nora.suitable(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://nora.gakken.jp/comic/page-buttigumi/",
        "https://nora.gakken.jp/",
        "https://nora.gakken.jp/comic/",
        "https://nora.gakken.jp/comic/tag/original",
        "https://nora.gakken.jp/books/",
        "https://nora.gakken.jp/comics/minfunction/title/00020/episode/1173/embedviewer",
        "https://gakcomic.gakken.jp/comic/page-1020632500/",
    ],
)
def test_suitable_rejects_other_urls(url):
    assert not Nora.suitable(url)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (WORK_URL, True),
        ("https://nora.gakken.jp/comic/page-buttigumi/?orderby=asc", True),
        (EPISODE_URL, False),
        ("https://nora.gakken.jp/comic/page-buttigumi/?episode_id=142&orderby=desc", False),
        ("https://nora.gakken.jp/comic/", False),
    ],
)
def test_is_series_is_a_work_page_without_an_episode_picked(url, expected):
    assert Nora.is_series(url) is expected


# --- episodes -----------------------------------------------------------------------


def test_episode_reads_the_titles_the_pages_and_the_next_episode(client, fake_response):
    gakken, session = client(
        {
            "/comic/page-buttigumi/": fake_response(text=work_page()),
            VIEWER_API: fake_response(payload=viewer_payload(), content_type="application/json"),
        },
    )
    episode = gakken.episode(EPISODE_URL)

    assert episode.url == EPISODE_URL
    assert episode.series_title == SERIES_TITLE
    assert (episode.writer, episode.publisher) == ("林家志弦（漫画）", "Gakken")
    assert (episode.published, episode.number) == (date(2026, 5, 28), 1)
    assert episode.episode_title == "特別予告編"
    assert [page.url for page in episode.pages] == [PAGE_1, PAGE_2]
    assert all(page.extra == {"seed": SEED} for page in episode.pages)
    assert (episode.prev_url, episode.next_url) == (f"{WORK_URL}?episode_id=100", NEXT_URL)
    assert episode.metadata["published"] is True
    assert episode.metadata["date"] == "2026/05/28"
    assert episode.metadata["viewer"]["episode_id"] == 142

    # The work page is asked for with the episode picked, then the API is signed
    # the viewer's way, then the work page again, oldest first, for the number.
    assert session.calls == [WORK_URL, VIEWER_API, WORK_URL]
    assert session.params_seen[0] == {"episode_id": 142}
    assert session.params_seen[1] == {"version": "6.0.0", "platform": "3", "episode_id": "142"}
    assert session.params_seen[2] == {"orderby": "asc"}
    headers = session.headers_seen[1]
    assert headers["x-com-sega-md-hash"] == service_hash(session.params_seen[1])
    assert headers["x-com-sega-md-is-crawler"] == "false"
    assert headers["Origin"] == "https://nora.gakken.jp"
    assert "User-Agent" in headers


def test_episode_joins_the_number_and_the_name_of_an_episode(client, fake_response):
    gakken, _ = client(
        {
            "/comic/page-koikoite/": fake_response(
                text=work_page(episode_id=1163, sub="第8話", main="いまだ干なくに（前編）")
            ),
            VIEWER_API: fake_response(payload=viewer_payload(seed=None, next_id=None), content_type="application/json"),
        },
    )
    episode = gakken.episode("https://nora.gakken.jp/comic/page-koikoite/?episode_id=1163")

    assert episode.episode_title == "第8話 いまだ干なくに（前編）"
    assert episode.next_url is None
    # No seed: nothing for `image()` to undo.
    assert all(page.extra == {} for page in episode.pages)


def test_episode_without_a_query_reads_the_one_the_work_page_embeds(client, fake_response):
    gakken, session = client(
        {
            "/comic/page-buttigumi/": fake_response(text=work_page(episode_id=1173, main="六発目")),
            VIEWER_API: fake_response(payload=viewer_payload(), content_type="application/json"),
        },
    )
    episode = gakken.episode("https://nora.gakken.jp/comic/page-buttigumi")

    assert episode.url == f"{WORK_URL}?episode_id=1173"
    assert episode.episode_title == "六発目"
    assert session.calls[0] == WORK_URL
    assert session.params_seen[0] is None
    assert session.params_seen[1]["episode_id"] == "1173"


def test_hidden_episode_has_no_pages_and_asks_the_api_nothing(client, fake_response):
    # `非公開`, or no such id: the work page comes back with an empty viewer.
    gakken, session = client(
        {"/comic/page-buttigumi/": fake_response(text=work_page(published=False, main="", date=""))}
    )
    episode = gakken.episode(HIDDEN_URL)

    assert episode.pages == ()
    assert episode.next_url is None
    assert episode.series_title == SERIES_TITLE
    assert episode.episode_title == "700"
    assert episode.metadata["published"] is False
    assert episode.metadata["viewer"] is None
    assert episode.number is None
    assert session.calls == [WORK_URL, WORK_URL]


def test_episode_the_api_calls_unreleased_has_no_pages(client, fake_response):
    gakken, _ = client(
        {
            "/comic/page-buttigumi/": fake_response(text=work_page()),
            VIEWER_API: fake_response(
                payload=UNRELEASED_PAYLOAD,
                status_code=HTTPStatus.BAD_REQUEST,
                content_type="application/json",
            ),
        },
    )
    episode = gakken.episode(EPISODE_URL)

    assert episode.pages == ()
    assert episode.next_url is None
    assert episode.episode_title == "特別予告編"
    assert episode.metadata["viewer"] == {}


def test_other_api_errors_propagate(client, fake_response):
    gakken, _ = client(
        {
            "/comic/page-buttigumi/": fake_response(text=work_page()),
            VIEWER_API: fake_response(text="", status_code=HTTPStatus.INTERNAL_SERVER_ERROR),
        },
    )
    with pytest.raises(HTTPStatusError):
        gakken.episode(EPISODE_URL)


def test_page_without_a_work_is_not_an_episode(client, fake_response):
    gakken, _ = client({"/comic/page-": fake_response(text="<html><body><h1>404 Not Found</h1></body></html>")})
    with pytest.raises(NotAnEpisodePageError, match="no work page"):
        gakken.episode("https://nora.gakken.jp/comic/page-nothing/?episode_id=1")


def test_work_page_that_embeds_nothing_is_not_an_episode_without_a_query(client, fake_response):
    gakken, _ = client({"/comic/page-buttigumi/": fake_response(text=work_page(published=False))})
    with pytest.raises(NotAnEpisodePageError, match="embeds no episode"):
        gakken.episode(WORK_URL)


# --- series -------------------------------------------------------------------------


def test_series_urls_lists_the_readable_episodes_in_page_order_deduplicated(client, fake_response):
    gakken, session = client({"/comic/page-buttigumi/": fake_response(text=work_page())})
    urls = gakken.series_urls(WORK_URL)

    assert urls == [EPISODE_URL, NEXT_URL, f"{WORK_URL}?episode_id=553"]
    assert all(Nora.suitable(url) and not Nora.is_series(url) for url in urls)
    # Asked for oldest first, so the page order is the download order.
    assert session.calls == [WORK_URL]
    assert session.params_seen == [{"orderby": "asc"}]


def test_series_urls_raises_on_an_empty_listing(client, fake_response):
    gakken, _ = client({"/comic/page-buttigumi/": fake_response(text=work_page(listing=False))})
    with pytest.raises(NotAnEpisodePageError, match="no readable episode"):
        gakken.series_urls(WORK_URL)


def test_series_urls_raises_without_a_work_page(client, fake_response):
    gakken, _ = client({"/comic/page-": fake_response(text="<html><body>404</body></html>")})
    with pytest.raises(NotAnEpisodePageError, match="no work page"):
        gakken.series_urls("https://nora.gakken.jp/comic/page-nothing/")


@pytest.mark.parametrize("url", [EPISODE_URL, "https://nora.gakken.jp/comic/"])
def test_series_urls_refuses_a_url_that_is_not_a_work_page(client, url):
    gakken, session = client({})
    with pytest.raises(UnsupportedUrlError):
        gakken.series_urls(url)
    assert session.calls == []


# --- downloading --------------------------------------------------------------------


def _png(image):
    raw = BytesIO()
    image.save(raw, "PNG")
    return raw.getvalue()


def test_download_writes_the_first_page_descrambled(client, fake_response, tmp_path):
    scrambled = tile_image(scrambled_layout())
    gakken, session = client(
        {
            "/comic/page-buttigumi/": fake_response(text=work_page()),
            VIEWER_API: fake_response(payload=viewer_payload(), content_type="application/json"),
            "3e1e6e0b.jpg": fake_response(_png(scrambled), content_type="image/png"),
        },
    )
    result = Downloader(gakken, tmp_path, only_first=True).download(EPISODE_URL)

    assert result.status == "saved"
    assert result.save_dir == tmp_path / "nora.gakken.jp" / SERIES_TITLE / "特別予告編"
    written = Image.open(result.save_dir / "0.jpg").convert("L")
    expected = tile_image(range(GRID * GRID))
    assert written.size == expected.size
    # Flat tiles survive the JPEG round trip within a few levels; compare the tile centres.
    for index in range(GRID * GRID):
        x, y = index % GRID * UNIT * 3 + UNIT, index // GRID * UNIT * 2 + UNIT // 2
        assert abs(written.getpixel((x, y)) - expected.getpixel((x, y))) <= 3
    assert not (result.save_dir / "1.jpg").exists()
    assert session.calls[-1] == PAGE_1
    assert session.headers_seen[-1]["Referer"] == EPISODE_URL


# --- logging in ---------------------------------------------------------------------


# --- the real site ------------------------------------------------------------------

# One episode per known host, free to read without an account: the scrambled
# `特別予告編` of ブッチ組, the first episode of a running series.
TEST_URLS: dict[str, str] = {
    "nora.gakken.jp": "https://nora.gakken.jp/comic/page-buttigumi/?episode_id=142",
}


@pytest.mark.network
@pytest.mark.parametrize("host", TEST_URLS)
def test_site_download(tmp_path, host):
    result = Downloader(Nora(), tmp_path, only_first=True).download(TEST_URLS[host])
    assert result.status == "saved"
    assert (result.save_dir / "0.jpg").stat().st_size > 0


@pytest.mark.network
def test_work_page_lists_episodes_oldest_first():
    urls = Nora().series_urls(WORK_URL)
    assert urls[0] == TEST_URLS["nora.gakken.jp"]
    assert all(Nora.suitable(url) for url in urls)


@pytest.mark.network
def test_hidden_episode_is_locked_on_the_site():
    episode = Nora().episode(HIDDEN_URL)
    assert episode.pages == ()
    assert episode.metadata["published"] is False
