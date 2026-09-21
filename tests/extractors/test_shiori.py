from __future__ import annotations

from datetime import date
from http import HTTPStatus
from io import BytesIO

import pytest
from PIL import Image

from getjmanga.downloader import Downloader
from getjmanga.errors import NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractors.shiori import BASE_URL, Shiori

SERIES_URL = f"{BASE_URL}/product/runrun"
EPISODE_URL = f"{BASE_URL}/story/runrun_1"
NEXT_URL = f"{BASE_URL}/story/runrun_2"
LAST_URL = f"{BASE_URL}/story/runrun_37"
UPLOADS = f"{BASE_URL}/wp/wp-content/uploads/2026/02"


def episode_html(
    *,
    series="ルンルン",
    episode="1．おしごとのひのあさ",
    pages=("syouzou_h.jpg", "asa1.jpg"),
    next_url=NEXT_URL,
    prev_url=None,
    viewer=True,
):
    """An episode page cut down to what `episode()` reads."""
    slides = "\n".join(
        f'<div class="swiper-slide"><div class="swiper-zoom-container"><img src="{UPLOADS}/{name}"></div></div>'
        for name in pages
    )
    end_nav = ""
    if prev_url:
        end_nav += f'<li><a href="{prev_url}">→前の話へ</a></li>'
    end_nav += f'<li><a href="{SERIES_URL}">作品TOPへ戻る</a></li>'
    if next_url:
        end_nav += f'<li><a href="{next_url}">次の話へ←</a></li>'
    footer_next = (
        f'<a class="prev" href="{next_url}"><b>＜</b> 次の話へ</a>' if next_url else '<span class="prev">　</span>'
    )
    footer_prev = (
        f'<a class="next" href="{prev_url}">前の話へ <b>＞</b></a>' if prev_url else '<span class="next">　</span>'
    )
    main = (
        f"""<main id="singleStory">
<div id="storySlide" dir="rtl"><div class="swiper-wrapper">
{slides}
<div class="swiper-slide pc"></div>
<div class="swiper-slide last">
<div class="ad1"><div id="im-534f"><img src="https://imp-adedge.i-mobile.co.jp/banner.png"></div></div>
<ul class="end-nav">{end_nav}</ul>
</div>
</div><div class="swiper-scrollbar"></div></div>
</main>"""
        if viewer
        else "<main><p>ここに公開中のエピソードはありません。</p></main>"
    )
    return f"""<!doctype html><html lang="ja" id="htmlViewer"><head><meta charset="utf-8">
<title>{series}【01】 &laquo;  栞</title>
<meta property="og:title" content="【{episode}】{series}">
<meta property="og:url" content="{EPISODE_URL}">
</head><body id="bodyViewer"><div id="containerViewer">
<header id="storyHeader">
<a class="back" href="{SERIES_URL}"><b>＜</b> 作品TOPへ</a>
<a class="logo" href="{BASE_URL}/"><b>栞</b></a>
<a class="sns" href="https://x.com/share?url={EPISODE_URL}" rel="nofollow noopener" target="_blank">
<img src="{BASE_URL}/wp/wp-content/themes/shiori/assets/images/story/ico-sns-x.png" alt=""></a>
</header>
{main}
<footer id="storyFooter">
{footer_next}
<span class="current">【{episode}】{series}</span>
{footer_prev}
</footer>
</div></body></html>"""


def series_html(items, *, title="ルンルン"):
    """A work page: the open episodes in a list, oldest first."""
    listing = "\n".join(f'<li><a href="{url}">{label}</a></li>' for url, label in items)
    return f"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<title>{title}  &laquo;  栞</title></head><body>
