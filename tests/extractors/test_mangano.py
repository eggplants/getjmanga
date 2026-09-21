from __future__ import annotations

from http import HTTPStatus
from io import BytesIO

import pytest
from PIL import Image

from getjmanga.downloader import Downloader
from getjmanga.errors import GetjmangaError, LoginError, NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractors.mangano import (
    API_URL,
    MangaNo,
    episode_url,
    render_template,
)

WORK_ID = "104589b3d95bd166922"
EPISODE_ID = "1052a1e16e4b6e96958"
NEXT_ID = "105cb4445e4b6e96958"
PREV_ID = "105bb9006e4b6e96958"
PAID_ID = "1053c1226e4b6e96958"
WORK_URL = f"https://manga-no.com/works/{WORK_ID}"
EPISODE_URL = f"https://manga-no.com/episodes/{EPISODE_ID}"

SIGN_UP_URL = "https://identitytoolkit.googleapis.com/v1/accounts:signUp"
SIGN_IN_URL = "https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword"
REFRESH_URL = "https://securetoken.googleapis.com/v1/token"

SOURCE_1 = "https://img.manga-no.com/EPISODE_PAGE/d43bc9dc-5b35-4a3a-8262-6e3f65bbc146"
SOURCE_2 = "https://img.manga-no.com/EPISODE_PAGE/c8854bb7-6357-4bf9-8e70-1a036f6fc6db"
SOURCE_3 = "https://img.manga-no.com/EPISODE_PAGE/b53c7abc-19d8-431d-b7e1-365310771b46"


def template(source: str, signature: str = "962ed818fb1fec900bb729222062ee92f1edd9fe") -> str:
    """A scissors template URL the way the API writes one, wrapping `source`."""
    encoded = source.replace(":", "%3A").replace("/", "%2F")
    return (
        f"https://cdn-scissors.manga-no.com/image/scale/{signature}/height={{height}};no_unsharpmask=1;"
        f"quality=80;variable_params=height,width;version=1;width={{width}}/{encoded}"
    )


def page_node(page_id: str, source: str, width: int = 577, height: int = 800) -> dict:
    return {
        "node": {
            "id": page_id,
            "width": width,
            "height": height,
            "image": {"id": f"i{page_id}", "url": template(source)},
        }
    }


def episode_payload(
    *,
    edges: list[dict],
    has_next: bool = False,
    cursor: str | None = None,
    can_skip_paywall: bool = True,
    next_id: str | None = NEXT_ID,
    sales_info: dict | None = None,
    total: int | None = None,
    viewable: int | None = None,
) -> dict:
    """What `GetEpisode` answers for the example episode."""
    return {
        "data": {
            "node": {
                "__typename": "Episode",
                "id": EPISODE_ID,
                "title": "絵の話",
                "number": 5,
                "publicNumber": 5,
                "status": "PUBLIC",
                "startPosition": "FORMER",
                "afterword": "私の実体験をもとに描いています。",
                "canViewerSkipPaywall": can_skip_paywall,
                "canViewerSkipFanwall": True,
                "purchasedByViewer": False,
                "salesInfo": sales_info,
                "work": {
                    "id": WORK_ID,
                    "title": "小学生たちの日常（仮）",
                    "scrollDirection": "RIGHT_TO_LEFT",
                    "user": {
                        "id": "10118d71495bd166922",
                        "screenName": "T3rut3ru-P0n1t3",
                        "displayName": "Teruteru Ponite",
                    },
                },
                "previousEpisode": {"id": PREV_ID},
                "nextEpisode": {"id": next_id} if next_id else None,
                "pages": {
                    "totalCount": total if total is not None else len(edges),
                    "viewableCount": viewable if viewable is not None else len(edges),
                    "edges": edges,
                    "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
                },
            }
        }
    }


def work_payload(episode_ids: list[str], *, has_next: bool = False, cursor: str | None = None) -> dict:
    """What `GetWorkDetailAndEpisodes` answers for the example work."""
    return {
        "data": {
            "node": {
                "__typename": "Work",
                "id": WORK_ID,
                "title": "小学生たちの日常（仮）",
                "episodes": {
                    "totalCount": len(episode_ids),
                    "edges": [
                        {"node": {"id": episode_id, "title": f"第{index}話", "number": index, "status": "PUBLIC"}}
                        for index, episode_id in enumerate(episode_ids, 1)
                    ],
                    "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
                },
            }
        }
    }


