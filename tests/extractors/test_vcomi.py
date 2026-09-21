from __future__ import annotations

import json
import math
from datetime import date
from http import HTTPStatus
from io import BytesIO

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from PIL import Image

from getjmanga.downloader import Downloader
from getjmanga.errors import LoginError, NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractors.vcomi import (
    BASE_URL,
    IMAGE_URL,
    TRPC_URL,
    Vcomi,
    episode_title,
    flatten,
    unflatten,
    urlsafe_b64decode,
)

SERIES_URL = f"{BASE_URL}/series/660"
EPISODE_URL = f"{BASE_URL}/episodes/15693"
SECOND_URL = f"{BASE_URL}/episodes/15835"
LOCKED_URL = f"{BASE_URL}/episodes/21629"
LAST_URL = f"{BASE_URL}/episodes/21765"

SECRET = "axLkvm84K3m9Kow5imEQ_XOV90KrvK52Ft4JkaA9m40"
IVS = ["8J_w2tK7RY_OpXmz5ohd7Q", "EY_i66bIOWCgLYCPpncG2Q"]
PATHS = ["episodes/15693/web/7A5R5HFNmSYRDtxEii8YRPog", "episodes/15693/web/EROuZJFwe8nJJe5bGeXbMHwH"]

SERIES = {
    "id": 660,
    "title": "転生したら殺人犯の娘だった",
    "orientation": "vertical",
    "authors": [{"author": {"name": "オカヤマ"}}, {"author": {"name": "沢ちより"}}],
}


def entry(episode_id, sequence, prefix, title):
    return {"id": episode_id, "sequence": sequence, "prefix": prefix, "title": title, "price": 50, "series": SERIES}


FIRST = entry(15693, 1, "1話", "バイト帰りのストーカー")
SECOND = entry(15835, 2, "2話", "恐怖の時間")
LOCKED = entry(21629, 111, "111話", "…やっと言える")
LAST = entry(21765, 112, "112話", "最終話　またね")


def page_node(**data):
    """A SvelteKit `__data.json` answer whose route node carries `data`, devalue-flattened."""
    return {
        "type": "data",
        "nodes": [
            None,
            {"type": "data", "data": [{"floatingBanner": 1}, None], "uses": {"url": 1}},
            {"type": "data", "data": json.loads(flatten(data)), "uses": {"params": ["id"]}},
        ],
    }


NOT_FOUND = {
    "type": "data",
    "nodes": [
        None,
        {"type": "data", "data": [{"floatingBanner": 1}, None], "uses": {"url": 1}},
        {"type": "error", "error": {"message": "Error: 404"}, "status": 404},
    ],
}


def trpc(data):
    """A batched tRPC answer: the result's `data` is devalue text inside the JSON envelope."""
    return [{"result": {"data": flatten(data)}}]


# `episode.getPages` for a readable episode and for a locked one, as the site answers them.
PAGES = {
    "episode": {
        "secret": SECRET,
        "pages": [{"platform": "web", "image": {"path": p, "iv": iv}} for p, iv in zip(PATHS, IVS, strict=True)],
    },
    "readOptions": None,
}
LOCKED_PAGES = {"episode": None, "readOptions": {}}
PAGES_NOT_FOUND = [
    {
        "error": json.dumps(
            [
                {"message": 1, "code": 2, "data": 3},
                "Episode not found",
                -32004,
                {"code": 4, "httpStatus": 5},
                "NOT_FOUND",
                404,
            ]
        )
    }
]


def png_bytes(color=(1, 2, 3), size=(8, 8)):
    raw = BytesIO()
    Image.new("RGB", size, color).save(raw, "PNG")
    return raw.getvalue()


def encrypt(data, secret, iv):
    """AES-CBC with PKCS#7, the way the site stores a page file."""
    padding = 16 - len(data) % 16
    encryptor = Cipher(algorithms.AES(urlsafe_b64decode(secret)), modes.CBC(urlsafe_b64decode(iv))).encryptor()
    return encryptor.update(data + bytes([padding]) * padding) + encryptor.finalize()


