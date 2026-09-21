from __future__ import annotations

from datetime import date
from http import HTTPStatus
from io import BytesIO

import pytest
from PIL import Image

from getjmanga.downloader import Downloader
from getjmanga.errors import NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractors.souffle import AJAX_URL, BASE_URL, PAGE_SIZE, Souffle

SERIES = "chigau-kurasu-no-sukina-hito"
SERIES_URL = f"{BASE_URL}/manga/{SERIES}/"
AUTHOR_URL = f"{BASE_URL}/author/{SERIES}/"
EPISODE_URL = f"{BASE_URL}/manga/{SERIES}/20250827-001/"
NEXT_URL = f"{BASE_URL}/manga/{SERIES}/20250827-002/"
EXPIRED_URL = f"{BASE_URL}/manga/tenmaku-no-ja-dougal/tenmaku043-20260725/"
CDN = f"{BASE_URL}/assets/img/manga/{SERIES}/001"


def episode_html(
    *, pages=("0001.jpg", "0002.jpg"), announce="次回は9月3日更新です。", next_url=NEXT_URL, prev_url=None
):
    """An episode page cut down to what `episode()` reads."""
    images = "\n".join(
        f'<img src="/assets/img/manga/{SERIES}/001/{name}" alt="『違うクラスの好きな人』"><br />' for name in pages
    )
    buttons = ""
    if prev_url:
        buttons += f'<span class="sf-before_btn"><a href="{prev_url}"><img src="/assets/img/before.png"></a></span>'
    if next_url:
        buttons += (
            f'<span class="sf-next_btn"><a href="{next_url}"><img src="/assets/img/contents_next_btn.png"></a></span>'
        )
    return f"""<html><head><title>#1 靴擦れと夏祭り | Souffle（スーフル）</title></head><body>
<article id="{SERIES}"><div id="sf-contents" class="sf-contents-cat_manga">
<div class="sf-content_header">
  <div class="sf-cotent_category"><span class="sf-category"><a href="{BASE_URL}/manga/">マンガ</a></span>
    <span class="sf-date">2025.08.27</span></div>
  <p class="sf-content_book_name"><a href="{AUTHOR_URL}">『違うクラスの好きな人』かわいちひろ</a>
    <span class="sf-book_info">きみと知っていく、初めての気持ち。</span></p>
  <h1>#1 靴擦れと夏祭り</h1>
</div>
<div class="sf-content_manga_scroll line_top">&nbsp;</div>
<div class="sf-content_img">
{images}
</div>
<div class="sf-announce"><p>{announce}</p>
  <p>今後の最新コンテンツが気になる方は、ぜひ<a href="https://x.com/Souffle_life">Souffle公式X</a>をフォロー！</p></div>
<div class="sf-content_before_next_btns">{buttons}</div>
</div></article></body></html>"""


def article(url, title, *, expired=False):
    """One episode card, as the series page and the Ajax Load More plugin write it."""
    date = "【公開終了しました】" if expired else "【2025.08.27公開】"
    css = "sf-content_book_article sf-disable" if expired else "sf-content_book_article"
    return f"""<article class="{css}">
  <a href="{url}"><img src="{BASE_URL}/wp-content/uploads/ec1.jpg" alt="{title}"></a>
  <div class="sf-content_book_description">
    <h4><a href="{AUTHOR_URL}">『違うクラスの好きな人』</a></h4>
    <p><span class="sf-content_release_date">{date}</span><br><a href="{url}">{title}</a></p>
    <div class="sf-contents_book_read"><a href="{url}?utm_source=x">1話を読む</a></div>
  </div>
</article>"""


def series_html(author="178", *cards):
    """A series page: the plugin's empty container plus the cards it renders itself."""
    listing = (
        f'<div id="ajax-load-more" class="ajax-load-more-wrap default" data-id="sf_infinite_article">'
        f'<div aria-live="polite" class="alm-listing alm-ajax" data-repeater="template_2" data-post-type="post"'
        f' data-author="{author}" data-order="DESC" data-orderby="date" data-posts-per-page="3"></div></div>'
        if author
        else ""
    )
    return f"""<html><body><section class="sf-content_books_related">
<h3>『違うクラスの好きな人』他の回を読む</h3>
<div class="sf-content_book_related">{listing}{"".join(cards)}</div>
</section></body></html>"""


def alm_payload(*cards, total=None):
    """What `alm_get_posts` answers, `html` being null when nothing matched."""
    html = "\n".join(cards) or None
    return {"html": html, "meta": {"postcount": len(cards), "totalposts": len(cards) if total is None else total}}


def jpeg_bytes(color=(10, 20, 30), size=(8, 8)):
    raw = BytesIO()
    Image.new("RGB", size, color).save(raw, "JPEG", quality=100)
    return raw.getvalue()


