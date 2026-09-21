from __future__ import annotations

from http import HTTPStatus
from io import BytesIO

import pytest
from PIL import Image

from getjmanga.downloader import Downloader
from getjmanga.errors import NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractors.omocoro import (
    BASE_URL,
    LISTING_PAGE_SIZE,
    Omocoro,
    listing_urls,
    parse_article,
)

KIJI_URL = f"{BASE_URL}/kiji/588437/"
COMIC_URL = f"{BASE_URL}/comic/590170/"
TAG_URL = f"{BASE_URL}/tag/%E3%83%87%E3%83%BC%E3%83%AA%E3%82%A3%E3%82%BA/"
UPLOADS = f"{BASE_URL}/assets/uploads/2026/08"


def page_img(name, *, width=1000, height=1400):
    """A WordPress full-size image, as the editor writes one into a paragraph."""
    return (
        f'<img alt="" class="alignnone size-full wp-image-5884{name[-2:]}" decoding="async" '
        f'height="{height}" src="{UPLOADS}/{name}" '
        f'srcset="{UPLOADS}/{name} 1000w, {UPLOADS}/{name[:-4]}-381x534.jpg 381w" width="{width}"/>'
    )


def article_html(
    *, body, category="kiji", label="特集", title="【漫画】聖剣", tags=("マンガ", "漫画"), writers=("dollly",)
):
    """An article page cut down to what `parse_article()` reads."""
    tag_links = "、".join(f'<a href="/tag/{tag}">#{tag}</a>' for tag in tags)
    staff_links = "".join(
        f'<a href="{BASE_URL}/writer/{writer}"><img src="{BASE_URL}/assets/uploads/icon.jpg"/>{writer}</a>'
        for writer in writers
    )
    return f"""<!DOCTYPE html><html lang="ja"><head><meta charset="utf-8"/>
<title>{title} | オモコロ</title></head><body>
<div class="content"><div class="article" id="entry-588437"><div class="article-inner">
<div class="article-header">
<div class="image"><img src="{UPLOADS}/eyecatch.jpg"/></div>
<div class="header-meta">
<div class="category"><span class="{category}">{label}</span><div class="tags">{tag_links}</div></div>
<div class="date">2026-09-17</div>
<div class="title">{title}</div>
<div class="description">ちょっとカタカタしてんだよ。明日にはいけるかもな</div>
<div class="staffs">{staff_links}</div>
</div></div>
<div class="article-top"></div>
<div class="article-body {category}">
{body}
</div>
</div>
<div class="article-tags"><div class="article-tag">
<h3><a href="/tag/%e6%bc%ab%e7%94%bb/"><span># 漫画</span>の記事</a></h3>
<div class="boxs"><div class="box">
<div class="image"><a href="{BASE_URL}/kiji/586205/"><img src="{UPLOADS}/other.jpg"/></a></div>
<div class="details"><div class="title"><a href="{BASE_URL}/kiji/586205/"><span>【漫画】夜は楽しい</span></a></div>
</div></div></div></div></div>
</div></div></body></html>"""


COMIC_BODY = (
    "\n".join(f"<p>{page_img(f'page{index:02d}.jpg')}</p>" for index in range(1, 4))
    + """
<p>&nbsp;</p>
<p>dollly の著書はこちら！</p>
<p><a href="https://www.amazon.co.jp/dp/1">チュンまんが （1） (電撃コミックスNEXT)</a></p>
<p>▶この作者の他のマンガを読む◀</p>
<div class="wp-embed"><a href="https://omocoro.jp/kiji/583091/">
<img src="https://omocoro.jp/assets/uploads/2026/07/embed.jpg"/>【まんが】チュン・人魚 続きを読む</a></div>
"""
)

FOUR_KOMA_BODY = f"""<div class="comic-image">
<div class="comic_comment"></div>
<img src="{BASE_URL}/assets/uploads/2026/09/1789109902bh7oj.png"/>
<div class="more-link">
<a href="{BASE_URL}/tag/%E3%82%B5%E3%83%9C%E3%82%8A%E5%85%88%E8%BC%A9/?sort=old">この連載をはじめから読む</a></div>
</div>
<p style="text-align: center">サボり先輩LINEスタンプ好評発売中です！</p>
<p><a href="https://store.line.me/1"><img class="aligncenter wp-image-360542 size-medium"
src="{BASE_URL}/assets/uploads/2022/09/sticker-534x534.jpg" width="534" height="534"/></a></p>
"""