NOT_FOUND = {
    "errors": [{"message": "not found", "path": ["node"], "extensions": {"code": "NOT_FOUND"}}],
    "data": {"node": None},
}
ANONYMOUS_TOKEN = {
    "kind": "identitytoolkit#SignupNewUserResponse",
    "idToken": "anon.id.token",
    "refreshToken": "anon.refresh",
    "expiresIn": "3600",
    "localId": "z85xs4onWjXX8V8DPci84h6jtO13",
}
ACCOUNT_TOKEN = {
    "kind": "identitytoolkit#VerifyPasswordResponse",
    "idToken": "account.id.token",
    "refreshToken": "account.refresh",
    "expiresIn": "3600",
    "email": "someone@example.com",
    "registered": True,
}
REFRESHED_TOKEN = {
    "id_token": "renewed.id.token",
    "refresh_token": "anon.refresh",
    "expires_in": "3600",
    "token_type": "Bearer",
}
WRONG_PASSWORD = {
    "error": {
        "code": 400,
        "message": "INVALID_LOGIN_CREDENTIALS",
        "errors": [{"message": "INVALID_LOGIN_CREDENTIALS", "domain": "global", "reason": "invalid"}],
    }
}


def jpeg_bytes(color: tuple[int, int, int] = (1, 2, 3), size: tuple[int, int] = (4, 6)) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", size, color).save(buffer, format="JPEG")
    return buffer.getvalue()


@pytest.fixture
def graphql_session(fake_session):
    """A fake session that routes GraphQL POSTs by operation, id and cursor.

    Every operation goes to the one `/query` URL, so a POST carrying a
    GraphQL body is matched as `<url>?opname=<operation>&id=<id>&after=<cursor>#`
    instead (the `#` keeps a route for the first page off the later ones).
    Other POSTs (Firebase) route by URL like a GET.
    """

    class GraphQLSession(fake_session):
        def post(self, url, data=None, json=None, **kwargs):
            self.headers_seen.append(kwargs.get("headers") or {})
            key = url
            if isinstance(json, dict) and json.get("operationName"):
                variables = json.get("variables") or {}
                operation, page_id, after = json["operationName"], variables.get("id", ""), variables.get("after") or ""
                key = f"{url}?opname={operation}&id={page_id}&after={after}#"
            self.posts.append((key, json if json is not None else data))
            return self._route(key)

    return GraphQLSession


@pytest.fixture
def client(graphql_session, fake_response):
    """A `MangaNo` over a session that signs in anonymously and knows the example pages."""

    def make(routes=None):
        routes = routes or {}
        defaults = {
            f"opname=GetEpisode&id={EPISODE_ID}&after=#": fake_response(
                payload=episode_payload(edges=[page_node("p1", SOURCE_1), page_node("p2", SOURCE_2, 564)])
            ),
            f"opname=GetEpisode&id={PAID_ID}&after=#": fake_response(
                payload=episode_payload(
                    edges=[page_node("p1", SOURCE_1)],
                    can_skip_paywall=False,
                    sales_info={"price": 500, "pagesChargedFrom": 19, "salesAppeal": "ぜひ"},
                    total=66,
                    viewable=19,
                )
            ),
            f"opname=GetEpisode&id={WORK_ID}&after=#": fake_response(
                payload={"data": {"node": {"__typename": "Work", "id": WORK_ID}}}
            ),
            "opname=GetEpisode&id=deadbeef&after=#": fake_response(payload=NOT_FOUND),
            f"opname=GetWorkDetailAndEpisodes&id={WORK_ID}&after=#": fake_response(
                payload=work_payload([PREV_ID, EPISODE_ID, EPISODE_ID, NEXT_ID])
            ),
            "opname=GetWorkDetailAndEpisodes&id=deadbeef&after=#": fake_response(payload=NOT_FOUND),
            SIGN_UP_URL: fake_response(payload=ANONYMOUS_TOKEN),
            SOURCE_1: fake_response(jpeg_bytes(), content_type="image/png"),
        }
        # A test's own routes come first, so they win over the defaults.
        session = graphql_session({**routes, **{key: value for key, value in defaults.items() if key not in routes}})
        return MangaNo(session), session

    return make


