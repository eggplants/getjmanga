from __future__ import annotations

from http import HTTPStatus
from io import BytesIO

import pytest
from PIL import Image

from getjmanga.downloader import Downloader
from getjmanga.extractors.comicessay import BASE_URL, LISTING_LIMIT, ComicEssay
from getjmanga.extractors.common import NotAnEpisodePageError, UnsupportedUrlError

SERIES = "a1063"
SERIES_URL = f"{BASE_URL}/episode/{SERIES}/"
EPISODE_URL = f"{BASE_URL}/read/{SERIES}/entry-53088.html"
NEXT_URL = f"{BASE_URL}/read/{SERIES}/entry-53089.html"
LAST_URL = f"{BASE_URL}/read/{SERIES}/entry-53092.html"
OLD_URL = f"{BASE_URL}/read/6/2763.html"
ARCHIVE = f"{BASE_URL}/archives/019/202608"


def episode_html(
    *,
    series="ちゃんぺんとママぺんの平凡だけど幸せな日々 4",
    number="第1話",
    heading="だって揚げパンだから",
    pages=("ef92.jpg", "1c9d.jpg"),
    next_url=NEXT_URL,
    prev_url=None,
):
    """A reading page cut down to what `episode()` reads."""
    images = "\n".join(
        f'<div class="episode-comic__image">\n<img alt="" src="/archives/019/202608/{name}" '
        'onselectstart="return false;" onmousedown="return false;" oncontextmenu="return false;" />\n</div>'
        for name in pages
    )
    buttons = '<div class="btn-wrap__item">'
    if prev_url:
        buttons += f'<a class="c-btn _btn-pager-left js-viewing-indelible" href="{prev_url}">前の話へ</a>'
    buttons += '</div><div class="btn-wrap__item">'
    if next_url:
        buttons += f'<a class="c-btn _btn-pager-right js-viewing-indelible" href="{next_url}">次の話へ</a>'
    buttons += "</div>"
    return f"""<!DOCTYPE html><html lang="ja"><head>
<title>{number}　{heading} | {series} | 漫画掲載ページ | コミックエッセイ劇場</title></head>
<body data-blog="read">
<header class="g-header"><a href="{BASE_URL}/"><img alt="コミックエッセイ劇場" src="/themes/logo.png" /></a></header>
<div class="episode-body"><div class="inner-width">
<div class="episode-detail">
<div class="episode-detail__title"> {series} </div>
<div class="episode-detail__episode-num">{number}</div>
<h1 class="episode-detail__episode-ttl">{heading}</h1>
<div class="episode-comic">
{images}
</div>
<div class="c-mt40"><div class="btn-wrap _btn-col-2">{buttons}</div></div>
<div class="c-mt20"><a class="c-btn _btn-wide" href="{BASE_URL}/episode/{SERIES}/">作品詳細はこちら</a></div>
</div>
</div></div>
<div class="book-detail-list__thum"><img src="https://cdn.kdkw.jp/cover_1000/322605/322605001099.jpg" alt="cover"></div>
</body></html>"""


def series_html(items, *, new=None):
    """A work page: the NEW entry in a list of its own, then the rest newest first."""
    lists = ""
    if new:
        url, title = new
        lists += f"""<ul class="episode-list">
<li class="episode-list__item _new" data-num="1" total-num="">
<a class="episode-list__item--link" href="{url}">
<div class="episode-list__item--label"> NEW </div>
<div class="episode-list__item--title">{title}</div>
</a></li></ul>"""
    lists += '<ul class="episode-list">'
    for index, (url, title) in enumerate(items, 1):
        lists += f"""<li class="episode-list__item _list" data-num="{index}" total-num="">
<a class="episode-list__item--link" href="{url}#top">
<div class="episode-list__item--title">{title}</div>
</a></li>"""
    lists += "</ul>"
    return f"""<!DOCTYPE html><html lang="ja"><head>
<title>ちゃんぺんとママぺんの平凡だけど幸せな日々 4 | 連載 | コミックエッセイ劇場</title></head>
<body data-blog="episode">
<header class="g-header"><a href="{BASE_URL}/read/6/entry-52930.html">今週の一本</a></header>
<div class="episode-info">
<h1><div class="episode-info__title">ちゃんぺんとママぺんの平凡だけど幸せな日々 4</div></h1>
</div>
<div class="detail-head"> 作品を読む </div>
{lists}
<div class="detail-head"> 作者紹介 </div>
</body></html>"""


