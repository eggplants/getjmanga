from __future__ import annotations

import json
from hashlib import sha256
from http import HTTPStatus
from io import BytesIO

import pytest
from PIL import Image

from getjmanga.downloader import Downloader
from getjmanga.errors import LoginError, NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractors.ganma import (
    BASE_URL,
    GRAPHQL_URL,
    LOGIN_URL,
    Ganma,
    episode_url,
)

MAGAZINE_ID = "5cbfdc20-9314-11ed-8541-6662d61d065d"
STORY_1 = "2b38ac10-a5c8-11ed-aeb5-aaf864fe781e"
STORY_2 = "2b380fd0-a5c8-11ed-aeb5-aaf864fe781e"
STORY_3 = "682850d0-b0c2-11ed-b286-aed1631da773"
STORY_4 = "4c160900-d1e2-11ed-a1b2-ee8d727a0508"
SERIES_URL = f"{BASE_URL}/web/magazine/wolfchan"
EPISODE_URL = f"{BASE_URL}/web/reader/wolfchan/{STORY_2}/0"
LOCKED_URL = f"{BASE_URL}/web/reader/wolfchan/{STORY_3}/0"
CDN = "https://d1bzi54d5ruxfk.cloudfront.net"
PAGE_BASE = f"{CDN}/story_{STORY_2}_pages_1675998570000_orig/"
SIGN = "Policy=eyJTdGF0ZW1lbnQi&Signature=FQk8ORm4IATL&Key-Pair-Id=K2H5CJD39JQYP6"

MAGAZINE = {
    "authorName": "ホンノシオリ",
    "magazineId": MAGAZINE_ID,
    "alias": "wolfchan",
    "title": "ウルフちゃんは澄ましたい",
    "storyLimitCount": 2,
    "totalStoryCount": 4,
}

# `magazineStoryForReader` for a readable story, cut down to what is read.
READER = {
    "data": {
        "magazine": {
            **MAGAZINE,
            "storyContents": {
                "__typename": "StoryContents",
                "storyInfo": {
                    "isVerticalOnly": False,
                    "title": "第2話",
                    "subtitle": "占いとお菓子",
                    "nextStoryInfo": {"isStoryCountLimited": True, "storyId": STORY_3, "title": "第3話"},
                    "previousStoryInfo": {"storyId": STORY_1},
                },
                "pageImages": {"pageCount": 2, "pageImageBaseURL": PAGE_BASE, "pageImageSign": SIGN},
                "afterword": {"imageURL": f"{CDN}/story_afterwordImage/x.jpg?{SIGN}", "text": None},
            },
        }
    }
}
PAGES = [f"{PAGE_BASE}1.jpg?{SIGN}", f"{PAGE_BASE}2.jpg?{SIGN}"]


def reader_error(error):
    return {"data": {"magazine": {**MAGAZINE, "storyContents": {"__typename": "StoryContentsError", "error": error}}}}


def story(story_id, title, subtitle, error=None):
    contents = (
        {"__typename": "StoryContents"} if error is None else {"__typename": "StoryContentsError", "error": error}
    )
    return {
        "__typename": "StoryInfo",
        "storyId": story_id,
        "title": title,
        "subtitle": subtitle,
        "isLast": False,
        "contentsAccessCondition": {"__typename": "FreeStoryContentsAccessCondition", "disableCM": True},
        "isPurchased": False,
        "storyContents": contents,
    }


def listing(stories, *, has_next=False, end_cursor="Y3Vyc29yOjM="):
    return {
        "data": {
            "magazine": {
                **{key: MAGAZINE[key] for key in ("magazineId", "totalStoryCount", "title", "authorName")},
                "storyInfos": {
                    "pageInfo": {"endCursor": end_cursor, "hasNextPage": has_next},
                    "edges": [{"cursor": f"c{i}", "node": node} for i, node in enumerate(stories)],
                },
            }
        }
    }


STORIES = [
    story(STORY_1, "第1話", "一匹狼"),
    story(STORY_2, "第2話", "占いとお菓子"),
    story(STORY_3, "第3話", "保健室と相合傘", "STORY_COUNT_LIMITED"),
    story(STORY_4, "第4話", "隣の席とコーヒー", "STORY_COUNT_LIMITED"),
]
LISTING = listing(STORIES)
NO_MAGAZINE = {"data": {"magazine": None}}


def png_bytes(color=(1, 2, 3), size=(8, 8)):
    raw = BytesIO()
    Image.new("RGB", size, color).save(raw, "PNG")
    return raw.getvalue()