@pytest.fixture
def client(fake_session, fake_response):
    """A `Vcomi` over a session answering the page data, `getPages` and two encrypted pages.

    `getPages` is asked for by a query parameter, so the routes here match on
    the URL with its query string appended.
    """

    class QuerySession(fake_session):
        def get(self, url, **kwargs):
            params = kwargs.get("params") or {}
            query = "&".join(f"{key}={value}" for key, value in params.items())
            self.calls.append(url)
            self.params_seen.append(kwargs.get("params"))
            self.headers_seen.append(kwargs.get("headers") or {})
            return self._route(f"{url}?{query}" if query else url)

        def post(self, url, data=None, json=None, **kwargs):
            self.posts.append((url, data if data is not None else json))
            self.headers_seen.append(kwargs.get("headers") or {})
            return self._route(url)

    def build(extra=None):
        routes = {
            f"{EPISODE_URL}/__data.json": fake_response(
                payload=page_node(episode=FIRST, nextEpisode=SECOND, prevEpisode=None)
            ),
            f"{SECOND_URL}/__data.json": fake_response(
                payload=page_node(episode=SECOND, nextEpisode=LOCKED, prevEpisode=FIRST)
            ),
            f"{LOCKED_URL}/__data.json": fake_response(
                payload=page_node(episode=LOCKED, nextEpisode=LAST, prevEpisode=SECOND)
            ),
            f"{LAST_URL}/__data.json": fake_response(
                payload=page_node(episode=LAST, nextEpisode=None, prevEpisode=LOCKED)
            ),
            f"{SERIES_URL}/__data.json": fake_response(
                payload=page_node(series={**SERIES, "episodes": [FIRST, SECOND, LOCKED, LAST]}, banners=[])
            ),
            f'{TRPC_URL}/episode.getPages?batch=1&input={{"0": "[15693]"}}': fake_response(payload=trpc(PAGES)),
            f'{TRPC_URL}/episode.getPages?batch=1&input={{"0": "[15835]"}}': fake_response(payload=trpc(PAGES)),
            f'{TRPC_URL}/episode.getPages?batch=1&input={{"0": "[21629]"}}': fake_response(payload=trpc(LOCKED_PAGES)),
            f'{TRPC_URL}/episode.getPages?batch=1&input={{"0": "[21765]"}}': fake_response(payload=trpc(LOCKED_PAGES)),
            f"{IMAGE_URL}/{PATHS[0]}": fake_response(encrypt(png_bytes(), SECRET, IVS[0]), content_type="image/jpeg"),
            f"{IMAGE_URL}/{PATHS[1]}": fake_response(
                encrypt(png_bytes((250, 0, 0)), SECRET, IVS[1]), content_type="image/jpeg"
            ),
        }
        # A test's own routes go first (first match wins) and replace the defaults they name.
        overrides = extra or {}
        session = QuerySession({**overrides, **{key: value for key, value in routes.items() if key not in overrides}})
        return Vcomi(session), session

    return build


# --- URLs ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        EPISODE_URL,
        f"{EPISODE_URL}/",
        f"{EPISODE_URL}?from=series",
        SERIES_URL,
        f"{SERIES_URL}/",
        "https://vcomi.jp/episodes/1",
    ],
)
def test_suitable_accepts_episode_and_series_urls(url):
    assert Vcomi.suitable(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://vcomi.jp/episodes/15693",
        "https://vcomi.jp/",
        "https://vcomi.jp/episodes/",
        "https://vcomi.jp/episodes/abc",
        "https://vcomi.jp/series/660/episodes",
        "https://vcomi.jp/ranking",
        "https://vcomi.jp/login",
        "https://images.vcomi.jp/episodes/15693/web/7A5R5HFNmSYRDtxEii8YRPog",
        "https://www.vcomi.jp/episodes/15693",
        "https://piccoma.com/web/viewer/8195/1185884",
    ],
)
def test_suitable_rejects_other_urls(url):
    assert not Vcomi.suitable(url)


@pytest.mark.parametrize(
    ("url", "expected"),
    [(EPISODE_URL, False), (SERIES_URL, True), (f"{SERIES_URL}/", True)],
)
def test_is_series(url, expected):
    assert Vcomi.is_series(url) is expected


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ({"prefix": "1話", "title": "バイト帰りのストーカー"}, "1話 バイト帰りのストーカー"),
        ({"prefix": "第1話", "title": ""}, "第1話"),
        ({"prefix": None, "title": "プロローグ"}, "プロローグ"),
        ({"id": 7}, "7"),
    ],
)
def test_episode_title(data, expected):
    assert episode_title(data) == expected


# --- devalue ---------------------------------------------------------------------------