NOT_FOUND_HTML = """<html><head><title>お探しのページは見つかりませんでした | コミックエッセイ劇場</title></head>
<body><div class="notfound__title"> お探しのページが見つかりません </div></body></html>"""


def jpeg_bytes(size=(8, 8), color=(10, 20, 30)):
    buffer = BytesIO()
    Image.new("RGB", size, color).save(buffer, format="JPEG")
    return buffer.getvalue()


@pytest.fixture
def client(fake_session, fake_response):
    """A `ComicEssay` on a scripted session; `extra` routes take precedence."""

    def build(extra=None):
        routes = {
            "/entry-53088.html": fake_response(text=episode_html()),
            "/entry-53092.html": fake_response(
                text=episode_html(
                    number="第5話",
                    heading="ママは見える所にいてほしい",
                    pages=("aaaa.jpg",),
                    next_url=None,
                    prev_url=f"{BASE_URL}/read/{SERIES}/entry-53091.html",
                ),
            ),
            "/archives/": fake_response(jpeg_bytes(), content_type="image/jpeg"),
        }
        session = fake_session({**routes, **(extra or {})})
        return ComicEssay(session), session

    return build


# --- URLs ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        EPISODE_URL,
        OLD_URL,
        f"{BASE_URL}/read/314/entry-27888.html",
        SERIES_URL,
        f"{BASE_URL}/episode/{SERIES}",
        f"{BASE_URL}/episode/6/",
        f"{BASE_URL}/episode/314/",
    ],
)
def test_suitable_accepts_episode_and_series_urls(url):
    assert ComicEssay.suitable(url)


@pytest.mark.parametrize(
    "url",
    [
        f"http://www.comic-essay.com/read/{SERIES}/entry-53088.html",
        f"https://comic-essay.com/read/{SERIES}/entry-53088.html",
        f"https://souffle.life/read/{SERIES}/entry-53088.html",
        f"{BASE_URL}/",
        f"{BASE_URL}/comics/",
        f"{BASE_URL}/comics/animal/",
        f"{BASE_URL}/episode/",
        f"{BASE_URL}/episode/314/page/2/",
        f"{BASE_URL}/read/314/",
        f"{BASE_URL}/read/314/rss2.xml",
        f"{BASE_URL}/read/{SERIES}/entry-53088",
        f"{BASE_URL}/news/entry-1.html",
        f"{BASE_URL}/book/322605001099.html",
        f"{BASE_URL}/links/a/entry-40695.html",
    ],
)
def test_suitable_rejects_other_urls(url):
    assert not ComicEssay.suitable(url)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (SERIES_URL, True),
        (f"{BASE_URL}/episode/6", True),
        (EPISODE_URL, False),
        (OLD_URL, False),
        (f"{BASE_URL}/comics/", False),
        (f"{BASE_URL}/episode/314/page/2/", False),
    ],
)
def test_is_series(url, expected):
    assert ComicEssay.is_series(url) is expected


# --- episodes ---------------------------------------------------------------------------


def test_episode_reads_the_titles_and_the_pages(client):
    comicessay, session = client()
    episode = comicessay.episode(EPISODE_URL)

    assert episode.series_title == "ちゃんぺんとママぺんの平凡だけど幸せな日々 4"
    assert episode.episode_title == "第1話　だって揚げパンだから"
    assert [page.url for page in episode.pages] == [f"{ARCHIVE}/ef92.jpg", f"{ARCHIVE}/1c9d.jpg"]
    assert all(page.extra == {} for page in episode.pages)
    assert episode.next_url == NEXT_URL
    assert episode.metadata == {
        "series_id": SERIES,
        "episode_id": "entry-53088",
        "number": "第1話",
        "heading": "だって揚げパンだから",
        "series_url": SERIES_URL,
        "prev_url": None,
        "images": [f"{ARCHIVE}/ef92.jpg", f"{ARCHIVE}/1c9d.jpg"],
    }
    assert session.calls == [EPISODE_URL]
    assert session.params_seen == [None]
    assert "User-Agent" in session.headers_seen[0]