# --- URLs ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        EPISODE_URL,
        f"{EPISODE_URL}/",
        WORK_URL,
        f"{WORK_URL}/",
    ],
)
def test_suitable_accepts_episode_and_work_urls(url):
    assert MangaNo.suitable(url)


@pytest.mark.parametrize(
    "url",
    [
        f"http://manga-no.com/episodes/{EPISODE_ID}",
        "https://manga-no.com/",
        "https://manga-no.com/episodes/",
        "https://manga-no.com/@T3rut3ru-P0n1t3/manga",
        "https://manga-no.com/rankings/total",
        f"https://manga-no.com/works/{WORK_ID}/episodes",
        f"https://www.manga-no.com/episodes/{EPISODE_ID}",
        f"https://shonenjumpplus.com/episode/{EPISODE_ID}",
    ],
)
def test_suitable_rejects_other_urls(url):
    assert not MangaNo.suitable(url)


@pytest.mark.parametrize(
    ("url", "expected"), [(WORK_URL, True), (EPISODE_URL, False), ("https://manga-no.com/", False)]
)
def test_is_series(url, expected):
    assert MangaNo.is_series(url) is expected


# --- image URLs ---------------------------------------------------------------------


# --- episode ------------------------------------------------------------------------


def test_episode_reads_the_titles_the_pages_and_the_next_episode(client):
    mangano, session = client()
    episode = mangano.episode(EPISODE_URL)

    assert episode.url == EPISODE_URL
    assert episode.series_title == "小学生たちの日常（仮）"
    assert episode.episode_title == "絵の話"
    assert [page.url for page in episode.pages] == [SOURCE_1, SOURCE_2]
    assert [(page.width, page.height) for page in episode.pages] == [(577, 800), (564, 800)]
    assert episode.pages[0].extra == {"template": template(SOURCE_1), "id": "p1"}
    assert (episode.prev_url, episode.next_url) == (episode_url(PREV_ID), episode_url(NEXT_ID))
    assert episode.metadata["prev_url"] == episode_url(PREV_ID)
    assert episode.metadata["work_url"] == WORK_URL
    assert episode.metadata["locked"] is False
    assert episode.metadata["images"] == [template(SOURCE_1), template(SOURCE_2)]
    assert episode.metadata["scroll_direction"] == "RIGHT_TO_LEFT"
    assert episode.metadata["author"] == "Teruteru Ponite"
    assert (episode.writer, episode.publisher) == ("Teruteru Ponite", "はてな")
    assert episode.number == 5

    # An anonymous Firebase sign-up first, then the query with its token.
    assert session.posts[0] == (SIGN_UP_URL, {"returnSecureToken": True})
    query_url, body = session.posts[1]
    assert query_url.startswith(API_URL)
    assert body["operationName"] == "GetEpisode"
    assert body["variables"] == {"id": EPISODE_ID, "first": 200, "after": None}
    assert session.headers_seen[-1]["Authorization"] == "Bearer anon.id.token"
    assert session.headers_seen[-1]["Referer"] == EPISODE_URL


def test_episode_accepts_a_trailing_slash(client):
    mangano, _ = client()
    assert mangano.episode(f"{EPISODE_URL}/").url == EPISODE_URL


def test_episode_walks_the_pages_connection(client, fake_response):
    mangano, session = client(
        {
            f"opname=GetEpisode&id={EPISODE_ID}&after=#": fake_response(
                payload=episode_payload(
                    edges=[page_node("p1", SOURCE_1), page_node("p2", SOURCE_2)], has_next=True, cursor="c2"
                )
            ),
            f"opname=GetEpisode&id={EPISODE_ID}&after=c2#": fake_response(
                payload=episode_payload(edges=[page_node("p3", SOURCE_3)], has_next=False, cursor="c3")
            ),
        }
    )
    episode = mangano.episode(EPISODE_URL)

    assert [page.url for page in episode.pages] == [SOURCE_1, SOURCE_2, SOURCE_3]
    assert [body["variables"]["after"] for _, body in session.posts[1:]] == [None, "c2"]