REPORT_BODY = """<p>年齢。それは誰しもが重ねるもの。
「いつまでも心は14歳」なんて思ってましたが流石にこのままではいられない年齢になりました。</p>
""" + "\n".join(
    f"<h3>見出し{index}</h3>\n<p>{page_img(f'photo{index:02d}.jpg', width=1000, height=707)}</p>\n"
    f"<p>{'本文が続きます。' * 12}</p>"
    for index in range(1, 6)
)

NOT_FOUND_HTML = """<!DOCTYPE html><html lang="ja"><head><meta charset="utf-8"/>
<title>Not Found | オモコロ</title></head>
<body><div class="content"><div class="notfound"><h1>お探しのページは見つかりませんでした</h1></div></div>
</body></html>"""


def listing_html(entries, *, pager=True):
    """A tag page: one `div.box` per article, newest (or, with `?sort=old`, oldest) first."""
    boxes = "".join(
        f'<div class="box"><div class="image"><a href="{url}"><img src="{UPLOADS}/thumb.jpg"/></a></div>'
        f'<div class="details"><div class="category"><span class="comic">4コマ</span></div>'
        f'<div class="date">2026.09.11</div>'
        f'<div class="title"><a href="{url}">{title}</a></div><div class="staffs"></div></div></div>'
        for url, title in entries
    )
    navi = (
        f'<div class="page-navi"><span>1</span><a href="{TAG_URL}page/2/?sort=old"><span>2</span></a></div>'
        if pager
        else ""
    )
    return f"""<!DOCTYPE html><html lang="ja"><head><meta charset="utf-8"/>
<title>「デーリィズ」に関する記事 | オモコロ</title></head><body>
<div class="content"><div class="tag-entries"><div class="tag-inner">
<h2 class="waku-text">デーリィズの記事</h2>
<div class="boxs">{boxes}</div>
{navi}
</div></div>
<div class="recommend"><div class="boxs"><div class="box">
<div class="title"><a href="{BASE_URL}/kiji/999999/">おすすめ</a></div></div></div></div>
</div></body></html>"""


def jpeg_bytes(size=(8, 8), color=(10, 20, 30)):
    buffer = BytesIO()
    Image.new("RGB", size, color).save(buffer, format="JPEG")
    return buffer.getvalue()


@pytest.fixture
def client(fake_session, fake_response):
    """An `Omocoro` on a scripted session; `extra` routes take precedence."""

    def build(extra=None):
        routes = {
            "/kiji/588437/": fake_response(text=article_html(body=COMIC_BODY)),
            "/comic/590170/": fake_response(
                text=article_html(
                    body=FOUR_KOMA_BODY,
                    category="comic",
                    label="4コマ",
                    title="【4コマ漫画】台風",
                    tags=("4コマ漫画", "デ～リィズ", "デーリィズ", "漫画"),
                    writers=(),
                ),
            ),
            "/assets/uploads/": fake_response(jpeg_bytes(), content_type="image/jpeg"),
        }
        session = fake_session({**routes, **(extra or {})})
        return Omocoro(session), session

    return build