@pytest.fixture
def client(fake_session, fake_response):
    """A `Ganma` over a session answering the GraphQL calls in the given order, and a page image."""

    def build(answers, extra=None):
        routes = {
            **(extra or {}),
            GRAPHQL_URL: [fake_response(payload=answer, content_type="application/json") for answer in answers],
            CDN: fake_response(png_bytes(), content_type="image/png"),
        }
        session = fake_session(routes)
        return Ganma(session), session

    return build


# --- URLs ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        EPISODE_URL,
        f"https://ganma.jp/web/reader/wolfchan/{STORY_2}/3",
        f"https://ganma.jp/web/reader/wolfchan/{STORY_2}",
        f"https://ganma.jp/web/reader/{MAGAZINE_ID}/{STORY_2}/0",
        SERIES_URL,
        "https://ganma.jp/web/magazine/wolfchan/",
        f"https://ganma.jp/web/magazine/{MAGAZINE_ID}",
        "https://ganma.jp/web/magazine/wolfchan?error=STORY_COUNT_LIMITED&episodeId=x",
        "https://ganma.jp/magazine/wolfchan",
        "https://ganma.jp/wolfchan",
        "https://ganma.jp/chiharasan_GTOON",
    ],
)
def test_suitable_accepts_reader_and_magazine_urls(url):
    assert Ganma.suitable(url)


@pytest.mark.parametrize(
    "url",
    [
        f"http://ganma.jp/web/reader/wolfchan/{STORY_2}/0",
        "https://ganma.jp/",
        "https://ganma.jp/web",
        "https://ganma.jp/web/",
        "https://ganma.jp/web/store",
        "https://ganma.jp/web/signin",
        "https://ganma.jp/g/about/",
        "https://ganma.jp/api/graphql",
        "https://ganma.jp/magazine",
        "https://ganma.jp/web/magazineTag/4226f40b-a82c-44e3-b7d4-c29410538cc6",
        "https://ganma.jp/web/store/comics/5cbfdc20-9314-11ed-8541-6662d61d065d/1",
        "https://ganma.jp/web/reader/wolfchan/123/0",
        "https://store.ganma.jp/comics/wolfchan/",
        "https://piccoma.com/web/viewer/8195/1185884",
    ],
)
def test_suitable_rejects_other_urls(url):
    assert not Ganma.suitable(url)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (EPISODE_URL, False),
        (SERIES_URL, True),
        ("https://ganma.jp/magazine/wolfchan", True),
        ("https://ganma.jp/wolfchan", True),
        ("https://ganma.jp/web", False),
    ],
)
def test_is_series(url, expected):
    assert Ganma.is_series(url) is expected


# --- episodes --------------------------------------------------------------------------


def test_episode_reads_the_titles_the_pages_and_the_next_story(client):
    ganma, session = client([READER])
    episode = ganma.episode(EPISODE_URL)

    assert episode.url == EPISODE_URL
    assert episode.series_title == "ウルフちゃんは澄ましたい"
    assert episode.episode_title == "第2話 占いとお菓子"
    assert [page.url for page in episode.pages] == PAGES
    assert all(page.extra == {} for page in episode.pages)
    assert episode.next_url == LOCKED_URL
    assert episode.metadata["storyContents"]["pageImages"]["pageCount"] == 2
    json.dumps(episode.metadata)

    # One persisted query, with the site's headers.
    assert len(session.posts) == 1
    url, body = session.posts[0]
    assert url == GRAPHQL_URL
    assert body["operationName"] == "magazineStoryForReader"
    assert body["variables"] == {"magazineIdOrAlias": "wolfchan", "storyId": STORY_2}
    assert body["extensions"]["persistedQuery"] == {
        "version": 1,
        "sha256Hash": "3304274fe222a3d39fb10227b8aff56bdd2bccfab8b4765f2ed8e20b56cf77c1",
    }
    assert sha256(body["query"].encode()).hexdigest() == body["extensions"]["persistedQuery"]["sha256Hash"]


def test_episode_sends_the_page_as_x_from(fake_response, fake_session):
    seen = {}

    class Session(fake_session):
        def post(self, url, data=None, json=None, **kwargs):
            seen.update(kwargs.get("headers") or {})
            return super().post(url, data, json, **kwargs)

    session = Session({GRAPHQL_URL: fake_response(payload=READER)})
    Ganma(session).episode(EPISODE_URL)
    assert seen["x-from"] == EPISODE_URL
    assert seen["x-noescape"] == "true"
    assert seen["Content-Type"].startswith("application/json")