def test_episode_falls_back_to_the_proxy_url_when_the_template_wraps_no_file(client, fake_response):
    mangano, _ = client(
        {
            f"opname=GetEpisode&id={EPISODE_ID}&after=#": fake_response(
                payload=episode_payload(
                    edges=[
                        {
                            "node": {
                                "id": "p1",
                                "width": 577,
                                "height": 800,
                                "image": {
                                    "id": "i1",
                                    "url": "https://cdn-scissors.manga-no.com/x/height={height};width={width}/plain",
                                },
                            }
                        }
                    ]
                )
            ),
        }
    )
    episode = mangano.episode(EPISODE_URL)

    assert episode.pages[0].url == "https://cdn-scissors.manga-no.com/x/height=800;width=577/plain"


def test_episode_without_a_title_is_named_by_its_number(client, fake_response):
    payload = episode_payload(edges=[page_node("p1", SOURCE_1)], next_id=None)
    payload["data"]["node"]["title"] = ""
    mangano, _ = client({f"opname=GetEpisode&id={EPISODE_ID}&after=#": fake_response(payload=payload)})
    episode = mangano.episode(EPISODE_URL)

    assert episode.episode_title == "第5話"
    assert episode.next_url is None


def test_paid_episode_has_no_pages_but_keeps_its_titles_and_the_next_episode(client):
    mangano, _ = client()
    episode = mangano.episode(f"https://manga-no.com/episodes/{PAID_ID}")

    assert not episode.readable
    assert episode.pages == ()
    assert episode.episode_title == "絵の話"
    assert episode.next_url == episode_url(NEXT_ID)
    assert episode.metadata["locked"] is True
    assert episode.metadata["sales_info"] == {"price": 500, "pagesChargedFrom": 19, "salesAppeal": "ぜひ"}
    assert (episode.metadata["viewable_count"], episode.metadata["page_count"]) == (19, 66)
    # The preview the API listed is still recorded, for `--metadata`.
    assert episode.metadata["images"] == [template(SOURCE_1)]


def test_unknown_episode_is_not_an_episode_page(client):
    mangano, _ = client()
    with pytest.raises(NotAnEpisodePageError, match="no such page"):
        mangano.episode("https://manga-no.com/episodes/deadbeef")


def test_a_work_id_is_not_an_episode_page(client):
    mangano, _ = client()
    with pytest.raises(NotAnEpisodePageError, match="not an episode"):
        mangano.episode(f"https://manga-no.com/episodes/{WORK_ID}")


def test_episode_rejects_a_work_url(client):
    mangano, _ = client()
    with pytest.raises(NotAnEpisodePageError, match="not an episode page"):
        mangano.episode(WORK_URL)


def test_another_api_error_is_reported_as_such(client, fake_response):
    mangano, _ = client(
        {
            f"opname=GetEpisode&id={EPISODE_ID}&after=#": fake_response(
                payload={"errors": [{"message": "internal", "extensions": {"code": "INTERNAL"}}], "data": None}
            )
        }
    )
    with pytest.raises(GetjmangaError, match="internal"):
        mangano.episode(EPISODE_URL)


def test_api_failure_status_is_raised(client, fake_response):
    mangano, _ = client(
        {
            f"opname=GetEpisode&id={EPISODE_ID}&after=#": fake_response(
                text="bad gateway", status_code=HTTPStatus.BAD_GATEWAY
            )
        }
    )
    with pytest.raises(Exception, match="502"):
        mangano.episode(EPISODE_URL)


# --- the token ----------------------------------------------------------------------


def test_the_anonymous_token_is_fetched_once_and_reused(client):
    mangano, session = client()
    mangano.episode(EPISODE_URL)
    mangano.episode(EPISODE_URL)

    assert [url for url, _ in session.posts].count(SIGN_UP_URL) == 1
    assert all(headers.get("Authorization") == "Bearer anon.id.token" for headers in session.headers_seen[1:])