def test_unflatten_reads_what_the_site_answers():
    # `episode.getPages` for a readable episode, exactly as served (cut to two pages).
    flat = json.loads(
        '[{"episode":1,"readOptions":-1},{"secret":2,"pages":3},"axLkvm84K3m9Kow5imEQ_XOV90KrvK52Ft4JkaA9m40",[4,9],'
        '{"platform":5,"image":6},"web",{"path":7,"iv":8},"episodes/15693/web/7A5R5HFNmSYRDtxEii8YRPog",'
        '"8J_w2tK7RY_OpXmz5ohd7Q",{"platform":5,"image":10},{"path":11,"iv":12},'
        '"episodes/15693/web/EROuZJFwe8nJJe5bGeXbMHwH","EY_i66bIOWCgLYCPpncG2Q"]'
    )
    assert unflatten(flat) == PAGES


def test_unflatten_reads_a_locked_answer():
    assert unflatten(json.loads('[{"episode":-1,"readOptions":1},{}]')) == LOCKED_PAGES


def test_unflatten_handles_the_special_indices():
    value = unflatten([[-1, -2, -3, -4, -5, -6, 1], "x"])
    assert value[0] is None
    assert math.isnan(value[1])
    assert value[2] == math.inf
    assert value[3] == -math.inf
    assert value[4] == 0
    assert math.copysign(1, value[4]) == -1
    assert value[5] is None
    assert value[6] == "x"


def test_unflatten_shares_a_repeated_entry():
    value = unflatten([{"a": 1, "b": 1}, {"n": 2}, 1])
    assert value == {"a": {"n": 1}, "b": {"n": 1}}
    assert value["a"] is value["b"]


@pytest.mark.parametrize(("flat", "expected"), [(-1, None), ([], None), ("text", None), (None, None), (True, None)])
def test_unflatten_of_nothing_is_none(flat, expected):
    assert unflatten(flat) is expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (15693, "[15693]"),
        ("x", '["x"]'),
        (None, "-1"),
        (True, "[true]"),
        ({"email": "a@b", "password": "p"}, '[{"email":1,"password":2},"a@b","p"]'),
        ([1, "x", True], '[[1,2,3],1,"x",true]'),
        ({"n": None}, '[{"n":-1}]'),
    ],
)
def test_flatten_writes_like_devalue(value, expected):
    assert flatten(value) == expected


# --- episodes --------------------------------------------------------------------------


def test_episode_reads_the_titles_the_pages_and_the_next_episode(client):
    vcomi, session = client()
    episode = vcomi.episode(EPISODE_URL)

    assert episode.url == EPISODE_URL
    assert episode.series_title == "転生したら殺人犯の娘だった"
    assert (episode.writer, episode.publisher) == ("オカヤマ, 沢ちより", "Vスクロールコミックス")
    assert episode.episode_title == "1話 バイト帰りのストーカー"
    assert [page.url for page in episode.pages] == [f"{IMAGE_URL}/{path}" for path in PATHS]
    assert episode.pages[0].extra == {"iv": IVS[0], "secret": SECRET}
    assert episode.pages[1].extra == {"iv": IVS[1], "secret": SECRET}
    assert episode.next_url == SECOND_URL
    assert episode.number == 1
    assert episode.metadata["episode"] == FIRST
    assert episode.metadata["nextEpisode"] == SECOND
    assert episode.metadata["viewer"] == PAGES
    json.dumps(episode.metadata)

    # The page data first, then `getPages` through the tRPC client with its platform header.
    assert session.calls == [f"{EPISODE_URL}/__data.json", f"{TRPC_URL}/episode.getPages"]
    assert session.params_seen[1] == {"batch": "1", "input": '{"0": "[15693]"}'}
    assert session.headers_seen[1]["x-platform"] == "web"
    assert all(headers["Referer"] == EPISODE_URL for headers in session.headers_seen)


def test_episode_is_dated_by_its_first_page_upload(client, fake_response, uploaded):
    vcomi, _ = client({f"{IMAGE_URL}/{PATHS[0]}": fake_response(b"", headers=uploaded)})
    assert vcomi.episode(EPISODE_URL).published == date(2025, 8, 21)


@pytest.mark.parametrize("url", [f"{EPISODE_URL}/", f"{EPISODE_URL}?from=series", f"{EPISODE_URL}/?x=1"])
def test_episode_accepts_a_trailing_slash_and_a_query(client, url):
    vcomi, session = client()
    episode = vcomi.episode(url)
    assert episode.url == EPISODE_URL
    assert len(episode.pages) == 2
    assert session.calls[0] == f"{EPISODE_URL}/__data.json"


