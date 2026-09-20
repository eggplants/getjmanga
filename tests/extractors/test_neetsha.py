from __future__ import annotations

from http import HTTPStatus
from io import BytesIO

import pytest
from httpx import HTTPStatusError
from PIL import Image

from getjmanga.downloader import Downloader
from getjmanga.errors import NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractors.neetsha import Neetsha

WORK_URL = "http://neetsha.jp/inside/comic.php?id=26627"
STORY_URL = "http://neetsha.jp/inside/comic.php?id=26627&story=1"
SPREAD_URL = "http://neetsha.jp/inside/comic2p.php?id=26627&story=1"
NEXT_URL = "http://neetsha.jp/inside/comic.php?id=26627&story=9"


def story_html(*, number: int = 1, prev: int | None = None, next_: int | None = 9, pages: int = 3) -> str:
    """A story page as the site writes it: the magazine link, the neighbours, one `div.image` per page."""
    prev_link = f'<a class="prev" href="?id=26627&story={prev}">&lt;&lt; 前</a>' if prev else ""
    next_link = f'<a class="next" href="?id=26627&story={next_}">次 &gt;&gt;</a>' if next_ else ""
    images = "".join(
        f'<p class="page-controller"><a href="#">▼</a></p>\n'
        f'<div class="image page{number + index}">\n'
        f'<img src="https://neetsha.jp/inside/up/2/6/26627/{number + index}.jpg" border="0" alt=""/><br>\n'
        "</div>\n"
        for index in range(pages)
    )
    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8">
<title>みいたんとヤニカス / ネルソン - 週刊ヤングVIP - Web漫画とWeb小説の新都社</title></head>
<body>
<table style="margin: 1em auto;"><tbody><tr>
<td><a href="/inside/main.php?magazine=2">
<img src="https://neetsha.jp/inside/image/neetel_inside.gif" alt="Neetel Inside" /></a></td>
<td><a href="/inside/main.php?magazine=2">
    週刊ヤングVIP