def test_episode_accepts_a_magazine_id_and_no_page_number(client):
    ganma, session = client([READER])
    episode = ganma.episode(f"https://ganma.jp/web/reader/{MAGAZINE_ID}/{STORY_2}")
    assert episode.url == f"{BASE_URL}/web/reader/{MAGAZINE_ID}/{STORY_2}/0"
    assert episode.next_url == f"{BASE_URL}/web/reader/{MAGAZINE_ID}/{STORY_3}/0"
    assert session.posts[0][1]["variables"]["magazineIdOrAlias"] == MAGAZINE_ID


def test_episode_without_a_next_story_ends_the_chain(client):
    last = json.loads(json.dumps(READER))
    last["data"]["magazine"]["storyContents"]["storyInfo"]["nextStoryInfo"] = None
    ganma, _ = client([last])
    assert ganma.episode(EPISODE_URL).next_url is None


def test_locked_episode_has_no_pages_but_is_named_by_the_listing(client):
    # Every `StoryContentsError` but `STORY_NOT_FOUND` takes this path.
    error = "STORY_COUNT_LIMITED"
    ganma, session = client([reader_error(error), LISTING])
    episode = ganma.episode(LOCKED_URL)

    assert episode.pages == ()
    assert not episode.readable
    assert episode.series_title == "ウルフちゃんは澄ましたい"
    assert episode.episode_title == "第3話 保健室と相合傘"
    assert episode.next_url == f"{BASE_URL}/web/reader/wolfchan/{STORY_4}/0"
    assert episode.metadata["storyContents"]["error"] == error
    assert episode.metadata["storyInfo"]["storyId"] == STORY_3
    json.dumps(episode.metadata)
    assert [body["operationName"] for _, body in session.posts] == ["magazineStoryForReader", "storyInfoList"]
    assert session.posts[1][1]["variables"] == {"magazineIdOrAlias": "wolfchan", "first": 100, "after": None}


def test_last_locked_episode_has_no_next(client):
    ganma, _ = client([reader_error("STORY_COUNT_LIMITED"), LISTING])
    episode = ganma.episode(f"{BASE_URL}/web/reader/wolfchan/{STORY_4}/0")
    assert episode.episode_title == "第4話 隣の席とコーヒー"
    assert episode.next_url is None


def test_locked_episode_off_the_listing_falls_back_to_its_id(client):
    ganma, _ = client([reader_error("STORY_NOT_PURCHASED"), listing([])])
    episode = ganma.episode(f"{BASE_URL}/web/reader/wolfchan/{STORY_4}/0")
    assert episode.episode_title == STORY_4
    assert episode.next_url is None
    assert not episode.readable


def test_unknown_story_is_not_an_episode_page(client):
    ganma, _ = client([reader_error("STORY_NOT_FOUND")])
    with pytest.raises(NotAnEpisodePageError, match="no story"):
        ganma.episode(f"{BASE_URL}/web/reader/wolfchan/00000000-0000-0000-0000-000000000000/0")


def test_unknown_magazine_is_not_an_episode_page(client):
    ganma, _ = client([NO_MAGAZINE])
    with pytest.raises(NotAnEpisodePageError, match="no magazine 'nope'"):
        ganma.episode(f"{BASE_URL}/web/reader/nope/{STORY_2}/0")


def test_refused_query_is_not_an_episode_page(client):
    ganma, _ = client([{"data": None, "errors": [{"message": "OnlyPersistedQueryIsAllowed"}]}])
    with pytest.raises(NotAnEpisodePageError, match="OnlyPersistedQueryIsAllowed"):
        ganma.episode(EPISODE_URL)


def test_failing_status_raises(client, fake_response, fake_session):
    session = fake_session({GRAPHQL_URL: fake_response(text="", status_code=HTTPStatus.BAD_REQUEST)})
    with pytest.raises(Exception, match="400"):
        Ganma(session).episode(EPISODE_URL)


def test_episode_rejects_a_series_url(client):
    ganma, _ = client([READER])
    with pytest.raises(UnsupportedUrlError):
        ganma.episode(SERIES_URL)


# --- series ----------------------------------------------------------------------------


@pytest.mark.parametrize("url", [SERIES_URL, "https://ganma.jp/magazine/wolfchan/", "https://ganma.jp/wolfchan"])
def test_series_urls_lists_the_stories_in_order(client, url):
    ganma, _ = client([LISTING])
    urls = ganma.series_urls(url)
    assert urls == [episode_url("wolfchan", story_id) for story_id in (STORY_1, STORY_2, STORY_3, STORY_4)]
    assert all(Ganma.suitable(candidate) for candidate in urls)


def test_series_urls_keeps_a_magazine_id(client):
    ganma, session = client([LISTING])
    urls = ganma.series_urls(f"https://ganma.jp/web/magazine/{MAGAZINE_ID}")
    assert urls[0] == f"{BASE_URL}/web/reader/{MAGAZINE_ID}/{STORY_1}/0"
    assert session.posts[0][1]["variables"]["magazineIdOrAlias"] == MAGAZINE_ID