def test_locked_episode_has_no_pages_but_keeps_its_titles_and_the_next_episode(client):
    vcomi, _ = client()
    episode = vcomi.episode(LOCKED_URL)

    assert episode.pages == ()
    assert not episode.readable
    assert episode.series_title == "転生したら殺人犯の娘だった"
    assert episode.episode_title == "111話 …やっと言える"
    assert episode.next_url == LAST_URL
    assert episode.metadata["viewer"] == LOCKED_PAGES


def test_last_episode_names_nothing_next(client):
    vcomi, _ = client()
    episode = vcomi.episode(LAST_URL)
    assert (episode.prev_url, episode.next_url) == (LOCKED_URL, None)
    assert episode.metadata["prevEpisode"] == LOCKED


def test_unknown_episode_is_not_an_episode_page(client, fake_response):
    # SvelteKit answers an unknown id with an error node, HTTP 200.
    vcomi, _ = client({f"{BASE_URL}/episodes/99999999/__data.json": fake_response(payload=NOT_FOUND)})
    with pytest.raises(NotAnEpisodePageError, match=r"nothing at .*99999999: Error: 404"):
        vcomi.episode(f"{BASE_URL}/episodes/99999999")


def test_http_404_is_not_an_episode_page(client, fake_response):
    vcomi, _ = client(
        {f"{BASE_URL}/episodes/99999999/__data.json": fake_response(text="<html>", status_code=HTTPStatus.NOT_FOUND)}
    )
    with pytest.raises(NotAnEpisodePageError, match="nothing at"):
        vcomi.episode(f"{BASE_URL}/episodes/99999999")


def test_page_data_without_an_episode_is_not_an_episode_page(client, fake_response):
    vcomi, _ = client({f"{EPISODE_URL}/__data.json": fake_response(payload=page_node(banners=[]))})
    with pytest.raises(NotAnEpisodePageError, match="no episode in the data"):
        vcomi.episode(EPISODE_URL)


def test_get_pages_saying_not_found_is_not_an_episode_page(client, fake_response):
    vcomi, _ = client(
        {
            f'{TRPC_URL}/episode.getPages?batch=1&input={{"0": "[15693]"}}': fake_response(
                payload=PAGES_NOT_FOUND, status_code=HTTPStatus.NOT_FOUND
            )
        }
    )
    with pytest.raises(NotAnEpisodePageError, match="Episode not found"):
        vcomi.episode(EPISODE_URL)


def test_episode_rejects_a_series_url(client):
    vcomi, _ = client()
    with pytest.raises(UnsupportedUrlError):
        vcomi.episode(SERIES_URL)


# --- series ----------------------------------------------------------------------------


@pytest.mark.parametrize("url", [SERIES_URL, f"{SERIES_URL}/"])
def test_series_urls_lists_the_episodes_in_order(client, url):
    vcomi, session = client()
    assert vcomi.series_urls(url) == [EPISODE_URL, SECOND_URL, LOCKED_URL, LAST_URL]
    assert all(Vcomi.suitable(candidate) for candidate in vcomi.series_urls(url))
    assert session.calls[0] == f"{SERIES_URL}/__data.json"


def test_series_urls_drops_a_repeated_entry(client, fake_response):
    twice = page_node(series={**SERIES, "episodes": [FIRST, FIRST, SECOND, {"title": "no id"}]})
    vcomi, _ = client({f"{SERIES_URL}/__data.json": fake_response(payload=twice)})
    assert vcomi.series_urls(SERIES_URL) == [EPISODE_URL, SECOND_URL]


def test_series_urls_rejects_an_episode_url(client):
    vcomi, _ = client()
    with pytest.raises(UnsupportedUrlError):
        vcomi.series_urls(EPISODE_URL)


def test_series_urls_raises_on_an_empty_listing(client, fake_response):
    empty = page_node(series={**SERIES, "episodes": []})
    vcomi, _ = client({f"{SERIES_URL}/__data.json": fake_response(payload=empty)})
    with pytest.raises(NotAnEpisodePageError, match="lists no episode"):
        vcomi.series_urls(SERIES_URL)


def test_series_urls_raises_on_an_unknown_series(client, fake_response):
    vcomi, _ = client({f"{BASE_URL}/series/99999999/__data.json": fake_response(payload=NOT_FOUND)})
    with pytest.raises(NotAnEpisodePageError, match="nothing at"):
        vcomi.series_urls(f"{BASE_URL}/series/99999999")


# --- images ----------------------------------------------------------------------------


