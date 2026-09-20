from __future__ import annotations

from http import HTTPStatus
from io import BytesIO

import pytest
from PIL import Image

from getjmanga.downloader import Downloader
from getjmanga.errors import NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractors.crea import BASE_URL, ROOM_TITLE, Crea, original_image_url

CDN = "https://crea.ismcdn.jp"
SERIES_URL = f"{BASE_URL}/list/40sai"
AUTHOR_URL = f"{BASE_URL}/list/comic-essay/author/%E3%81%8A%E3%81%A5%20%E3%81%BE%E3%82%8A%E3%81%93"
EPISODE_URL = f"{BASE_URL}/articles/-/55866"
NEXT_URL = f"{BASE_URL}/articles/-/55981"
LAST_URL = f"{BASE_URL}/articles/-/55984"
SUMMARY_URL = f"{BASE_URL}/articles/-/45263"
ESSAY_URL = f"{BASE_URL}/articles/-/59798"

PAGE_1 = "a/3/1280wm/img_a3f39015009ca60cd09371911e0834fd847037.jpg"
PAGE_2 = "7/5/1280wm/img_75d82f4b58501800c2ac688a1541ec38736218.jpg"
ORIGINAL_1 = f"{CDN}/mwimgs/a/3/-/img_a3f39015009ca60cd09371911e0834fd847037.jpg"
ORIGINAL_2 = f"{CDN}/mwimgs/7/5/-/img_75d82f4b58501800c2ac688a1541ec38736218.jpg"


def button(label, href, *, extra_class=""):
    return f'<div class="link-button {extra_class} noprovide"><a href="{href}">{label}</a></div>'


def article_html(
    *,
    comic=True,
    title="第1回　はじめに",
    series="ごきげんな40歳になりたい",
    pages=(PAGE_1, PAGE_2),
    next_url=NEXT_URL,
    prev_url=None,
    summary_url=None,
    author="おづ まりこ",
    box_title=None,
):
    """A CREA article cut down to what `episode()` reads."""
    logo = (
        '<p class="subtitle comic"><a href="/list/comic-essay"><img alt="コミックエッセイルーム" class="lazyload" '
        f'data-src="{CDN}/common/crea/images/v1/logo/comic_essay_sp.png" src="{CDN}/common/images/blank.gif"></a></p>'
        if comic
        else ""
    )
    figures = "\n".join(
        f'<figure class="image-area figure-center"><img alt="" data-height="1824" data-width="1250" height="1824" '
        f'src="{CDN}/mwimgs/{name}" width="1250"></figure>'
        for name in pages
    )
    buttons = ""
    if prev_url:
        buttons += button("前のお話を読む", prev_url, extra_class="back")
    if next_url:
        buttons += button("次のお話を読む", next_url)
    if summary_url:
        buttons += button("まとめページへ", summary_url)
    page_title = f"{title} | {series}" if series else title
    box_title = series if box_title is None else box_title
    return f"""<!DOCTYPE html><html lang="ja"><head><title>{page_title}</title>
<meta property="og:title" content="{page_title}"></head>
<body><main class="content content--single"><div class="wrap">
<div class="article-head google-anno-skip">
<div class="article-head__top"><p class="article-head__date">2025.10.17</p></div>
<h1 class="article-head__title comic-and-essayroom">{title}</h1>
<div class="article-head__labels"><div class="article-head__genres">
<a href="/list/latest/comic">コミック ＆ エッセイ</a></div></div>
<div class="article-head__author"><span class="author"><span class="icon-author"></span>
<a class="abClick" href="/list/author/5d82199377656162dd000000">{author}</a></span></div>
</div>
<div class="article-head no-border">{logo}
<p class="icatch"><img alt="{title}" class="lazyload" src="{CDN}/common/images/blank.gif" width="800" height="600"></p>
</div>
<article class="article-body">
{figures}
<h2 style="text-align: center;">【単行本のお知らせ】</h2>
<div class="box-color clearfix">
<figure class="image-area figure-center"><a href="https://www.amazon.co.jp/dp/4163920455" target="_blank">
<img alt="" data-height="2880" data-width="2039"
src="{CDN}/mwimgs/7/8/1280wm/img_78a7397ee2a4c9db4496901f8f55098c8335663.jpg"></a>
<figcaption class="blank-caption"></figcaption></figure>
<p>いちばん心地いい、わたしを探して――。</p>
</div>
{buttons}
<hr>
<img alt="" height="1" id="read-to-end" src="{CDN}/common/images/blank.gif" width="1">
<div class="link-button green nekokuma">
<a href="https://form.bunshun.jp/webapp/form/11761_jkr_413/index.do" target="_blank">感想を送る</a></div>
</article>
<div class="comic-essay-bottom"><div class="box"><div class="box-left"><p class="title">{box_title}</p></div>
<div class="box-right"><ul><li><a href="/articles/-/55984">
<p class="title">第3回<br>忘れるスイッチ</p></a></li></ul></div></div>
<div class="link-button">
<a href="/list/comic-essay/author/%E3%81%8A%E3%81%A5%20%E3%81%BE%E3%82%8A%E3%81%93">作家ページへ</a></div>
</div>
</div></main></body></html>"""