def test_series_urls_follows_the_cursor(client):
    first = listing(STORIES[:2], has_next=True, end_cursor="Y3Vyc29yOjE=")
    second = listing(STORIES[2:], has_next=False)
    ganma, session = client([first, second])
    assert len(ganma.series_urls(SERIES_URL)) == 4
    assert [body["variables"]["after"] for _, body in session.posts] == [None, "Y3Vyc29yOjE="]


def test_series_urls_drops_a_repeated_entry(client):
    ganma, _ = client([listing([STORIES[0], STORIES[0], STORIES[1]])])
    assert ganma.series_urls(SERIES_URL) == [episode_url("wolfchan", STORY_1), episode_url("wolfchan", STORY_2)]


def test_series_urls_rejects_a_reader_url(client):
    ganma, _ = client([LISTING])
    with pytest.raises(UnsupportedUrlError):
        ganma.series_urls(EPISODE_URL)


def test_series_urls_raises_on_an_empty_listing(client):
    ganma, _ = client([listing([])])
    with pytest.raises(NotAnEpisodePageError, match="lists no story"):
        ganma.series_urls(SERIES_URL)


def test_series_urls_raises_on_an_unknown_magazine(client):
    ganma, _ = client([NO_MAGAZINE])
    with pytest.raises(NotAnEpisodePageError, match="no magazine 'nope'"):
        ganma.series_urls("https://ganma.jp/web/magazine/nope")


# --- images ----------------------------------------------------------------------------


def test_image_is_fetched_as_served_with_the_reader_as_referer(client):
    ganma, session = client([READER])
    episode = ganma.episode(EPISODE_URL)
    image = ganma.image(episode.pages[0], episode)

    assert image.convert("RGB").getpixel((0, 0)) == (1, 2, 3)
    assert session.calls == [PAGES[0]]
    assert session.headers_seen[-1]["Referer"] == EPISODE_URL


# --- logging in ------------------------------------------------------------------------


def test_login_posts_the_credentials_as_json(fake_session, fake_response):
    session = fake_session({LOGIN_URL: fake_response(payload={"success": True}, content_type="application/json")})
    Ganma(session).login(EPISODE_URL, "someone@example.com", "hunter2")

    assert session.calls == []
    url, body = session.posts[0]
    assert url == LOGIN_URL
    assert body == {"mail": "someone@example.com", "password": "hunter2"}


def test_login_raises_with_the_site_reason(fake_session, fake_response):
    refused = {"success": False, "message": "Authentication failed", "code": "notFound"}
    session = fake_session(
        {LOGIN_URL: fake_response(payload=refused, status_code=HTTPStatus.NOT_FOUND, content_type="application/json")}
    )
    with pytest.raises(LoginError, match="Authentication failed"):
        Ganma(session).login(EPISODE_URL, "someone@example.com", "wrong")


def test_login_raises_on_a_bare_failing_status(fake_session, fake_response):
    session = fake_session({LOGIN_URL: fake_response(text="", status_code=HTTPStatus.TOO_MANY_REQUESTS)})
    with pytest.raises(LoginError, match="HTTP 429"):
        Ganma(session).login(EPISODE_URL, "someone@example.com", "wrong")


# --- the real site ---------------------------------------------------------------------

# One free episode per known host: the first story of a long-running magazine.
TEST_URLS: dict[str, str] = {
    "ganma.jp": f"https://ganma.jp/web/reader/wolfchan/{STORY_1}/0",
}


@pytest.mark.network
@pytest.mark.parametrize("host", TEST_URLS)
def test_site_download(tmp_path, host):
    result = Downloader(Ganma(), tmp_path, only_first=True).download(TEST_URLS[host])
    assert result.status == "saved"
    assert (result.save_dir / "0.jpg").stat().st_size > 0


@pytest.mark.network
def test_site_app_only_episode_has_no_pages():
    # The web reader stops after the magazine's `storyLimitCount` stories.
    episode = Ganma().episode(f"https://ganma.jp/web/reader/wolfchan/{STORY_4}/0")
    assert not episode.readable
    assert episode.episode_title.startswith("第6話")
    assert episode.next_url is not None


@pytest.mark.network
def test_site_magazine_lists_stories():
    urls = Ganma().series_urls("https://ganma.jp/web/magazine/wolfchan")
    assert urls[0] == TEST_URLS["ganma.jp"]
    assert len(urls) > 5
    assert all(Ganma.suitable(url) for url in urls)