# --- URLs ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        KIJI_URL,
        f"{BASE_URL}/kiji/588437",
        COMIC_URL,
        TAG_URL,
        f"{BASE_URL}/tag/サボり先輩/",
        f"{BASE_URL}/tag/%E6%BC%AB%E7%94%BB/page/3/",
        f"{BASE_URL}/tag/サボり先輩/?sort=old",
        f"{BASE_URL}/comic/",
        f"{BASE_URL}/comic/page/2/",
    ],
)
def test_suitable_accepts_article_and_listing_urls(url):
    assert Omocoro.suitable(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://omocoro.jp/kiji/588437/",
        "https://www.omocoro.jp/kiji/588437/",
        "https://example.com/kiji/588437/",
        f"{BASE_URL}/",
        f"{BASE_URL}/bros/kiji/588726/",
        f"{BASE_URL}/onigiri/kiji/1/",
        f"{BASE_URL}/matome/570095/",
        f"{BASE_URL}/rensai/584851/",
        f"{BASE_URL}/info/1/",
        f"{BASE_URL}/writer/dollly",
        f"{BASE_URL}/tag/",
        f"{BASE_URL}/tag/a/b/",
        f"{BASE_URL}/bros/?tag=%E6%BC%AB%E7%94%BB",
        f"{BASE_URL}/kiji/abc/",
    ],
)
def test_suitable_rejects_other_urls(url):
    assert not Omocoro.suitable(url)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (KIJI_URL, False),
        (COMIC_URL, False),
        (TAG_URL, True),
        (f"{BASE_URL}/tag/%E6%BC%AB%E7%94%BB/page/3/", True),
        (f"{BASE_URL}/comic/", True),
        (f"{BASE_URL}/comic/page/2/", True),
    ],
)
def test_is_series(url, expected):
    assert Omocoro.is_series(url) is expected


# --- parsing ---------------------------------------------------------------------------


def test_parse_article_takes_the_first_series_tag_as_the_series():
    html = article_html(body=COMIC_BODY, tags=("ハッチンパモス", "マンガ", "漫画"), writers=("キューライス",))
    assert parse_article(html, KIJI_URL).series_title == "ハッチンパモス"


def test_parse_article_falls_back_to_the_site_name_without_tags_or_writers():
    html = article_html(body=COMIC_BODY, tags=("漫画",), writers=())
    assert parse_article(html, KIJI_URL).series_title == "オモコロ"


def test_parse_article_keeps_a_short_afterword_out_of_the_pages():
    """Text after the last page (an ad, a link) is neither a page nor prose between pages."""
    body = COMIC_BODY + f"<p>{'長いあとがき。' * 100}</p>"
    article = parse_article(article_html(body=body), KIJI_URL)
    assert len(article.images) == 3
    assert article.prose_between == 0
    assert article.is_comic


def test_parse_article_counts_prose_between_the_pages():
    body = f"<p>{page_img('page01.jpg')}</p>\n<p>（つづき）</p>\n<p>{page_img('page02.jpg')}</p>"
    article = parse_article(article_html(body=body), KIJI_URL)
    assert article.images == (f"{UPLOADS}/page01.jpg", f"{UPLOADS}/page02.jpg")
    assert article.prose_between == len("（つづき）")
    assert article.is_comic


def test_parse_article_takes_several_images_in_one_block_in_order():
    body = (
        f"<p>{page_img('page01.jpg')}<br/>{page_img('page02.jpg')}</p>"
        "<div><a href='#'><img src='/assets/uploads/p3.png'/></a></div>"
    )
    article = parse_article(article_html(body=body), KIJI_URL)
    assert article.images == (f"{UPLOADS}/page01.jpg", f"{UPLOADS}/page02.jpg", f"{BASE_URL}/assets/uploads/p3.png")


def test_parse_article_ignores_emoji_and_script_blocks():
    body = (
        f'<p><img class="emoji" src="https://s.w.org/images/core/emoji/1f600.svg"/></p>'
        f"<p>{page_img('page01.jpg')}</p><script>var x = 'この文字は数えない';</script>"
        f"<p>{page_img('page02.jpg')}</p>"
    )
    article = parse_article(article_html(body=body), KIJI_URL)
    assert article.images == (f"{UPLOADS}/page01.jpg", f"{UPLOADS}/page02.jpg")
    assert article.prose_between == 0


def test_parse_article_an_untagged_image_article_is_not_a_comic():
    article = parse_article(article_html(body=COMIC_BODY, tags=("レポート", "グルメ")), KIJI_URL)
    assert article.images
    assert not article.is_comic


def test_parse_article_rejects_a_page_without_an_article():
    with pytest.raises(NotAnEpisodePageError, match="no article"):
        parse_article(NOT_FOUND_HTML, f"{BASE_URL}/kiji/1/")