def listing_html(items, *, next_page=None, title="ごきげんな40歳になりたい"):
    """A `/list/<slug>` page: the articles newest first, 20 to a page."""
    entries = "\n".join(
        f"""<div class="list-default">
<time class="list-default__date" datetime="2025-11-13T12:00:00+09:00">2025.11.13</time>
<div class="list-default__item">
<a class="list-default__image" href="{href}"><img alt="" src="{CDN}/mwimgs/c/2/-/img_c298.jpg"></a>
<div class="list-default__desc"><a class="list-default__title" href="{href}">{text}</a>
<ul class="list-default__genres"><li><a href="/list/comic-essay">コミック ＆ エッセイ</a></li></ul></div>
</div></div>"""
        for href, text in items
    )
    pager = f'<div class="list-next"><a href="{next_page}">次の20件を表示</a></div>' if next_page else ""
    return f"""<!DOCTYPE html><html lang="ja"><head><title>{title} | CREA</title></head>
<body><main class="content"><div class="content__left">
<div class="list-header"><p class="list-numbers">{len(items)}<span>件</span></p></div>
<div class="lists-default">
{entries}
</div>
{pager}
<div class="list-author-info"><p class="list-author-info__name">おづ まりこ</p></div>
</div>
<section class="module-comic-and-essayroom --page"><div class="article-list">
<a class="article__link" href="/articles/-/59798">川添愛「言葉のセンス研究所」（9）</a>
</div></section>
</main></body></html>"""


def author_html(works):
    """An author page: one block per series, its articles newest first."""
    blocks = "\n".join(
        f"""<div class="list-authors-work__unit"><div class="list-authors-work__list">
<div class="list-authors-work__list-head"><a href="{slug}">{name}</a></div>
<div class="list-authors-work__items">{"".join(f'<a href="{href}">{text}</a>' for href, text in items)}</div>
</div></div>"""
        for slug, name, items in works
    )
    return f"""<!DOCTYPE html><html lang="ja"><head>
<title>おづ まりこの作家ページ | コミックエッセイルーム | CREA</title></head>
<body><main class="content">{blocks}
<div class="list-promo"><ul class="list-promo__list">
<li class="list-promo__media"><a href="/articles/-/56315">単行本</a></li></ul></div>
</main></body></html>"""


NOT_FOUND_HTML = """<html><head><title>CREA | クレア ウェブ 好奇心旺盛な女性たちへ</title></head>
<body><main class="content"><p>お探しのページは見つかりませんでした。</p></main></body></html>"""


def image_bytes(fmt="JPEG", size=(8, 8), color=(10, 20, 30)):
    buffer = BytesIO()
    Image.new("RGB", size, color).save(buffer, format=fmt)
    return buffer.getvalue()


@pytest.fixture
def client(fake_session, fake_response):
    """A `Crea` on a scripted session; `extra` routes take precedence."""

    def build(extra=None):
        routes = {
            "/articles/-/55866": fake_response(text=article_html()),
            "/articles/-/55984": fake_response(
                text=article_html(title="第3回　忘れるスイッチ", pages=(PAGE_2,), next_url=None, prev_url=NEXT_URL),
            ),
            "/articles/-/59798": fake_response(
                text=article_html(
                    comic=False, title="言葉のセンス研究所（9）", series="言葉のセンス研究所", next_url=None
                ),
            ),
            "/mwimgs/": fake_response(image_bytes(), content_type="image/jpeg"),
        }
        extra = extra or {}
        # The extra routes go first: the session takes the first substring match.
        session = fake_session(
            {**extra, **{needle: response for needle, response in routes.items() if needle not in extra}}
        )
        return Crea(session), session

    return build