def test_a_refused_token_is_renewed_and_the_query_retried(client, fake_response):
    mangano, session = client(
        {
            f"opname=GetEpisode&id={EPISODE_ID}&after=#": [
                fake_response(text="Unauthorized", status_code=HTTPStatus.UNAUTHORIZED),
                fake_response(payload=episode_payload(edges=[page_node("p1", SOURCE_1)])),
            ],
            REFRESH_URL: fake_response(payload=REFRESHED_TOKEN),
        }
    )
    episode = mangano.episode(EPISODE_URL)

    assert len(episode.pages) == 1
    assert [url for url, _ in session.posts] == [
        SIGN_UP_URL,
        f"{API_URL}?opname=GetEpisode&id={EPISODE_ID}&after=#",
        REFRESH_URL,
        f"{API_URL}?opname=GetEpisode&id={EPISODE_ID}&after=#",
    ]
    assert session.posts[2][1] == {"grant_type": "refresh_token", "refresh_token": "anon.refresh"}
    assert session.headers_seen[-1]["Authorization"] == "Bearer renewed.id.token"


def test_an_expired_token_is_renewed_before_the_next_query(client, fake_response, monkeypatch):
    mangano, session = client({REFRESH_URL: fake_response(payload=REFRESHED_TOKEN)})
    now = 1000.0
    monkeypatch.setattr("getjmanga.extractors.mangano.time.monotonic", lambda: now)
    mangano.episode(EPISODE_URL)
    now += 3600.0
    mangano.episode(EPISODE_URL)

    assert [url for url, _ in session.posts].count(REFRESH_URL) == 1
    assert session.headers_seen[-1]["Authorization"] == "Bearer renewed.id.token"


def test_a_failed_refresh_signs_in_anonymously_again(client, fake_response):
    mangano, session = client(
        {
            f"opname=GetEpisode&id={EPISODE_ID}&after=#": [
                fake_response(text="Unauthorized", status_code=HTTPStatus.UNAUTHORIZED),
                fake_response(payload=episode_payload(edges=[page_node("p1", SOURCE_1)])),
            ],
            REFRESH_URL: fake_response(
                payload={"error": {"code": 400, "message": "TOKEN_EXPIRED"}}, status_code=HTTPStatus.BAD_REQUEST
            ),
        }
    )
    mangano.episode(EPISODE_URL)

    assert [url for url, _ in session.posts].count(SIGN_UP_URL) == 2


def test_a_sign_up_without_a_token_is_an_error(client, fake_response):
    mangano, _ = client({SIGN_UP_URL: fake_response(payload={"kind": "identitytoolkit#SignupNewUserResponse"})})
    with pytest.raises(GetjmangaError, match="no anonymous token"):
        mangano.episode(EPISODE_URL)


# --- series -------------------------------------------------------------------------


def test_series_urls_lists_the_episodes_in_order_without_repeats(client):
    mangano, session = client()
    urls = mangano.series_urls(WORK_URL)

    assert urls == [episode_url(PREV_ID), EPISODE_URL, episode_url(NEXT_ID)]
    assert all(MangaNo.suitable(url) for url in urls)
    assert session.posts[-1][1]["variables"] == {"id": WORK_ID, "first": 100, "after": None}


def test_series_urls_walks_the_episodes_connection(client, fake_response):
    mangano, session = client(
        {
            f"opname=GetWorkDetailAndEpisodes&id={WORK_ID}&after=#": fake_response(
                payload=work_payload([PREV_ID, EPISODE_ID], has_next=True, cursor="e2")
            ),
            f"opname=GetWorkDetailAndEpisodes&id={WORK_ID}&after=e2#": fake_response(
                payload=work_payload([NEXT_ID], has_next=False, cursor="e3")
            ),
        }
    )
    urls = mangano.series_urls(WORK_URL)

    assert urls == [episode_url(PREV_ID), EPISODE_URL, episode_url(NEXT_ID)]
    assert [body["variables"]["after"] for _, body in session.posts[1:]] == [None, "e2"]


def test_series_urls_rejects_an_episode_url(client):
    mangano, _ = client()
    with pytest.raises(UnsupportedUrlError, match="not a work page"):
        mangano.series_urls(EPISODE_URL)


def test_series_urls_raises_on_an_unknown_work(client):
    mangano, _ = client()
    with pytest.raises(NotAnEpisodePageError, match="no such page"):
        mangano.series_urls("https://manga-no.com/works/deadbeef")


def test_series_urls_raises_when_the_id_is_not_a_work(client, fake_response):
    mangano, _ = client(
        {
            f"opname=GetWorkDetailAndEpisodes&id={WORK_ID}&after=#": fake_response(
                payload={"data": {"node": {"__typename": "Episode", "id": WORK_ID}}}
            )
        }
    )
    with pytest.raises(NotAnEpisodePageError, match="not a work page"):
        mangano.series_urls(WORK_URL)