def test_listing_urls_keeps_the_article_links_in_order_deduplicated():
    html = listing_html(
        [
            (f"{BASE_URL}/comic/1/", "a"),
            (f"{BASE_URL}/kiji/2/", "b"),
            (f"{BASE_URL}/bros/kiji/3/", "c"),
            (f"{BASE_URL}/matome/4/", "d"),
            (f"{BASE_URL}/comic/1/", "a again"),
            ("/kiji/5/", "e"),
        ],
    )
    assert listing_urls(html, TAG_URL) == [f"{BASE_URL}/comic/1/", f"{BASE_URL}/kiji/2/", f"{BASE_URL}/kiji/5/"]


# --- episodes --------------------------------------------------------------------------


def test_episode_reads_the_titles_and_the_pages(client):
    omocoro, session = client()
    episode = omocoro.episode(KIJI_URL)

    assert episode.url == KIJI_URL
    assert episode.series_title == "dollly"
    assert episode.episode_title == "【漫画】聖剣"
    assert (episode.writer, episode.publisher) == ("dollly", "バーグハンバーグバーグ")
    assert [page.url for page in episode.pages] == [f"{UPLOADS}/page{index:02d}.jpg" for index in range(1, 4)]
    assert episode.next_url is None
    assert episode.readable
    assert episode.metadata["tags"] == ["マンガ", "漫画"]
    assert episode.metadata["category"] == "kiji"
    assert episode.metadata["images"] == [page.url for page in episode.pages]
    assert session.calls == [KIJI_URL]
    assert session.params_seen == [None]


def test_episode_reads_a_four_koma_post(client):
    omocoro, _ = client()
    episode = omocoro.episode(COMIC_URL)

    assert episode.series_title == "デ～リィズ"
    assert episode.episode_title == "【4コマ漫画】台風"
    assert [page.url for page in episode.pages] == [f"{BASE_URL}/assets/uploads/2026/09/1789109902bh7oj.png"]
    assert episode.metadata["writers"] == []


def test_episode_rejects_a_text_article(client, fake_response):
    omocoro, _ = client({"/kiji/533886/": fake_response(text=article_html(body=REPORT_BODY, tags=("漫画",)))})
    with pytest.raises(NotAnEpisodePageError, match="text article"):
        omocoro.episode(f"{BASE_URL}/kiji/533886/")


def test_episode_rejects_a_404(client, fake_response):
    omocoro, _ = client({"/kiji/1/": fake_response(text=NOT_FOUND_HTML, status_code=HTTPStatus.NOT_FOUND)})
    with pytest.raises(NotAnEpisodePageError, match="404"):
        omocoro.episode(f"{BASE_URL}/kiji/1/")


def test_episode_rejects_a_listing_url_without_fetching(client):
    omocoro, session = client()
    with pytest.raises(NotAnEpisodePageError, match="not an article"):
        omocoro.episode(TAG_URL)
    assert session.calls == []


# --- series ----------------------------------------------------------------------------


def test_series_urls_walks_the_pages_oldest_first(client, fake_response):
    first = [(f"{BASE_URL}/comic/{index}/", f"第{index}話") for index in range(1, LISTING_PAGE_SIZE + 1)]
    second = [
        (f"{BASE_URL}/comic/{index}/", f"第{index}話") for index in range(LISTING_PAGE_SIZE + 1, LISTING_PAGE_SIZE + 4)
    ]
    omocoro, session = client(
        {
            "/page/2/": fake_response(text=listing_html(second, pager=False)),
            "/page/3/": fake_response(text=NOT_FOUND_HTML, status_code=HTTPStatus.NOT_FOUND),
            "/tag/": fake_response(text=listing_html(first)),
        },
    )
    urls = omocoro.series_urls(TAG_URL)

    assert urls == [url for url, _ in first + second]
    assert session.calls == [TAG_URL, f"{TAG_URL}page/2/"]
    assert session.params_seen == [{"sort": "old"}, {"sort": "old"}]
    assert all(Omocoro.suitable(url) for url in urls)


def test_series_urls_stops_at_a_404(client, fake_response):
    full = [(f"{BASE_URL}/kiji/{index}/", f"第{index}話") for index in range(1, LISTING_PAGE_SIZE + 1)]
    omocoro, session = client(
        {
            "/page/2/": fake_response(text=NOT_FOUND_HTML, status_code=HTTPStatus.NOT_FOUND),
            "/tag/": fake_response(text=listing_html(full)),
        },
    )
    assert len(omocoro.series_urls(f"{BASE_URL}/tag/サボり先輩/page/5/")) == LISTING_PAGE_SIZE
    # Whatever page the URL named, the walk starts from the first one.
    assert session.calls == [f"{BASE_URL}/tag/サボり先輩/", f"{BASE_URL}/tag/サボり先輩/page/2/"]