def test_episode_at_the_end_of_a_series_has_no_next(client):
    comicessay, _ = client()
    episode = comicessay.episode(LAST_URL)

    assert episode.episode_title == "第5話　ママは見える所にいてほしい"
    assert episode.next_url is None
    assert episode.metadata["prev_url"] == f"{BASE_URL}/read/{SERIES}/entry-53091.html"


def test_episode_takes_the_older_url_shape(client, fake_response):
    comicessay, _ = client(
        {"/read/6/2763.html": fake_response(text=episode_html(series="ねことじいちゃん＋番外編", number="番外編40"))},
    )
    episode = comicessay.episode(OLD_URL)

    assert episode.series_title == "ねことじいちゃん＋番外編"
    assert episode.episode_title == "番外編40　だって揚げパンだから"
    assert episode.metadata["series_id"] == "6"
    assert episode.metadata["episode_id"] == "2763"
    assert episode.metadata["series_url"] == f"{BASE_URL}/episode/6/"


def test_episode_without_images_has_no_pages_but_still_a_next(client, fake_response):
    comicessay, _ = client({"/entry-1.html": fake_response(text=episode_html(pages=()))})
    episode = comicessay.episode(f"{BASE_URL}/read/{SERIES}/entry-1.html")

    assert episode.pages == ()
    assert not episode.readable
    assert episode.next_url == NEXT_URL


def test_episode_falls_back_to_the_ids_when_the_page_names_nothing(client, fake_response):
    bare = (
        '<html><body><div class="episode-detail"><div class="episode-comic__image">'
        '<img src="/archives/x/0001.jpg"></div></div></body></html>'
    )
    comicessay, _ = client({"/read/x/entry-9.html": fake_response(text=bare)})
    episode = comicessay.episode(f"{BASE_URL}/read/x/entry-9.html")

    assert episode.series_title == "x"
    assert episode.episode_title == "entry-9"
    assert [page.url for page in episode.pages] == [f"{BASE_URL}/archives/x/0001.jpg"]
    assert episode.next_url is None


@pytest.mark.parametrize("html", [series_html([(EPISODE_URL, "第1話")]), NOT_FOUND_HTML])
def test_episode_rejects_a_page_without_the_reading_block(client, fake_response, html):
    # A work page, or the not-found page served as 200.
    comicessay, _ = client({"/read/314/": fake_response(text=html)})
    with pytest.raises(NotAnEpisodePageError, match="no comic"):
        comicessay.episode(f"{BASE_URL}/read/314/entry-1.html")


def test_episode_rejects_a_404(client, fake_response):
    comicessay, _ = client({"/entry-1.html": fake_response(text=NOT_FOUND_HTML, status_code=HTTPStatus.NOT_FOUND)})
    with pytest.raises(NotAnEpisodePageError, match="no episode"):
        comicessay.episode(f"{BASE_URL}/read/314/entry-1.html")


# --- series -----------------------------------------------------------------------------


def episode_url(number):
    return f"{BASE_URL}/read/{SERIES}/entry-{53087 + number}.html"


def test_series_urls_lists_the_episodes_oldest_first(client, fake_response):
    listing = [(episode_url(number), f"第{number}話") for number in (4, 3, 2, 1)]
    # The newest episode is linked twice: once as NEW, once in the list.
    comicessay, session = client(
        {"/episode/": fake_response(text=series_html([(LAST_URL, "第5話"), *listing], new=(LAST_URL, "第5話")))},
    )

    assert comicessay.series_urls(SERIES_URL) == [episode_url(number) for number in range(1, 6)]
    assert session.calls == [SERIES_URL]