CARDS = [
    article(EPISODE_URL, "#1 靴擦れと夏祭り"),
    article(NEXT_URL, "#2 持ち主は高田くん"),
    article(f"{BASE_URL}/manga/{SERIES}/20250827-003/", "#3 失くした高田くん", expired=True),
]


@pytest.fixture
def client(fake_session, fake_response):
    """A `Souffle` over a session answering a readable episode, an expired one, the images and the series."""

    def build(extra=None):
        routes = {
            "/20250827-001/": fake_response(text=episode_html()),
            "/tenmaku043-20260725/": fake_response(
                text=episode_html(
                    pages=("0001.jpg",),
                    announce="【公開期限が終了しました】",
                    next_url=f"{BASE_URL}/manga/tenmaku-no-ja-dougal/tenmaku044-20260825/",
                    prev_url=f"{BASE_URL}/manga/tenmaku-no-ja-dougal/tenmaku042-20260525/",
                ),
            ),
            "/assets/img/manga/": fake_response(jpeg_bytes(), content_type="image/jpeg"),
            # The series page and its plugin, for where an episode stands; after the episodes, which it matches too.
            "/chigau-kurasu-no-sukina-hito/": fake_response(text=series_html("178", *reversed(CARDS))),
            AJAX_URL: fake_response(payload=alm_payload(*CARDS)),
        }
        # A test's own routes come first (the series route matches its episodes too); anything
        # else on the site is a 404, so an episode of an unknown series goes unnumbered.
        overrides = extra or {}
        unknown = fake_response(text="<html></html>", status_code=HTTPStatus.NOT_FOUND)
        session = fake_session(
            {**overrides, **{key: value for key, value in routes.items() if key not in overrides}, BASE_URL: unknown}
        )
        return Souffle(session), session

    return build


# --- URLs ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        EPISODE_URL,
        f"{BASE_URL}/manga/{SERIES}/20250827-001",
        f"{BASE_URL}/manga/{SERIES}/chigau056-20260916/",
        f"{BASE_URL}/manga/manekineko-no-uta/20190807/",
        f"{BASE_URL}/petitprincess/hanaseijo-rutida/20260901-13/",
        SERIES_URL,
        f"{BASE_URL}/manga/{SERIES}",
        f"{BASE_URL}/petitprincess/hanaseijo-rutida/",
        AUTHOR_URL,
    ],
)
def test_suitable_accepts_episode_and_series_urls(url):
    assert Souffle.suitable(url)


@pytest.mark.parametrize(
    "url",
    [
        f"http://souffle.life/manga/{SERIES}/20250827-001/",
        f"https://www.souffle.life/manga/{SERIES}/20250827-001/",
        f"https://championcross.jp/manga/{SERIES}/20250827-001/",
        f"{BASE_URL}/",
        f"{BASE_URL}/manga/",
        f"{BASE_URL}/manga/page/2/",
        f"{BASE_URL}/manga/feed/",
        f"{BASE_URL}/manga/{SERIES}/feed/",
        f"{BASE_URL}/manga/{SERIES}/page/2/",
        f"{BASE_URL}/author/{SERIES}/feed/",
        f"{BASE_URL}/column/iekatsu-onna-futari-ie-wo-kau/20240101/",
        f"{BASE_URL}/tag/実録エッセイ/",
        f"{BASE_URL}/about/",
    ],
)
def test_suitable_rejects_other_urls(url):
    assert not Souffle.suitable(url)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (SERIES_URL, True),
        (AUTHOR_URL, True),
        (f"{BASE_URL}/petitprincess/hanaseijo-rutida", True),
        (EPISODE_URL, False),
        (f"{BASE_URL}/manga/", False),
        (f"{BASE_URL}/manga/page/2/", False),
    ],
)
def test_is_series(url, expected):
    assert Souffle.is_series(url) is expected


# --- episodes ---------------------------------------------------------------------------


def test_episode_reads_the_titles_and_the_pages(client):
    souffle, session = client()
    episode = souffle.episode(EPISODE_URL)

    assert episode.series_title == "違うクラスの好きな人"
    assert (episode.writer, episode.publisher) == ("かわいちひろ", "秋田書店")
    assert (episode.published, episode.number) == (date(2025, 8, 27), 1)
    assert episode.episode_title == "#1 靴擦れと夏祭り"
    assert [page.url for page in episode.pages] == [f"{CDN}/0001.jpg", f"{CDN}/0002.jpg"]
    assert episode.next_url == NEXT_URL
    assert episode.metadata["book_name"] == "『違うクラスの好きな人』かわいちひろ"
    assert episode.metadata["date"] == "2025.08.27"
    assert episode.metadata["section"] == "manga"
    assert episode.metadata["series_slug"] == SERIES
    assert episode.metadata["episode_id"] == "20250827-001"
    assert episode.metadata["expired"] is False
    assert episode.metadata["prev_url"] is None
    # The episode, then the series page and its plugin for where the episode stands.
    assert session.calls == [EPISODE_URL, SERIES_URL, AJAX_URL]
    assert session.params_seen[0] is None
    assert "User-Agent" in session.headers_seen[0]