# --- URLs ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        EPISODE_URL,
        f"{BASE_URL}/articles/-/22579/",
        SERIES_URL,
        f"{BASE_URL}/list/tobidase-tsudui/",
        f"{BASE_URL}/list/202506_asano-chikada",
        AUTHOR_URL,
        f"{BASE_URL}/list/comic-essay/author/yoimoa",
    ],
)
def test_suitable_accepts_article_series_and_author_urls(url):
    assert Crea.suitable(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://crea.bunshun.jp/articles/-/55866",
        "https://bunshun.jp/articles/-/55866",
        "https://number.bunshun.jp/articles/-/55866",
        f"{BASE_URL}/",
        f"{BASE_URL}/articles/",
        f"{BASE_URL}/articles/-/",
        f"{BASE_URL}/articles/-/abc",
        f"{BASE_URL}/articles/-/55866/2",
        f"{BASE_URL}/list/",
        f"{BASE_URL}/list/comic-essay",
        f"{BASE_URL}/list/comic-essay/",
        f"{BASE_URL}/list/genre/culture",
        f"{BASE_URL}/list/matome/ambassador",
        f"{BASE_URL}/list/comic-essay/author/",
        f"{BASE_URL}/list/author/5d82199377656162dd000000",
        f"{BASE_URL}/list/feed/rss-all",
    ],
)
def test_suitable_rejects_other_urls(url):
    assert not Crea.suitable(url)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (SERIES_URL, True),
        (f"{SERIES_URL}?page=2", True),
        (AUTHOR_URL, True),
        (EPISODE_URL, False),
        (f"{BASE_URL}/list/comic-essay", False),
        (f"{BASE_URL}/list/genre/culture", False),
    ],
)
def test_is_series(url, expected):
    assert Crea.is_series(url) is expected


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (f"{CDN}/mwimgs/{PAGE_1}", ORIGINAL_1),
        (
            f"{CDN}/mwimgs/2/2/1600wm/img_22bef6e5b684e409b97a118ca77d852b348551.png?rd=1",
            f"{CDN}/mwimgs/2/2/-/img_22bef6e5b684e409b97a118ca77d852b348551.png",
        ),
        (ORIGINAL_1, ORIGINAL_1),
        (f"{CDN}/common/images/blank.gif", f"{CDN}/common/images/blank.gif"),
        (f"{CDN}/mwimgs/a/3/img_a3f3.jpg", f"{CDN}/mwimgs/a/3/img_a3f3.jpg"),
    ],
)
def test_original_image_url_drops_the_rendition_size(url, expected):
    assert original_image_url(url) == expected


# --- episodes ---------------------------------------------------------------------------


def test_episode_reads_the_titles_and_the_pages(client):
    crea, session = client()
    episode = crea.episode(EPISODE_URL)

    assert episode.series_title == "ごきげんな40歳になりたい"
    assert episode.episode_title == "第1回　はじめに"
    # The book cover inside the promotion box is not a page.
    assert [page.url for page in episode.pages] == [ORIGINAL_1, ORIGINAL_2]
    assert [(page.width, page.height) for page in episode.pages] == [(1250, 1824), (1250, 1824)]
    assert all(page.extra == {} for page in episode.pages)
    assert episode.next_url == NEXT_URL
    assert episode.metadata == {
        "article_id": "55866",
        "date": "2025.10.17",
        "author": "おづ まりこ",
        "author_url": AUTHOR_URL,
        "prev_url": None,
        "summary_url": None,
        "images": [ORIGINAL_1, ORIGINAL_2],
    }
    assert session.calls == [EPISODE_URL]
    assert session.params_seen == [None]
    assert "User-Agent" in session.headers_seen[0]


def test_episode_at_the_end_of_a_series_has_no_next(client):
    crea, _ = client()
    episode = crea.episode(LAST_URL)

    assert episode.episode_title == "第3回　忘れるスイッチ"
    assert [page.url for page in episode.pages] == [ORIGINAL_2]
    assert episode.next_url is None
    assert episode.metadata["prev_url"] == NEXT_URL


def test_episode_reads_the_summary_link_and_relative_buttons(client, fake_response):
    html = article_html(
        title="第4話 「老犬とお風呂」",
        series="老犬とつづ井",
        next_url="/articles/-/47307",
        prev_url="/articles/-/43833",
        summary_url=SUMMARY_URL,
    )
    crea, _ = client({"/articles/-/44277": fake_response(text=html)})
    episode = crea.episode(f"{BASE_URL}/articles/-/44277")

    assert episode.series_title == "老犬とつづ井"
    assert episode.next_url == f"{BASE_URL}/articles/-/47307"
    assert episode.metadata["prev_url"] == f"{BASE_URL}/articles/-/43833"
    assert episode.metadata["summary_url"] == SUMMARY_URL