def test_image_fetches_the_page_file_and_decrypts_it(client):
    vcomi, session = client()
    episode = vcomi.episode(EPISODE_URL)
    first = vcomi.image(episode.pages[0], episode)
    second = vcomi.image(episode.pages[1], episode)

    assert first.size == (8, 8)
    assert first.convert("RGB").getpixel((0, 0)) == (1, 2, 3)
    assert second.convert("RGB").getpixel((0, 0))[0] > 200
    assert session.calls[-2:] == [episode.pages[0].url, episode.pages[1].url]
    assert session.headers_seen[-1]["Referer"] == EPISODE_URL


def test_image_without_an_iv_is_kept_as_served(client, fake_response):
    plain = {
        "episode": {"secret": "", "pages": [{"platform": "web", "image": {"path": "episodes/1/web/plain"}}]},
        "readOptions": None,
    }
    vcomi, _ = client(
        {
            f'{TRPC_URL}/episode.getPages?batch=1&input={{"0": "[15693]"}}': fake_response(payload=trpc(plain)),
            f"{IMAGE_URL}/episodes/1/web/plain": fake_response(png_bytes((7, 8, 9)), content_type="image/jpeg"),
        }
    )
    episode = vcomi.episode(EPISODE_URL)
    assert episode.pages[0].extra == {"iv": "", "secret": ""}
    assert vcomi.image(episode.pages[0], episode).convert("RGB").getpixel((0, 0)) == (7, 8, 9)


# --- login -----------------------------------------------------------------------------


def test_login_posts_the_credentials_once_as_a_trpc_mutation(client, fake_response):
    vcomi, session = client(
        {f"{TRPC_URL}/user.login": fake_response(payload=trpc({"id": 1, "email": "someone@example.com"}))}
    )
    vcomi.login(EPISODE_URL, "someone@example.com", "hunter2")

    assert session.posts == [
        (f"{TRPC_URL}/user.login", {"0": '[{"email":1,"password":2},"someone@example.com","hunter2"]'})
    ]
    headers = session.headers_seen[-1]
    assert headers["x-platform"] == "web"
    assert headers["Origin"] == BASE_URL
    assert headers["Content-Type"] == "application/json"


def test_login_raises_with_the_site_reason(client, fake_response):
    vcomi, _ = client({f"{TRPC_URL}/user.login": fake_response(payload=trpc({"error": "INVALID"}))})
    with pytest.raises(LoginError, match="refused the credentials for 'who': INVALID"):
        vcomi.login(EPISODE_URL, "who", "wrong")


def test_login_reports_a_locked_account(client, fake_response):
    vcomi, _ = client({f"{TRPC_URL}/user.login": fake_response(payload=trpc({"error": "LOCKED", "duration": 120}))})
    with pytest.raises(LoginError, match="LOCKED for 120 more seconds"):
        vcomi.login(EPISODE_URL, "who", "pw")


def test_login_raises_when_the_site_forbids_the_request(client, fake_response):
    # Without the Origin header SvelteKit's CSRF check answers 403; the site would too for a blocked client.
    vcomi, _ = client(
        {f"{TRPC_URL}/user.login": fake_response(payload={"message": "Forbidden"}, status_code=HTTPStatus.FORBIDDEN)}
    )
    with pytest.raises(LoginError, match="HTTP 403"):
        vcomi.login(EPISODE_URL, "who", "pw")


def test_login_raises_on_an_unexpected_answer(client, fake_response):
    vcomi, _ = client({f"{TRPC_URL}/user.login": fake_response(text="<html>")})
    with pytest.raises(LoginError, match="something unexpected"):
        vcomi.login(EPISODE_URL, "who", "pw")


# --- the real site ---------------------------------------------------------------------

# One free episode per known host: the first episode of a completed, long series.
TEST_URLS: dict[str, str] = {
    "vcomi.jp": "https://vcomi.jp/episodes/15693",
}


@pytest.mark.network
@pytest.mark.parametrize("host", TEST_URLS)
def test_site_download(tmp_path, host):
    result = Downloader(Vcomi(), tmp_path, only_first=True).download(TEST_URLS[host])
    assert result.status == "saved"
    assert (result.save_dir / "0.jpg").stat().st_size > 0


@pytest.mark.network
def test_site_locked_episode_has_no_pages():
    # The last episode of the series is paid; the site lists it but serves no pages.
    episode = Vcomi().episode(LAST_URL)
    assert not episode.readable
    assert episode.episode_title.startswith("112話")


@pytest.mark.network
def test_site_series_lists_episodes():
    urls = Vcomi().series_urls(SERIES_URL)
    assert urls[0] == EPISODE_URL
    assert len(urls) > 100
    assert all(Vcomi.suitable(url) for url in urls)