def test_series_urls_walks_back_from_the_oldest_listed_episode_when_the_list_is_full(client, fake_response):
    listing = [(episode_url(number), f"第{number}話") for number in range(LISTING_LIMIT + 2, 2, -1)]
    routes = {
        "/episode/": fake_response(text=series_html(listing[1:], new=listing[0])),
        "/entry-53090.html": fake_response(text=episode_html(number="第3話", prev_url=episode_url(2))),
        "/entry-53089.html": fake_response(text=episode_html(number="第2話", prev_url=episode_url(1))),
        "/entry-53088.html": fake_response(text=episode_html(number="第1話", prev_url=None)),
    }
    comicessay, session = client(routes)

    assert comicessay.series_urls(SERIES_URL) == [episode_url(number) for number in range(1, LISTING_LIMIT + 3)]
    assert session.calls == [SERIES_URL, episode_url(3), episode_url(2), episode_url(1)]


def test_series_urls_stops_walking_at_an_episode_already_listed(client, fake_response):
    listing = [(episode_url(number), f"第{number}話") for number in range(LISTING_LIMIT, 0, -1)]
    routes = {
        "/episode/": fake_response(text=series_html(listing)),
        # The oldest listed episode points back at one the list already had.
        "/entry-53088.html": fake_response(text=episode_html(prev_url=episode_url(2))),
    }
    comicessay, session = client(routes)

    assert comicessay.series_urls(SERIES_URL) == [episode_url(number) for number in range(1, LISTING_LIMIT + 1)]
    assert session.calls == [SERIES_URL, episode_url(1)]


def test_series_urls_raises_on_an_empty_listing(client, fake_response):
    comicessay, _ = client({"/episode/": fake_response(text=series_html([]))})
    with pytest.raises(NotAnEpisodePageError, match="lists no episode"):
        comicessay.series_urls(SERIES_URL)


def test_series_urls_raises_on_a_404(client, fake_response):
    comicessay, _ = client({"/episode/": fake_response(text=NOT_FOUND_HTML, status_code=HTTPStatus.NOT_FOUND)})
    with pytest.raises(NotAnEpisodePageError, match="no work"):
        comicessay.series_urls(f"{BASE_URL}/episode/99999/")


def test_series_urls_rejects_an_episode_url(client):
    comicessay, _ = client()
    with pytest.raises(UnsupportedUrlError, match="not a work page"):
        comicessay.series_urls(EPISODE_URL)


# --- downloading ------------------------------------------------------------------------


def test_download_writes_the_pages_as_served(client, tmp_path):
    comicessay, session = client()
    result = Downloader(comicessay, tmp_path).download(EPISODE_URL)

    assert result.status == "saved"
    assert result.save_dir == tmp_path / "ちゃんぺんとママぺんの平凡だけど幸せな日々 4" / "第1話　だって揚げパンだから"
    assert sorted(path.name for path in result.save_dir.iterdir()) == ["0.jpg", "1.jpg"]
    with Image.open(result.save_dir / "0.jpg") as saved:
        assert saved.size == (8, 8)
        assert saved.getpixel((4, 4)) == pytest.approx((10, 20, 30), abs=8)
    assert session.calls[1:] == [f"{ARCHIVE}/ef92.jpg", f"{ARCHIVE}/1c9d.jpg"]
    assert session.headers_seen[-1]["Referer"] == EPISODE_URL


# --- the real site ----------------------------------------------------------------------

# One episode per known host, free to read without an account.
TEST_URLS: dict[str, str] = {
    "www.comic-essay.com": "https://www.comic-essay.com/read/a1063/entry-53088.html",
}


@pytest.mark.network
@pytest.mark.parametrize("host", TEST_URLS)
def test_site_download(tmp_path, host):
    result = Downloader(ComicEssay(), tmp_path, only_first=True).download(TEST_URLS[host])
    assert result.status == "saved"
    assert (result.save_dir / "0.jpg").stat().st_size > 0


@pytest.mark.network
def test_work_page_lists_episodes():
    urls = ComicEssay().series_urls("https://www.comic-essay.com/episode/a1063/")
    assert urls[0] == TEST_URLS["www.comic-essay.com"]
    assert all(ComicEssay.suitable(url) for url in urls)


@pytest.mark.network
def test_older_url_shape_is_readable():
    episode = ComicEssay().episode("https://www.comic-essay.com/read/6/2763.html")
    assert episode.readable
    assert episode.series_title == "ねことじいちゃん＋番外編"
    assert episode.next_url == "https://www.comic-essay.com/read/6/2813.html"