def test_series_urls_raises_on_an_empty_listing(client, fake_response):
    mangano, _ = client(
        {f"opname=GetWorkDetailAndEpisodes&id={WORK_ID}&after=#": fake_response(payload=work_payload([]))}
    )
    with pytest.raises(NotAnEpisodePageError, match="no episode"):
        mangano.series_urls(WORK_URL)


# --- images and the download --------------------------------------------------------


def test_image_fetches_the_source_file_with_the_episode_as_referer(client):
    mangano, session = client()
    episode = mangano.episode(EPISODE_URL)
    image = mangano.image(episode.pages[0], episode)

    assert image.size == (4, 6)
    assert session.calls == [SOURCE_1]
    assert session.headers_seen[-1]["Referer"] == EPISODE_URL


def test_image_falls_back_to_the_proxy_when_the_source_answers_an_error(client, fake_response):
    proxy_url = render_template(template(SOURCE_1), 577, 800)
    mangano, session = client(
        {
            SOURCE_1: fake_response(text="forbidden", status_code=HTTPStatus.FORBIDDEN),
            "cdn-scissors.manga-no.com": fake_response(jpeg_bytes((9, 9, 9)), content_type="image/jpeg"),
        }
    )
    episode = mangano.episode(EPISODE_URL)
    image = mangano.image(episode.pages[0], episode)

    assert image.getpixel((0, 0)) == (9, 9, 9)
    assert session.calls == [SOURCE_1, proxy_url]


# --- login --------------------------------------------------------------------------


def test_login_posts_the_credentials_and_uses_the_account_token(client, fake_response):
    mangano, session = client({SIGN_IN_URL: fake_response(payload=ACCOUNT_TOKEN)})
    mangano.login("https://manga-no.com/", "someone@example.com", "hunter2")
    mangano.episode(EPISODE_URL)

    assert session.posts[0] == (
        SIGN_IN_URL,
        {"email": "someone@example.com", "password": "hunter2", "returnSecureToken": True},
    )
    assert session.headers_seen[0]["Referer"] == "https://manga-no.com/"
    assert [url for url, _ in session.posts].count(SIGN_UP_URL) == 0
    assert session.headers_seen[-1]["Authorization"] == "Bearer account.id.token"


def test_login_raises_with_the_site_reason(client, fake_response):
    mangano, _ = client({SIGN_IN_URL: fake_response(payload=WRONG_PASSWORD, status_code=HTTPStatus.BAD_REQUEST)})
    with pytest.raises(LoginError, match="INVALID_LOGIN_CREDENTIALS"):
        mangano.login("https://manga-no.com/", "someone@example.com", "wrong")


def test_login_raises_when_no_token_comes_back(client, fake_response):
    mangano, _ = client({SIGN_IN_URL: fake_response(text="<html>not json</html>")})
    with pytest.raises(LoginError, match="refused the credentials"):
        mangano.login("https://manga-no.com/", "someone@example.com", "hunter2")


# --- the real site --------------------------------------------------------------------

# One free episode per known host: the first episode of the site's most-read work.
TEST_URLS: dict[str, str] = {
    "manga-no.com": "https://manga-no.com/episodes/10550450214407d6064",
}


@pytest.mark.network
@pytest.mark.parametrize("host", TEST_URLS)
def test_site_download(tmp_path, host):
    result = Downloader(MangaNo(), tmp_path, only_first=True).download(TEST_URLS[host])
    assert result.status == "saved"
    assert (result.save_dir / "0.jpg").stat().st_size > 0


@pytest.mark.network
def test_work_page_lists_episodes():
    urls = MangaNo().series_urls("https://manga-no.com/works/1044f450214407d6064")
    assert urls[0] == TEST_URLS["manga-no.com"]
    assert len(urls) > 100  # more than one page of the connection
    assert all(MangaNo.suitable(url) for url in urls)


@pytest.mark.network
def test_paid_episode_is_locked():
    episode = MangaNo().episode("https://manga-no.com/episodes/1053c1226e4b6e96958")
    assert not episode.readable
    assert episode.metadata["sales_info"]["price"] > 0