def test_episode_at_the_end_of_a_series_has_no_next(client, fake_response):
    souffle, _ = client({"/chigau056-20260916/": fake_response(text=episode_html(next_url=None, prev_url=EPISODE_URL))})
    episode = souffle.episode(f"{BASE_URL}/manga/{SERIES}/chigau056-20260916/")

    assert (episode.prev_url, episode.next_url) == (EPISODE_URL, None)
    assert episode.metadata["prev_url"] == EPISODE_URL


def test_expired_episode_has_no_pages_but_still_a_next(client):
    souffle, _ = client()
    episode = souffle.episode(EXPIRED_URL)

    assert episode.pages == ()
    assert not episode.readable
    assert episode.episode_title == "#1 靴擦れと夏祭り"
    assert episode.next_url == f"{BASE_URL}/manga/tenmaku-no-ja-dougal/tenmaku044-20260825/"
    assert episode.metadata["expired"] is True
    # The preview image the page still shows is kept on record, not downloaded.
    assert episode.metadata["images"] == [f"{CDN}/0001.jpg"]


def test_episode_falls_back_to_the_slugs_when_the_page_names_nothing(client, fake_response):
    bare = '<html><body><div class="sf-content_img"><img src="/assets/img/manga/x/001/0001.jpg"></div></body></html>'
    souffle, _ = client({"/manga/x/20200101/": fake_response(text=bare)})
    episode = souffle.episode(f"{BASE_URL}/manga/x/20200101/")

    assert episode.series_title == "x"
    assert episode.episode_title == "20200101"
    assert [page.url for page in episode.pages] == [f"{BASE_URL}/assets/img/manga/x/001/0001.jpg"]


def test_episode_keeps_a_book_name_without_brackets_as_is(client, fake_response):
    html = episode_html().replace("『違うクラスの好きな人』かわいちひろ", "【特集】")
    souffle, _ = client({"/manga/souffle-special/20200101/": fake_response(text=html)})

    assert souffle.episode(f"{BASE_URL}/manga/souffle-special/20200101/").series_title == "【特集】"


def test_episode_rejects_a_page_without_pages(client, fake_response):
    souffle, _ = client({"/about/": fake_response(text="<html><body><h1>スーフルについて</h1></body></html>")})
    with pytest.raises(NotAnEpisodePageError, match="no pages"):
        souffle.episode(f"{BASE_URL}/about/")


def test_episode_rejects_a_404(client, fake_response):
    souffle, _ = client({"/19990101/": fake_response(text="<html>not found</html>", status_code=HTTPStatus.NOT_FOUND)})
    with pytest.raises(NotAnEpisodePageError, match="no episode"):
        souffle.episode(f"{BASE_URL}/manga/manekineko-no-uta/19990101/")


# --- series -----------------------------------------------------------------------------


@pytest.mark.parametrize("url", [SERIES_URL, AUTHOR_URL])
def test_series_urls_asks_the_plugin_for_the_authors_posts_oldest_first(client, fake_response, url):
    souffle, session = client(
        {
            "/chigau-kurasu-no-sukina-hito/": fake_response(text=series_html("178", *reversed(CARDS))),
            AJAX_URL: fake_response(payload=alm_payload(*CARDS)),
        },
    )
    urls = souffle.series_urls(url)

    assert urls == [EPISODE_URL, NEXT_URL, f"{BASE_URL}/manga/{SERIES}/20250827-003/"]
    assert all(Souffle.suitable(url) for url in urls)
    assert session.calls == [url, AJAX_URL]
    assert session.params_seen[1] == {
        "action": "alm_get_posts",
        "repeater": "template_2",
        "author": "178",
        "order": "ASC",
        "orderby": "date",
        "posts_per_page": PAGE_SIZE,
        "page": 0,
    }
    assert session.headers_seen[1]["Referer"] == url