def test_series_urls_lists_the_four_koma_archive(client, fake_response):
    html = listing_html([(f"{BASE_URL}/comic/22918/", "a"), (f"{BASE_URL}/comic/22923/", "b")]).replace(
        "tag-entries", "category-entries"
    )
    omocoro, session = client({"/comic/": fake_response(text=html)})
    assert omocoro.series_urls(f"{BASE_URL}/comic/page/2/") == [f"{BASE_URL}/comic/22918/", f"{BASE_URL}/comic/22923/"]
    assert session.calls == [f"{BASE_URL}/comic/"]


def test_series_urls_raises_on_an_empty_listing(client, fake_response):
    omocoro, _ = client({"/tag/": fake_response(text=listing_html([], pager=False))})
    with pytest.raises(NotAnEpisodePageError, match="no article"):
        omocoro.series_urls(TAG_URL)


def test_series_urls_raises_on_an_unknown_tag(client, fake_response):
    omocoro, _ = client({"/tag/": fake_response(text=NOT_FOUND_HTML, status_code=HTTPStatus.NOT_FOUND)})
    with pytest.raises(NotAnEpisodePageError, match="no article"):
        omocoro.series_urls(f"{BASE_URL}/tag/nonexistent/")


def test_series_urls_rejects_an_article_url(client):
    omocoro, _ = client()
    with pytest.raises(UnsupportedUrlError):
        omocoro.series_urls(KIJI_URL)


# --- downloading ------------------------------------------------------------------------


def test_download_writes_the_pages_as_served(client, tmp_path):
    omocoro, session = client()
    result = Downloader(omocoro, tmp_path).download(KIJI_URL)

    assert result.status == "saved"
    assert result.save_dir == tmp_path / "omocoro.jp" / "dollly" / "【漫画】聖剣"
    assert sorted(path.name for path in result.save_dir.iterdir()) == ["0.jpg", "1.jpg", "2.jpg"]
    with Image.open(result.save_dir / "0.jpg") as saved:
        assert saved.size == (8, 8)
        assert saved.getpixel((4, 4)) == pytest.approx((10, 20, 30), abs=8)
    assert session.calls[1:] == [f"{UPLOADS}/page{index:02d}.jpg" for index in range(1, 4)]
    assert session.headers_seen[-1]["Referer"] == KIJI_URL


# --- the real site ----------------------------------------------------------------------

# One episode per known host, free to read without an account.
TEST_URLS: dict[str, str] = {
    "omocoro.jp": "https://omocoro.jp/kiji/588437/",
}


@pytest.mark.network
@pytest.mark.parametrize("host", TEST_URLS)
def test_site_download(tmp_path, host):
    result = Downloader(Omocoro(), tmp_path, only_first=True).download(TEST_URLS[host])
    assert result.status == "saved"
    assert (result.save_dir / "0.jpg").stat().st_size > 0


@pytest.mark.network
def test_four_koma_post_is_readable(tmp_path):
    result = Downloader(Omocoro(), tmp_path, only_first=True).download("https://omocoro.jp/comic/590170/")
    assert result.status == "saved"
    assert result.episode.episode_title == "【4コマ漫画】台風"


@pytest.mark.network
def test_tag_page_lists_the_series_oldest_first():
    urls = Omocoro().series_urls(
        "https://omocoro.jp/tag/%E3%83%8F%E3%83%83%E3%83%81%E3%83%B3%E3%83%91%E3%83%A2%E3%82%B9/"
    )
    assert urls[0] == "https://omocoro.jp/kiji/484565/"
    assert "https://omocoro.jp/kiji/588005/" in urls
    assert all(Omocoro.suitable(url) for url in urls)


@pytest.mark.network
def test_text_article_is_not_an_episode():
    with pytest.raises(NotAnEpisodePageError, match="text article"):
        Omocoro().episode("https://omocoro.jp/kiji/533886/")
