from __future__ import annotations

from http import HTTPStatus
from io import BytesIO

import pytest
from PIL import Image

from getjmanga.downloader import Downloader
from getjmanga.extractors.carula import Carula, is_locked, next_key, split_title
from getjmanga.extractors.common import NotAnEpisodePageError, UnsupportedUrlError

EPISODE_URL = "https://note.com/carula/n/nb016b73d0f1d"
NEXT_URL = "https://note.com/carula/n/n596c41e8118a"
LEGACY_URL = "https://carula.jp/series/3246e491487f1"
MAGAZINE_URL = "https://note.com/carula/m/m98ee122ad3e2"

INDEX = (
    '<p name="x" id="x"><a href="https://note.com/carula/n/nb016b73d0f1d" target="_blank">Lesson 1</a>　'
    '<a href="https://note.com/carula/n/n596c41e8118a" target="_blank">Lesson 2</a>　<br>'
    '<strong>4月17日更新&nbsp;</strong><a href="https://note.com/carula/n/na3813b19fd66">Lesson 3</a>（連載中）</p>'
)
PAGE_1 = "https://assets.st-note.com/img/1733025948-1YSvMtWZ3i7GKsof8OD9hmVQ.jpg"
PAGE_2 = "https://assets.st-note.com/img/1733025948-7uHDFyiMf5kACSx2IGmbrJEj.jpg"
AMAZON_EMBED = (
    '<figure name="e" id="e" data-src="https://amzn.asia/d/x" embedded-service="external-article">'
    '<a href="https://amzn.asia/d/x"><strong>留学ろっく!! 1</strong></a></figure>'
)
BODY = (
    INDEX
    + f'<figure name="a" id="a"><img src="{PAGE_1}" alt="Lesson 1" width="620" height="946"><figcaption></figcaption>'
    + f'</figure><figure name="b" id="b"><img src="{PAGE_2}" alt="" width="620" height="946"><figcaption></figcaption>'
    + "</figure>"
    + AMAZON_EMBED
)


def note(**overrides):
    return {
        "key": "nb016b73d0f1d",
        "name": "『留学ろっく!!』 Lesson 1　パパはダイヤモンドチューバー‼ ",
        "body": BODY,
        "price": 0,
        "is_purchased": False,
        "remained_figure_num": 0,
        "remained_image_num": 0,
        "user": {"urlname": "carula", "nickname": "コミックカルラ"},
        **overrides,
    }


@pytest.fixture
def api(fake_response):
    def make(payload, status_code=HTTPStatus.OK):
        return fake_response(payload=payload, status_code=status_code, content_type="application/json")

    return make


# --- parsing --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        (
            "『留学ろっく!!』 Lesson 1　パパはダイヤモンドチューバー‼ ",
            ("留学ろっく!!", "Lesson 1　パパはダイヤモンドチューバー‼"),
        ),
        ("読み切り『私の家族』", ("私の家族", "読み切り")),
        ("『皇帝列伝』プロローグ、第1話 カエサル", ("皇帝列伝", "プロローグ、第1話 カエサル")),
        ("『カイレコ』", ("カイレコ", "『カイレコ』")),
        ("コミックカルラです。", ("コミックカルラ", "コミックカルラです。")),
    ],
)
def test_split_title(name, expected):
    assert split_title(name, "コミックカルラ") == expected


def test_next_key_ignores_links_to_other_creators():
    body = (
        '<p><a href="https://note.com/other/n/nb016b73d0f1d">1</a>'
        '<a href="https://note.com/carula/n/nb016b73d0f1d">1</a>'
        '<a href="https://example.com/carula/n/n596c41e8118a">2</a></p>'
    )
    assert next_key(body, "nb016b73d0f1d") is None


@pytest.mark.parametrize(
    ("fields", "locked"),
    [
        ({}, False),
        ({"price": 100}, True),
        ({"price": 100, "is_purchased": True}, False),
        ({"remained_figure_num": 11}, True),
        ({"remained_image_num": 10}, True),
        ({"price": None, "remained_figure_num": None}, False),
    ],
)
def test_is_locked(fields, locked):
    assert is_locked(note(**fields)) is locked