def test_episode_without_page_images_has_no_pages(client, fake_response):
    # A series' まとめ index is a comic-essay article with links and no pages.
    crea, _ = client(
        {"/articles/-/45263": fake_response(text=article_html(title="老犬とつづ井 まとめ", pages=(), next_url=None))}
    )
    episode = crea.episode(SUMMARY_URL)

    assert episode.pages == ()
    assert not episode.readable
    assert episode.episode_title == "老犬とつづ井 まとめ"
    assert episode.next_url is None


def test_episode_falls_back_to_the_series_box_when_the_page_title_names_no_series(client, fake_response):
    crea, _ = client({"/articles/-/1": fake_response(text=article_html(series="", box_title="老犬とつづ井"))})
    episode = crea.episode(f"{BASE_URL}/articles/-/1")

    assert episode.series_title == "老犬とつづ井"
    assert episode.episode_title == "第1回　はじめに"


def test_episode_belongs_to_the_room_when_nothing_names_a_series(client, fake_response):
    crea, _ = client({"/articles/-/1": fake_response(text=article_html(series="", box_title=""))})
    assert crea.episode(f"{BASE_URL}/articles/-/1").series_title == ROOM_TITLE


def test_episode_takes_a_lazy_loaded_image_from_data_src(client, fake_response):
    html = article_html(pages=()).replace(
        '<article class="article-body">',
        '<article class="article-body"><figure class="image-area"><img class="lazyload" '
        f'src="{CDN}/common/images/blank.gif" data-src="/mwimgs/{PAGE_1}" width="800" height="1100"></figure>'
        f'<figure class="image-area"><img src="{CDN}/common/images/blank.gif"></figure>',
    )
    crea, _ = client({"/articles/-/2": fake_response(text=html)})
    episode = crea.episode(f"{BASE_URL}/articles/-/2")

    assert [page.url for page in episode.pages] == [
        f"{BASE_URL}/mwimgs/a/3/-/img_a3f39015009ca60cd09371911e0834fd847037.jpg"
    ]
    assert (episode.pages[0].width, episode.pages[0].height) == (800, 1100)


def test_episode_rejects_an_article_that_is_not_a_comic(client):
    crea, _ = client()
    with pytest.raises(NotAnEpisodePageError, match="not a comic essay"):
        crea.episode(ESSAY_URL)


def test_episode_rejects_a_page_without_an_article_body(client, fake_response):
    crea, _ = client({"/list/40sai": fake_response(text=listing_html([(EPISODE_URL, "第1回")]))})
    with pytest.raises(NotAnEpisodePageError, match="no article"):
        crea.episode(SERIES_URL)


def test_episode_rejects_a_404(client, fake_response):
    crea, _ = client({"/articles/-/43300": fake_response(text=NOT_FOUND_HTML, status_code=HTTPStatus.NOT_FOUND)})
    with pytest.raises(NotAnEpisodePageError, match="no article"):
        crea.episode(f"{BASE_URL}/articles/-/43300")


# --- series -----------------------------------------------------------------------------


def article_url(number):
    return f"{BASE_URL}/articles/-/{55865 + number}"


def test_series_urls_lists_the_articles_oldest_first(client, fake_response):
    listing = [(f"/articles/-/{55865 + number}", f"第{number}回") for number in (3, 2, 1)]
    crea, session = client({"/list/40sai": fake_response(text=listing_html(listing))})

    assert crea.series_urls(SERIES_URL) == [article_url(number) for number in (1, 2, 3)]
    assert session.calls == [SERIES_URL]


def test_series_urls_follows_the_next_20_link(client, fake_response):
    newest = [(f"/articles/-/{55865 + number}", f"第{number}回") for number in range(24, 4, -1)]
    oldest = [(f"/articles/-/{55865 + number}", f"第{number}回") for number in range(4, 0, -1)]
    routes = {
        "/list/talk?page=2": fake_response(text=listing_html(oldest)),
        "/list/talk": fake_response(text=listing_html(newest, next_page="/list/talk?page=2")),
    }
    crea, session = client(routes)

    assert crea.series_urls(f"{BASE_URL}/list/talk") == [article_url(number) for number in range(1, 25)]
    assert session.calls == [f"{BASE_URL}/list/talk", f"{BASE_URL}/list/talk?page=2"]