def test_series_urls_walks_every_page_of_the_listing(client, fake_response):
    first = [article(f"{BASE_URL}/manga/{SERIES}/2025-{n:03d}/", f"#{n}") for n in range(PAGE_SIZE)]
    second = [article(f"{BASE_URL}/manga/{SERIES}/2026-001/", "#101")]
    souffle, session = client(
        {
            "/manga/chigau-kurasu-no-sukina-hito/": fake_response(text=series_html("178")),
            AJAX_URL: [
                fake_response(payload=alm_payload(*first, total=PAGE_SIZE + 1)),
                fake_response(payload=alm_payload(*second, total=PAGE_SIZE + 1)),
            ],
        },
    )
    urls = souffle.series_urls(SERIES_URL)

    assert len(urls) == PAGE_SIZE + 1
    assert urls[-1] == f"{BASE_URL}/manga/{SERIES}/2026-001/"
    assert [params["page"] for params in session.params_seen[1:]] == [0, 1]


def test_series_urls_stops_when_a_page_brings_nothing_new(client, fake_response):
    souffle, session = client(
        {
            "/manga/chigau-kurasu-no-sukina-hito/": fake_response(text=series_html("178")),
            # A site that answered every page with the same posts would loop forever otherwise.
            AJAX_URL: fake_response(payload=alm_payload(*[article(EPISODE_URL, "#1")] * PAGE_SIZE, total=10_000)),
        },
    )
    assert souffle.series_urls(SERIES_URL) == [EPISODE_URL]
    assert len(session.calls) == 3


def test_series_urls_falls_back_to_the_cards_on_the_page(client, fake_response):
    souffle, session = client(
        {"/manga/chigau-kurasu-no-sukina-hito/": fake_response(text=series_html("", *reversed(CARDS)))},
    )
    assert souffle.series_urls(SERIES_URL) == [EPISODE_URL, NEXT_URL, f"{BASE_URL}/manga/{SERIES}/20250827-003/"]
    assert session.calls == [SERIES_URL]


def test_series_urls_falls_back_when_the_plugin_answers_nothing(client, fake_response):
    souffle, _ = client(
        {
            "/manga/chigau-kurasu-no-sukina-hito/": fake_response(text=series_html("999", *reversed(CARDS[:2]))),
            AJAX_URL: fake_response(payload=alm_payload()),
        },
    )
    assert souffle.series_urls(SERIES_URL) == [EPISODE_URL, NEXT_URL]


def test_series_urls_raises_on_an_empty_listing(client, fake_response):
    souffle, _ = client(
        {
            "/manga/nonexistent-slug/": fake_response(text=series_html("999")),
            AJAX_URL: fake_response(payload=alm_payload()),
        },
    )
    with pytest.raises(NotAnEpisodePageError, match="lists no episode"):
        souffle.series_urls(f"{BASE_URL}/manga/nonexistent-slug/")


def test_series_urls_raises_on_a_404(client, fake_response):
    souffle, _ = client({"/manga/gone/": fake_response(text="", status_code=HTTPStatus.NOT_FOUND)})
    with pytest.raises(NotAnEpisodePageError, match="no series"):
        souffle.series_urls(f"{BASE_URL}/manga/gone/")


def test_series_urls_rejects_an_episode_url(client):
    souffle, _ = client()
    with pytest.raises(UnsupportedUrlError, match="not a series page"):
        souffle.series_urls(EPISODE_URL)


# --- downloading ------------------------------------------------------------------------


def test_download_writes_the_pages_as_served(client, tmp_path):
    souffle, session = client()
    result = Downloader(souffle, tmp_path).download(EPISODE_URL)

    assert result.status == "saved"
    assert result.save_dir == tmp_path / "souffle.life" / "違うクラスの好きな人" / "#1 靴擦れと夏祭り"
    assert sorted(path.name for path in result.save_dir.iterdir()) == ["0.jpg", "1.jpg"]
    with Image.open(result.save_dir / "0.jpg") as saved:
        assert saved.size == (8, 8)
        assert saved.getpixel((4, 4)) == pytest.approx((10, 20, 30), abs=8)
    assert session.headers_seen[-1]["Referer"] == EPISODE_URL


# --- the real site ----------------------------------------------------------------------

# One episode per known host, free to read without an account.
TEST_URLS: dict[str, str] = {
    "souffle.life": "https://souffle.life/manga/chigau-kurasu-no-sukina-hito/20250827-001/",
}


@pytest.mark.network
@pytest.mark.parametrize("host", TEST_URLS)
def test_site_download(tmp_path, host):
    result = Downloader(Souffle(), tmp_path, only_first=True).download(TEST_URLS[host])
    assert result.status == "saved"
    assert (result.save_dir / "0.jpg").stat().st_size > 0


@pytest.mark.network
def test_series_page_lists_episodes():
    urls = Souffle().series_urls("https://souffle.life/manga/chigau-kurasu-no-sukina-hito/")
    assert urls[0] == TEST_URLS["souffle.life"]
    assert all(Souffle.suitable(url) for url in urls)