<div id="container"><header id="header"><nav><ul class="nav">
<li><a href="{BASE_URL}/product">作品一覧</a></li>
<li><a href="{BASE_URL}/about">栞とは</a></li>
</ul></nav></header>
<main id="singleProduct">
<section class="single-product-intro"><hgroup><h1 class="title">{title}</h1></hgroup></section>
<section class="product-story">
<h2 class="section-title"><img alt="公開中のエピソード" src="/title-episode.png"></h2>
<ul>
{listing}
</ul>
</section>
<section class="product-profile">
<h2 class="section-title"><img alt="著者プロフィール" src="/title-profile.png"></h2>
<div class="col2"><div class="text"><div class="scrollable"><div class="comment">
<h3>三崎 島</h3><p>魚座、O型。</p>
</div></div></div></div>
</section>
<section class="product-sns"><ul>
<li><a href="https://x.com/share?url={SERIES_URL}" class="sns-x">X</a></li>
</ul></section>
</main></div></body></html>"""


NOT_FOUND_HTML = "<html><head><title>Page Not Found</title></head><body><h1>404</h1></body></html>"


def jpeg_bytes(size=(8, 8), color=(10, 20, 30)):
    buffer = BytesIO()
    Image.new("RGB", size, color).save(buffer, format="JPEG")
    return buffer.getvalue()


@pytest.fixture
def client(fake_session, fake_response):
    """A `Shiori` on a scripted session; `extra` routes take precedence."""

    def build(extra=None):
        routes = {
            "/story/runrun_1": fake_response(text=episode_html()),
            "/story/runrun_37": fake_response(
                text=episode_html(
                    episode="37．タイムアタックおくりもの",
                    pages=("last.jpg",),
                    next_url=None,
                    prev_url=f"{BASE_URL}/story/runrun_36",
                ),
            ),
            "/story/otome_5": fake_response(text=NOT_FOUND_HTML, status_code=HTTPStatus.NOT_FOUND),
            "/product/runrun": fake_response(
                text=series_html(
                    [
                        (EPISODE_URL, "1．おしごとのひのあさ"),
                        (NEXT_URL, "2．おそうじします"),
                        (f"{BASE_URL}/story/runrun_3?utm=x", "3．バレンタイン"),
                        (NEXT_URL, "2．おそうじします (again)"),
                    ],
                ),
            ),
            "/wp-content/uploads/": fake_response(jpeg_bytes(), content_type="image/jpeg"),
        }
        session = fake_session({**routes, **(extra or {})})
        return Shiori(session), session

    return build


# --- URLs ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        EPISODE_URL,
        f"{BASE_URL}/story/yominoie_021/",
        f"{BASE_URL}/story/gokusairapusodi_21",
        SERIES_URL,
        f"{BASE_URL}/product/yominoie/",
    ],
)
def test_suitable_accepts_episode_and_series_urls(url):
    assert Shiori.suitable(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://shiori-on.com/story/runrun_1",
        "https://www.shiori-on.com/story/runrun_1",
        "https://souffle.life/story/runrun_1",
        f"{BASE_URL}/",
        f"{BASE_URL}/product",
        f"{BASE_URL}/product/",
        f"{BASE_URL}/product/?status=fin",
        f"{BASE_URL}/story/",
        f"{BASE_URL}/story/runrun_1/extra",
        f"{BASE_URL}/blog",
        f"{BASE_URL}/about",
        f"{BASE_URL}/wp/wp-content/uploads/2026/02/asa1.jpg",
    ],
)
def test_suitable_rejects_other_urls(url):
    assert not Shiori.suitable(url)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (SERIES_URL, True),
        (f"{BASE_URL}/product/yominoie/", True),
        (EPISODE_URL, False),
        (f"{BASE_URL}/product/", False),
        (f"{BASE_URL}/product/?order=date", False),
    ],
)
def test_is_series(url, expected):
    assert Shiori.is_series(url) is expected


# --- episodes ---------------------------------------------------------------------------


def test_episode_reads_the_titles_and_the_pages(client):
    shiori, session = client()
    episode = shiori.episode(EPISODE_URL)

    assert episode.series_title == "ルンルン"
    assert episode.episode_title == "1．おしごとのひのあさ"
    assert (episode.writer, episode.publisher) == ("三崎 島", "大洋図書")
    assert [page.url for page in episode.pages] == [f"{UPLOADS}/syouzou_h.jpg", f"{UPLOADS}/asa1.jpg"]
    assert all(page.extra == {} for page in episode.pages)
    assert episode.next_url == NEXT_URL
    assert episode.metadata == {
        "episode_id": "runrun_1",
        "series_url": SERIES_URL,
        "prev_url": None,
        "images": [f"{UPLOADS}/syouzou_h.jpg", f"{UPLOADS}/asa1.jpg"],
    }
    # The author comes off the work page, read once.
    assert session.calls == [EPISODE_URL, SERIES_URL]
    assert session.params_seen == [None, None]
    assert "User-Agent" in session.headers_seen[0]


def test_episode_is_dated_by_its_first_page_upload(client, fake_response, uploaded):
    shiori, _ = client({"/wp-content/uploads/": fake_response(b"", headers=uploaded)})
    assert shiori.episode(EPISODE_URL).published == date(2025, 8, 21)


def test_episode_at_the_end_of_a_series_has_no_next(client):
    shiori, _ = client()
    episode = shiori.episode(LAST_URL)

    assert episode.episode_title == "37．タイムアタックおくりもの"
    assert [page.url for page in episode.pages] == [f"{UPLOADS}/last.jpg"]
    assert (episode.prev_url, episode.next_url) == (f"{BASE_URL}/story/runrun_36", None)
    assert episode.metadata["prev_url"] == f"{BASE_URL}/story/runrun_36"


def test_episode_without_images_has_no_pages_but_still_a_next(client, fake_response):
    shiori, _ = client({"/story/runrun_9": fake_response(text=episode_html(pages=()))})
    episode = shiori.episode(f"{BASE_URL}/story/runrun_9")

    assert episode.pages == ()
    assert not episode.readable
    assert episode.next_url == NEXT_URL


def test_episode_falls_back_to_og_title_and_the_id(client, fake_response):
    bare = (
        '<html><head><meta property="og:title" content="【第２話】そして、乙女は花開く"></head><body>'
        '<div id="storySlide"><div class="swiper-slide"><img src="/wp/wp-content/uploads/x/0001.jpg"></div></div>'
        "</body></html>"
    )
    shiori, _ = client({"/story/otome_2": fake_response(text=bare)})
    episode = shiori.episode(f"{BASE_URL}/story/otome_2")

    assert episode.series_title == "そして、乙女は花開く"
    assert episode.episode_title == "第２話"
    assert [page.url for page in episode.pages] == [f"{BASE_URL}/wp/wp-content/uploads/x/0001.jpg"]
    assert episode.next_url is None
    assert episode.metadata["series_url"] == ""

    shiori, _ = client(
        {"/story/otome_3": fake_response(text='<html><body><div id="storySlide"></div></body></html>')},
    )
    episode = shiori.episode(f"{BASE_URL}/story/otome_3")

    assert episode.series_title == "otome"
    assert episode.episode_title == "otome_3"
    assert episode.pages == ()


def test_expired_episode_is_a_404(client):
    """An episode past its free period is removed: the site answers 404."""
    shiori, _ = client()
    with pytest.raises(NotAnEpisodePageError, match="HTTP 404"):
        shiori.episode(f"{BASE_URL}/story/otome_5")


def test_episode_rejects_a_page_without_the_viewer(client, fake_response):
    shiori, _ = client({"/story/runrun_8": fake_response(text=episode_html(viewer=False))})
    with pytest.raises(NotAnEpisodePageError, match="no viewer"):
        shiori.episode(f"{BASE_URL}/story/runrun_8")


def test_episode_raises_on_a_server_error(client, fake_response):
    shiori, _ = client({"/story/runrun_7": fake_response(text="", status_code=HTTPStatus.BAD_GATEWAY)})
    with pytest.raises(Exception, match="502"):
        shiori.episode(f"{BASE_URL}/story/runrun_7")


# --- series -----------------------------------------------------------------------------


def test_series_urls_lists_the_episodes_in_order_without_duplicates(client):
    shiori, session = client()

    assert shiori.series_urls(SERIES_URL) == [EPISODE_URL, NEXT_URL, f"{BASE_URL}/story/runrun_3"]
    assert session.calls == [SERIES_URL]


def test_series_urls_rejects_an_episode_url(client):
    shiori, _ = client()
    with pytest.raises(UnsupportedUrlError):
        shiori.series_urls(EPISODE_URL)


def test_series_urls_raises_on_an_empty_listing(client, fake_response):
    shiori, _ = client({"/product/empty": fake_response(text=series_html([], title="なにもない"))})
    with pytest.raises(NotAnEpisodePageError, match="lists no episode"):
        shiori.series_urls(f"{BASE_URL}/product/empty")


def test_series_urls_raises_on_an_unknown_work(client, fake_response):
    shiori, _ = client({"/product/gone": fake_response(text=NOT_FOUND_HTML, status_code=HTTPStatus.NOT_FOUND)})
    with pytest.raises(NotAnEpisodePageError, match="no work"):
        shiori.series_urls(f"{BASE_URL}/product/gone")


# --- download ---------------------------------------------------------------------------


def test_download_writes_the_pages(client, tmp_path):
    shiori, session = client()
    result = Downloader(shiori, tmp_path).download(EPISODE_URL)

    assert result.status == "saved"
    assert result.save_dir == tmp_path / "shiori-on.com" / "ルンルン" / "1．おしごとのひのあさ"
    assert sorted(path.name for path in result.save_dir.iterdir()) == ["0.jpg", "1.jpg"]
    with Image.open(result.save_dir / "0.jpg") as image:
        assert image.size == (8, 8)
    assert session.calls[2:] == [f"{UPLOADS}/syouzou_h.jpg", f"{UPLOADS}/asa1.jpg"]
    assert session.headers_seen[2]["Referer"] == EPISODE_URL


# --- the real site --------------------------------------------------------------------

TEST_URLS: dict[str, str] = {
    "shiori-on.com": "https://shiori-on.com/story/runrun_1",
}


@pytest.mark.network
@pytest.mark.parametrize("host", TEST_URLS)
def test_site_download(tmp_path, host):
    result = Downloader(Shiori(), tmp_path, only_first=True).download(TEST_URLS[host])
    assert result.status == "saved"
    assert (result.save_dir / "0.jpg").stat().st_size > 0


@pytest.mark.network
def test_work_page_lists_episodes():
    urls = Shiori().series_urls(SERIES_URL)
    assert urls[0] == EPISODE_URL
    assert all(Shiori.suitable(url) for url in urls)


@pytest.mark.network
def test_expired_episode_raises():
    with pytest.raises(NotAnEpisodePageError, match="HTTP 404"):
        Shiori().episode(f"{BASE_URL}/story/otome_5")