def test_series_urls_stops_at_a_page_already_seen(client, fake_response):
    listing = [(f"/articles/-/{55865 + number}", f"第{number}回") for number in (2, 1)]
    crea, session = client({"/list/loop": fake_response(text=listing_html(listing, next_page="/list/loop"))})

    assert crea.series_urls(f"{BASE_URL}/list/loop") == [article_url(1), article_url(2)]
    assert session.calls == [f"{BASE_URL}/list/loop"]


def test_series_urls_lists_an_author_page_series_by_series(client, fake_response):
    works = [
        (
            "/list/tobidase-tsudui",
            "とびだせ！ つづ井さん",
            [("/articles/-/59799", "第36回"), ("/articles/-/42090", "「ごあいさつ」")],
        ),
        (
            "/list/roukentotsudui",
            "老犬とつづ井",
            [("/articles/-/47307", "第13話"), ("/articles/-/42392", "はじめに"), ("/articles/-/59799", "dup")],
        ),
    ]
    crea, session = client({"/list/comic-essay/author/": fake_response(text=author_html(works))})

    assert crea.series_urls(AUTHOR_URL) == [
        f"{BASE_URL}/articles/-/42090",
        f"{BASE_URL}/articles/-/59799",
        f"{BASE_URL}/articles/-/42392",
        f"{BASE_URL}/articles/-/47307",
    ]
    assert session.calls == [AUTHOR_URL]


def test_series_urls_raises_on_an_empty_listing(client, fake_response):
    crea, _ = client({"/list/40sai": fake_response(text=listing_html([]))})
    with pytest.raises(NotAnEpisodePageError, match="lists no episode"):
        crea.series_urls(SERIES_URL)


def test_series_urls_raises_on_a_404(client, fake_response):
    crea, _ = client({"/list/nothing": fake_response(text=NOT_FOUND_HTML, status_code=HTTPStatus.NOT_FOUND)})
    with pytest.raises(NotAnEpisodePageError, match="no series"):
        crea.series_urls(f"{BASE_URL}/list/nothing")


def test_series_urls_rejects_an_article_url(client):
    crea, _ = client()
    with pytest.raises(UnsupportedUrlError, match="not a series page"):
        crea.series_urls(EPISODE_URL)


# --- downloading ------------------------------------------------------------------------


def test_download_writes_the_pages_as_served(client, tmp_path):
    crea, session = client()
    result = Downloader(crea, tmp_path).download(EPISODE_URL)

    assert result.status == "saved"
    assert result.save_dir == tmp_path / "crea.bunshun.jp" / "ごきげんな40歳になりたい" / "第1回　はじめに"
    assert sorted(path.name for path in result.save_dir.iterdir()) == ["0.jpg", "1.jpg"]
    with Image.open(result.save_dir / "0.jpg") as saved:
        assert saved.size == (8, 8)
        assert saved.getpixel((4, 4)) == pytest.approx((10, 20, 30), abs=8)
    assert session.calls[1:] == [ORIGINAL_1, ORIGINAL_2]
    assert session.headers_seen[-1]["Referer"] == EPISODE_URL


# --- the real site ----------------------------------------------------------------------

# One episode per known host, free to read without an account.
TEST_URLS: dict[str, str] = {
    "crea.bunshun.jp": "https://crea.bunshun.jp/articles/-/22579",
}


@pytest.mark.network
@pytest.mark.parametrize("host", TEST_URLS)
def test_site_download(tmp_path, host):
    result = Downloader(Crea(), tmp_path, only_first=True).download(TEST_URLS[host])
    assert result.status == "saved"
    assert result.episode.series_title == "裸一貫！ つづ井さん"
    assert result.episode.next_url == "https://crea.bunshun.jp/articles/-/22784"
    assert (result.save_dir / "0.jpg").stat().st_size > 0


@pytest.mark.network
def test_series_page_lists_episodes():
    urls = Crea().series_urls("https://crea.bunshun.jp/list/40sai")
    assert urls[0] == "https://crea.bunshun.jp/articles/-/55866"
    assert all(Crea.suitable(url) for url in urls)


@pytest.mark.network
def test_author_page_lists_every_series():
    urls = Crea().series_urls("https://crea.bunshun.jp/list/comic-essay/author/%E3%81%A4%E3%81%A5%E4%BA%95")
    assert TEST_URLS["crea.bunshun.jp"] in urls
    assert all(Crea.suitable(url) for url in urls)


@pytest.mark.network
def test_text_essay_is_not_an_episode():
    with pytest.raises(NotAnEpisodePageError, match="not a comic essay"):
        Crea().episode("https://crea.bunshun.jp/articles/-/59798")