# --- urls -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (EPISODE_URL, True),
        (EPISODE_URL + "/", True),
        (MAGAZINE_URL, True),
        (LEGACY_URL, True),
        ("http://note.com/carula/n/nb016b73d0f1d", False),
        ("https://note.com/other/n/nb016b73d0f1d", False),
        ("https://note.com/carula", False),
        ("https://note.com/carula/n/nb016b73d0f1d/edit", False),
        ("https://note.com/carula/n/xyz", False),
        ("https://carula.jp/", False),
        ("https://carula.jp/books", False),
        ("https://carula.jp/series/abc", False),
        ("https://www.note.com/carula/n/nb016b73d0f1d", False),
    ],
)
def test_suitable(url, expected):
    assert Carula.suitable(url) is expected


@pytest.mark.parametrize(
    ("url", "expected"),
    [(MAGAZINE_URL, True), (EPISODE_URL, False), (LEGACY_URL, False), ("https://carula.jp/", False)],
)
def test_is_series(url, expected):
    assert Carula.is_series(url) is expected


# --- episodes -------------------------------------------------------------------------------


def test_episode_reads_the_titles_the_pages_and_the_next_episode(fake_session, api):
    session = fake_session({"/api/v3/notes/nb016b73d0f1d": api({"data": note()})})
    episode = Carula(session).episode(EPISODE_URL)

    assert episode.url == EPISODE_URL
    assert episode.series_title == "留学ろっく!!"
    assert episode.episode_title == "Lesson 1　パパはダイヤモンドチューバー‼"
    assert [page.url for page in episode.pages] == [PAGE_1, PAGE_2]
    assert episode.next_url == NEXT_URL
    assert episode.metadata["key"] == "nb016b73d0f1d"
    assert session.calls == ["https://note.com/api/v3/notes/nb016b73d0f1d"]
    assert session.headers_seen[-1]["Accept"].startswith("application/json")


def test_episode_follows_the_legacy_catalogue_redirect(fake_session, fake_response, api):
    session = fake_session(
        {
            "carula.jp/series/": fake_response(text="<html></html>", url=EPISODE_URL),
            "/api/v3/notes/nb016b73d0f1d": api({"data": note()}),
        },
    )
    episode = Carula(session).episode(LEGACY_URL)

    assert episode.url == EPISODE_URL
    assert len(episode.pages) == 2
    assert session.calls == [LEGACY_URL, "https://note.com/api/v3/notes/nb016b73d0f1d"]


def test_episode_rejects_a_legacy_url_that_lands_elsewhere(fake_session, fake_response):
    session = fake_session({"carula.jp/series/": fake_response(text="<html></html>", url="https://carula.jp/")})
    with pytest.raises(NotAnEpisodePageError):
        Carula(session).episode(LEGACY_URL)


def test_paid_episode_is_locked_but_still_names_the_next_one(fake_session, api):
    preview = INDEX + f'<figure><img src="{PAGE_1}"></figure>'
    session = fake_session(
        {"/api/v3/notes/nb016b73d0f1d": api({"data": note(body=preview, price=100, remained_figure_num=11)})},
    )
    episode = Carula(session).episode(EPISODE_URL)

    assert not episode.readable
    assert episode.pages == ()
    assert episode.next_url == NEXT_URL
    assert episode.series_title == "留学ろっく!!"


def test_last_episode_has_no_next(fake_session, api):
    session = fake_session({"/api/v3/notes/na3813b19fd66": api({"data": note(key="na3813b19fd66")})})
    assert Carula(session).episode("https://note.com/carula/n/na3813b19fd66").next_url is None


def test_article_without_pages_is_not_an_episode(fake_session, api):
    session = fake_session({"/api/v3/notes/": api({"data": note(body="<p>コミックカルラです。</p>")})})
    with pytest.raises(NotAnEpisodePageError, match="no page image"):
        Carula(session).episode(EPISODE_URL)


def test_unknown_article_is_not_an_episode(fake_session, api):
    session = fake_session(
        {"/api/v3/notes/": api({"error": {"type": "not_found"}}, status_code=HTTPStatus.NOT_FOUND)},
    )
    with pytest.raises(NotAnEpisodePageError, match="no note article"):
        Carula(session).episode(EPISODE_URL)