</a></td>
</tr></tbody></table>
<div class="center">
<table class="prev-top-next"><tbody><tr>
<td>{prev_link}</td>
<td class="to-top"><a class="comictop" href="?id=26627">表紙</a></td>
<td>{next_link}</td>
</tr></tbody></table>
<div id="main">
<h1>みいたんとヤニカス<br>1、友達</h1>
<p class="page-controller" style="margin:0;">
<a class="comic2p" href="/inside/comic2p.php?id=26627&story=1">見開き</a>&nbsp;&nbsp;
</p>
{images}
<p style="margin:0;"><a href="#">▲</a></p>
</div>
<table class="prev-top-next"><tbody><tr>
<td>{prev_link}</td>
<td class="to-top"><a class="comictop" href="?id=26627">表紙</a></td>
<td>{next_link}</td>
</tr></tbody></table>
<form action="/inside/comment.php" method="POST" class="post">
<p><a href="/inside/main.php?author=ネルソン">ネルソン</a> 先生に励ましのお便りを送ろう！！
<a href="https://neetsha.jp/wiki/index.php/x"><img src="https://neetsha.jp/inside/image/wiki.png"></a></p>
</form>
</div>
<p><a href="/" target="_top"><img src="/image/banner.gif" border="0" alt="Neetsha" /></a></p>
</body></html>
"""


STORY_HTML = story_html()

# A story page whose images have been taken down: the heading stays, the `div.image` blocks are gone.
EMPTY_STORY_HTML = story_html(pages=0)

# A work page: a thumbnail link, a title link and a spread link per story, oldest first,
# the latest story listed twice as the site does after an edit.
WORK_HTML = """<!DOCTYPE html>
<html><head><meta charset="UTF-8">
<title>みいたんとヤニカス / ネルソン - 週刊ヤングVIP - Web漫画とWeb小説の新都社</title></head>
<body>
<table><tbody><tr><td><a href="/inside/main.php?magazine=2">週刊ヤングVIP</a></td></tr></tbody></table>
<div id="main" class="comic-box">
<h1>みいたんとヤニカス</h1>
<p>作：<a href="/inside/main.php?author=ネルソン">ネルソン</a></p>
<table class="story"><tbody><tr><td>
<div class="sep sep-thumb">
<div class="top_image"><a href="/inside/comic.php?id=26627&story=1"
><img src="/inside/up/2/6/26627/1_200_200.jpg"></a></div>
<p><a href="/inside/comic.php?id=26627&story=1">1、友達</a> <span>(8P)</span>
<a href="/inside/comic2p.php?id=26627&story=1">[見開き]</a></p>
</div>
<div class="sep sep-thumb">
<div class="top_image"><a href="/inside/comic.php?id=26627&story=9"
><img src="/inside/up/2/6/26627/9_200_200.jpg"></a></div>
<p><a href="/inside/comic.php?id=26627&story=9">2、大切な日</a> <span>(8P)</span>
<a href="/inside/comic2p.php?id=26627&story=9">[見開き]</a></p>
</div>
<div class="sep sep-thumb">
<p><a href="/inside/comic.php?id=26627&story=17">3、嫌な事</a> <span>(8P)</span>
<a href="/inside/comic2p.php?id=26627&story=17">[見開き]</a></p>
</div>
<div class="sep sep-thumb">
<p><a href="/inside/comic.php?id=26627&story=17">3、嫌な事（再掲）</a></p>
</div>
<div class="sep sep-thumb">
<p><a href="/inside/comic.php?id=99999&story=1">別の作品</a></p>
</div>
</td></tr></tbody></table>
<p>作者コメント:なるべく毎日4ページ</p>
</div>
<div id="comment" class="comic-box"><a href="/inside/comment.php?id=26627">〒みんなの感想を読む</a></div>
</body></html>
"""

EMPTY_WORK_HTML = """<!DOCTYPE html>
<html><head><meta charset="UTF-8"></head><body>
<div id="main" class="comic-box"><h1>準備中</h1>
<table class="story"><tbody><tr><td></td></tr></tbody></table></div>
</body></html>
"""


@pytest.fixture
def client(fake_session):
    def make(routes):
        session = fake_session(routes)
        return Neetsha(session), session

    return make


# --- URLs -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        STORY_URL,
        SPREAD_URL,
        WORK_URL,
        "https://neetsha.jp/inside/comic.php?id=26627&story=1",
        "http://www.neetsha.jp/inside/comic.php?id=26627",
        "http://neetsha.jp/inside/comic.php?story=1&id=26627",
    ],
)
def test_suitable_accepts_work_and_story_urls_over_http(url):
    assert Neetsha.suitable(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://neetsha.jp/",
        "http://neetsha.jp/inside/main.php?magazine=2",
        "http://neetsha.jp/inside/comic.php",
        "http://neetsha.jp/inside/comic.php?id=abc",
        "http://neetsha.jp/inside/comment.php?id=26627",
        "http://neetsha.jp/inside/novel.php?id=26627",
        "http://bbs.neetsha.jp/inside/comic.php?id=26627",
        "ftp://neetsha.jp/inside/comic.php?id=26627",
        "https://example.com/inside/comic.php?id=26627",
    ],
)
def test_suitable_rejects_other_pages(url):
    assert not Neetsha.suitable(url)


def test_is_series_only_for_a_work_page():
    assert Neetsha.is_series(WORK_URL)
    assert Neetsha.is_series("http://neetsha.jp/inside/comic2p.php?id=26627")
    assert not Neetsha.is_series(STORY_URL)
    assert not Neetsha.is_series("http://neetsha.jp/inside/main.php?magazine=2")


# --- a story ----------------------------------------------------------------------


def test_episode_reads_a_story(client, fake_response):
    neetsha, session = client({"comic.php?id=26627&story=1": fake_response(STORY_HTML.encode())})
    episode = neetsha.episode(STORY_URL)

    assert episode.url == STORY_URL
    assert episode.series_title == "みいたんとヤニカス"
    assert episode.episode_title == "1、友達"
    assert [page.url for page in episode.pages] == [
        f"https://neetsha.jp/inside/up/2/6/26627/{n}.jpg" for n in (1, 2, 3)
    ]
    assert episode.next_url == NEXT_URL
    assert episode.metadata["author"] == "ネルソン"
    assert episode.metadata["magazine"] == "週刊ヤングVIP"
    assert episode.metadata["work_url"] == WORK_URL
    assert episode.metadata["prev_url"] is None
    assert session.calls == [STORY_URL]
    assert session.headers_seen[0]["User-Agent"].startswith("Mozilla/5.0")


def test_episode_reads_the_spread_layout_as_the_plain_one(client, fake_response):
    neetsha, session = client({"comic.php?id=26627&story=1": fake_response(STORY_HTML.encode())})
    episode = neetsha.episode(SPREAD_URL)

    assert episode.url == STORY_URL
    assert session.calls == [STORY_URL]


def test_episode_keeps_https_and_www_when_given(client, fake_response):
    url = "https://www.neetsha.jp/inside/comic.php?id=26627&story=1"
    neetsha, session = client({"comic.php?id=26627&story=1": fake_response(STORY_HTML.encode())})
    episode = neetsha.episode(url)

    assert session.calls == [url]
    assert episode.url == url
    assert episode.next_url == "https://www.neetsha.jp/inside/comic.php?id=26627&story=9"


def test_latest_story_has_no_next(client, fake_response):
    html = story_html(number=55, prev=47, next_=None)
    neetsha, _ = client({"comic.php?id=26627&story=55": fake_response(html.encode())})
    episode = neetsha.episode("http://neetsha.jp/inside/comic.php?id=26627&story=55")

    assert (episode.prev_url, episode.next_url) == ("http://neetsha.jp/inside/comic.php?id=26627&story=47", None)
    assert episode.metadata["prev_url"] == "http://neetsha.jp/inside/comic.php?id=26627&story=47"


def test_story_without_pages_is_returned_empty(client, fake_response):
    neetsha, _ = client({"comic.php?id=26627&story=1": fake_response(EMPTY_STORY_HTML.encode())})
    episode = neetsha.episode(STORY_URL)

    assert episode.pages == ()
    assert not episode.readable
    assert episode.next_url == NEXT_URL


def test_story_that_names_none_is_sent_to_the_work_page(client, fake_response):
    # The site answers `story=2` (a page in the middle of a story) with a redirect to the work page.
    neetsha, _ = client({"comic.php?id=26627&story=2": fake_response(WORK_HTML.encode(), url=WORK_URL)})
    with pytest.raises(NotAnEpisodePageError, match="names no story"):
        neetsha.episode("http://neetsha.jp/inside/comic.php?id=26627&story=2")


def test_work_hosted_elsewhere_is_not_an_episode(client, fake_response):
    neetsha, _ = client({"comic.php?id=14": fake_response(b"<html></html>", url="https://haki.web.fc2.com/")})
    with pytest.raises(NotAnEpisodePageError, match="hosted elsewhere"):
        neetsha.episode("http://neetsha.jp/inside/comic.php?id=14&story=1")


@pytest.mark.parametrize("status", [HTTPStatus.NOT_FOUND, HTTPStatus.INTERNAL_SERVER_ERROR])
def test_missing_work_is_not_an_episode(client, fake_response, status):
    neetsha, _ = client({"comic.php": fake_response("作品がありません".encode(), status_code=status)})
    with pytest.raises(NotAnEpisodePageError, match="is gone"):
        neetsha.episode("http://neetsha.jp/inside/comic.php?id=999999999&story=1")


def test_other_errors_still_raise(client, fake_response):
    neetsha, _ = client({"comic.php": fake_response(b"", status_code=HTTPStatus.SERVICE_UNAVAILABLE)})
    with pytest.raises(HTTPStatusError):
        neetsha.episode(STORY_URL)


def test_episode_rejects_a_work_page_url(client):
    neetsha, session = client({})
    with pytest.raises(NotAnEpisodePageError, match="not an episode page"):
        neetsha.episode(WORK_URL)
    assert session.calls == []


# --- a work -----------------------------------------------------------------------


def test_series_urls_lists_oldest_first(client, fake_response):
    neetsha, session = client({"comic.php?id=26627": fake_response(WORK_HTML.encode())})
    urls = neetsha.series_urls(WORK_URL)

    assert urls == [STORY_URL, NEXT_URL, "http://neetsha.jp/inside/comic.php?id=26627&story=17"]
    assert all(Neetsha.suitable(url) for url in urls)
    assert session.calls == [WORK_URL]


def test_series_urls_reads_the_spread_url_as_the_work_page(client, fake_response):
    neetsha, session = client({"comic.php?id=26627": fake_response(WORK_HTML.encode())})
    neetsha.series_urls("http://neetsha.jp/inside/comic2p.php?id=26627")

    assert session.calls == [WORK_URL]


def test_series_urls_rejects_a_story(client):
    neetsha, _ = client({})
    with pytest.raises(UnsupportedUrlError):
        neetsha.series_urls(STORY_URL)


def test_series_urls_with_nothing_listed(client, fake_response):
    neetsha, _ = client({"comic.php?id=26627": fake_response(EMPTY_WORK_HTML.encode())})
    with pytest.raises(NotAnEpisodePageError, match="lists no episode"):
        neetsha.series_urls(WORK_URL)


def test_series_urls_of_a_missing_work(client, fake_response):
    neetsha, _ = client({"comic.php": fake_response(b"", status_code=HTTPStatus.NOT_FOUND)})
    with pytest.raises(NotAnEpisodePageError, match="is gone"):
        neetsha.series_urls("http://neetsha.jp/inside/comic.php?id=999999999")


def test_series_urls_of_a_work_hosted_elsewhere(client, fake_response):
    neetsha, _ = client({"comic.php?id=14": fake_response(b"<html></html>", url="https://haki.web.fc2.com/")})
    with pytest.raises(NotAnEpisodePageError, match="hosted elsewhere"):
        neetsha.series_urls("http://neetsha.jp/inside/comic.php?id=14")


# --- download ---------------------------------------------------------------------


def test_download_writes_the_pages_as_served(client, fake_response, tmp_path):
    buffer = BytesIO()
    Image.new("RGB", (30, 40), (128, 128, 128)).save(buffer, format="JPEG")
    neetsha, session = client(
        {
            "comic.php?id=26627&story=1": fake_response(STORY_HTML.encode()),
            "/inside/up/": fake_response(buffer.getvalue(), content_type="image/jpeg"),
        }
    )
    result = Downloader(neetsha, tmp_path).download(STORY_URL)

    assert result.status == "saved"
    assert result.save_dir == tmp_path / "neetsha.jp" / "みいたんとヤニカス" / "1、友達"
    assert sorted(path.name for path in result.save_dir.iterdir()) == ["0.jpg", "1.jpg", "2.jpg"]
    with Image.open(result.save_dir / "0.jpg") as image:
        assert image.size == (30, 40)
        assert image.getpixel((15, 20)) == (128, 128, 128)
    # The images are asked for with the story as Referer.
    assert session.headers_seen[-1]["Referer"] == STORY_URL


# --- login ------------------------------------------------------------------------


# --- the real site --------------------------------------------------------------------

# One story per known host, free to read without an account: the first story
# of a serial that runs in 週刊ヤングVIP, on the plain host and on its alias.
TEST_URLS: dict[str, str] = {
    "neetsha.jp": "http://neetsha.jp/inside/comic.php?id=26627&story=1",
    "www.neetsha.jp": "http://www.neetsha.jp/inside/comic.php?id=25201&story=1",
}


@pytest.mark.network
@pytest.mark.geoblocked
@pytest.mark.parametrize("host", TEST_URLS)
def test_site_download(tmp_path, host):
    result = Downloader(Neetsha(), tmp_path, only_first=True).download(TEST_URLS[host])
    assert result.status == "saved"
    assert (result.save_dir / "0.jpg").stat().st_size > 0


@pytest.mark.network
@pytest.mark.geoblocked
def test_work_page_lists_stories():
    urls = Neetsha().series_urls(WORK_URL)
    assert urls[0] == STORY_URL
    assert NEXT_URL in urls
    assert all(Neetsha.suitable(url) for url in urls)


@pytest.mark.network
@pytest.mark.geoblocked
def test_story_is_named_and_linked():
    episode = Neetsha().episode(STORY_URL)
    assert episode.series_title == "みいたんとヤニカス"
    assert episode.episode_title == "1、友達"
    assert episode.next_url == NEXT_URL
    assert len(episode.pages) > 1


@pytest.mark.network
@pytest.mark.geoblocked
def test_story_that_names_none_is_not_an_episode():
    with pytest.raises(NotAnEpisodePageError):
        Neetsha().episode("http://neetsha.jp/inside/comic.php?id=26627&story=2")