def test_other_note_failures_raise(fake_session, api):
    session = fake_session({"/api/v3/notes/": api({}, status_code=HTTPStatus.INTERNAL_SERVER_ERROR)})
    with pytest.raises(Exception, match="500"):
        Carula(session).episode(EPISODE_URL)


# --- series ---------------------------------------------------------------------------------


def section(keys, *, last):
    return {"data": {"section": {"is_last_page": last, "contents": [{"key": k, "type": "TextNote"} for k in keys]}}}


def test_series_urls_walks_the_magazine_pages_in_order(fake_session, api):
    session = fake_session(
        {
            "/api/v1/layout/magazine/m98ee122ad3e2/section": [
                api(section(["nd2861191fd33", "n390e1d3052c7"], last=False)),
                api(section(["n390e1d3052c7", "n858bcc495d8e"], last=True)),
            ],
        },
    )
    urls = Carula(session).series_urls(MAGAZINE_URL)

    assert urls == [
        "https://note.com/carula/n/nd2861191fd33",
        "https://note.com/carula/n/n390e1d3052c7",
        "https://note.com/carula/n/n858bcc495d8e",
    ]
    assert all(Carula.suitable(url) for url in urls)
    assert session.params_seen == [{"page": 1}, {"page": 2}]


def test_series_urls_skips_what_is_not_an_article(fake_session, api):
    payload = section(["nd2861191fd33"], last=True)
    payload["data"]["section"]["contents"].append({"key": "m00b7c532c913", "type": "Magazine"})
    session = fake_session({"/section": api(payload)})
    assert Carula(session).series_urls(MAGAZINE_URL) == ["https://note.com/carula/n/nd2861191fd33"]


def test_series_urls_raises_on_an_empty_magazine(fake_session, api):
    session = fake_session({"/section": api(section([], last=True))})
    with pytest.raises(NotAnEpisodePageError):
        Carula(session).series_urls(MAGAZINE_URL)


def test_series_urls_rejects_an_episode_url(fake_session):
    with pytest.raises(UnsupportedUrlError):
        Carula(fake_session({})).series_urls(EPISODE_URL)


# --- downloading ----------------------------------------------------------------------------


def test_download_writes_the_pages(tmp_path, fake_session, fake_response, api):
    raw = BytesIO()
    Image.new("RGB", (4, 6), (10, 20, 30)).save(raw, "PNG")
    session = fake_session(
        {
            "/api/v3/notes/nb016b73d0f1d": api({"data": note()}),
            "assets.st-note.com": fake_response(raw.getvalue(), content_type="image/jpg"),
        },
    )
    result = Downloader(Carula(session), tmp_path).download(EPISODE_URL)

    assert result.status == "saved"
    assert result.save_dir == tmp_path / "留学ろっく!!" / "Lesson 1　パパはダイヤモンドチューバー‼"
    assert sorted(p.name for p in result.save_dir.iterdir()) == ["0.jpg", "1.jpg"]
    assert Image.open(result.save_dir / "0.jpg").size == (4, 6)
    assert session.headers_seen[-1]["Referer"] == EPISODE_URL


# --- the real site --------------------------------------------------------------------------

# One episode per known host, free to read without an account. The carula.jp
# one is the legacy work URL that redirects to Lesson 1 of 留学ろっく!!.
TEST_URLS: dict[str, str] = {
    "carula.jp": LEGACY_URL,
    "note.com": EPISODE_URL,
}


@pytest.mark.network
@pytest.mark.parametrize("host", TEST_URLS)
def test_site_download(tmp_path, host):
    result = Downloader(Carula(), tmp_path, only_first=True).download(TEST_URLS[host])
    assert result.status == "saved"
    assert result.episode.next_url == NEXT_URL
    assert (result.save_dir / "0.jpg").stat().st_size > 0


@pytest.mark.network
def test_site_paid_episode_is_locked():
    episode = Carula().episode(NEXT_URL)
    assert not episode.readable
    assert episode.next_url == "https://note.com/carula/n/n00b25b011cb5"


@pytest.mark.network
def test_site_magazine_lists_its_episodes():
    urls = Carula().series_urls(MAGAZINE_URL)
    assert urls[0] == "https://note.com/carula/n/nd2861191fd33"
    assert len(urls) > 14  # more than one page of the listing
    assert all(Carula.suitable(url) for url in urls)
