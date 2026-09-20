from __future__ import annotations

import json
from http import HTTPStatus

import pytest
from httpx import HTTPStatusError
from PIL import Image

from getjmanga.downloader import Downloader
from getjmanga.errors import GetjmangaError, LoginError, NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractors.comici import COLUMNS, ROWS, TILES, Comici, descramble, parse_scramble

SCRAMBLE = [1, 5, 13, 8, 4, 14, 3, 2, 10, 0, 11, 12, 7, 6, 9, 15]

EPISODE_HTML = """
<html><head>
<title>IRUKA・prologue | MANGABU!</title>
<meta property="og:title" content="IRUKA・prologue | MANGABU!(マンガ部!)"/>
</head><body>
<div id="comici-viewer" data-comici-viewer-id="abc123" data-api-domain="/api"
     data-share-text="IRUKA" data-prev-episode-id="abc000" data-next-episode-id="def456"></div>
</body></html>
"""

EPISODE_URL = "https://mangabu.jp/episodes/71f48a2c352ed"


def tiled_image(order):
    """A 4x4 grid of tiles, tile k painted with a colour derived from order[k]."""
    image = Image.new("RGB", (COLUMNS * 8, ROWS * 8))
    for position, tile in enumerate(order):
        col, row = divmod(position, ROWS)
        colour = (tile * 16, 255 - tile * 16, (tile * 37) % 256)
        image.paste(Image.new("RGB", (8, 8), colour), (col * 8, row * 8))
    return image


def page(**overrides):
    return {"imageUrl": "u1", "scramble": "[]", "sort": 0, "width": 8, "height": 8, "expiresOn": 0, **overrides}


# --- descrambling -------------------------------------------------------------------------


def test_parse_scramble_reads_the_api_format():
    assert parse_scramble("[1, 5, 13, 8, 4, 14, 3, 2, 10, 0, 11, 12, 7, 6, 9, 15]") == SCRAMBLE


@pytest.mark.parametrize("bad", ["[0, 1, 2]", "[0, 0, " + "1, " * 13 + "1]"])
def test_parse_scramble_rejects_non_permutations(bad):
    with pytest.raises(GetjmangaError):
        parse_scramble(bad)


def test_descramble_restores_the_original():
    original = tiled_image(range(TILES))
    # The viewer copies source tile SCRAMBLE[f] into slot f, so the scrambled
    # image must hold original tile f at slot SCRAMBLE[f].
    inverse = [0] * TILES
    for slot, source in enumerate(SCRAMBLE):
        inverse[source] = slot
    scrambled = tiled_image(inverse)

    assert scrambled.tobytes() != original.tobytes()
    assert descramble(scrambled, SCRAMBLE).tobytes() == original.tobytes()


def test_descramble_leaves_the_uneven_edge_alone():
    # 34 = 4 * 8 + 2, so a two pixel strip falls outside the shuffled grid.
    image = Image.new("RGB", (34, 34), (10, 20, 30))
    image.putpixel((33, 33), (200, 100, 50))
    assert descramble(image, list(range(TILES))).getpixel((33, 33)) == (200, 100, 50)


# --- urls -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://mangabu.jp/episodes/71f48a2c352ed", True),
        ("https://younganimal.com/episodes/006a5e6131753", True),
        ("http://mangabu.jp/episodes/71f48a2c352ed", False),
        ("https://example.com/episodes/1", False),
    ],
)
def test_suitable(url, expected):
    assert Comici.suitable(url) is expected


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://takecomic.jp/series/b167ea507d35f/rss", True),
        ("https://takecomic.jp/series/b167ea507d35f/rss/", True),
        ("https://takecomic.jp/series/b167ea507d35f", True),
        ("https://takecomic.jp/series/b167ea507d35f/new", True),
        ("https://takecomic.jp/series/b167ea507d35f/2", True),
        ("https://rimacomiplus.jp/digitalmargaret/series/df4f799786dfd", True),
        ("https://takecomic.jp/series/b167ea507d35f/2/extra", False),
        ("https://takecomic.jp/episodes/6b35483f82ab3", False),
    ],
)
def test_is_series(url, expected):
    assert Comici.is_series(url) is expected


# --- reading an episode -----------------------------------------------------------------------


def test_viewer_reads_the_viewer_element(fake_session, fake_response):
    session = fake_session({"/episodes/": fake_response(text=EPISODE_HTML)})
    viewer = Comici(session).viewer(EPISODE_URL)

    assert viewer.viewer_id == "abc123"
    assert viewer.api_base == "https://mangabu.jp/api"
    assert viewer.series_title == "IRUKA"
    assert viewer.episode_title == "prologue"
    assert viewer.next_url == "https://mangabu.jp/episodes/def456"


def test_viewer_without_a_next_episode(fake_session, fake_response):
    html = EPISODE_HTML.replace('data-next-episode-id="def456"', 'data-next-episode-id=""')
    session = fake_session({"/episodes/": fake_response(text=html)})
    assert Comici(session).viewer("https://mangabu.jp/episodes/x").next_url is None


def test_viewer_rejects_a_page_without_a_viewer(fake_session, fake_response):
    session = fake_session(
        {
            "/api/episodes/": fake_response(payload={}),
            "/episodes/": fake_response(text="<html><body>nope</body></html>"),
        },
    )
    with pytest.raises(NotAnEpisodePageError):
        Comici(session).viewer("https://example.com/episodes/1")


def test_episode_carries_the_pages_with_their_scramble(fake_session, fake_response):
    pages = [page(imageUrl="u2", scramble=json.dumps(SCRAMBLE), sort=1), page(imageUrl="u1", sort=0)]
    session = fake_session(
        {
            "/episodes/": fake_response(text=EPISODE_HTML),
            "contentsInfo": fake_response(payload={"totalPages": 2, "result": pages}),
        },
    )
    episode = Comici(session).episode(EPISODE_URL)

    assert episode.series_title == "IRUKA"
    assert episode.episode_title == "prologue"
    assert [item.url for item in episode.pages] == ["u1", "u2"]
    assert parse_scramble(episode.pages[1].extra["scramble"]) == SCRAMBLE
    assert (episode.prev_url, episode.next_url) == (
        "https://mangabu.jp/episodes/abc000",
        "https://mangabu.jp/episodes/def456",
    )
    assert episode.metadata["viewer_id"] == "abc123"
    assert [item["sort"] for item in episode.metadata["pages"]] == [0, 1]


def test_pages_asks_for_the_total_before_the_range(fake_session, fake_response):
    pages = [page(imageUrl="u2", sort=1), page(imageUrl="u1", sort=0)]
    session = fake_session(
        {
            "/episodes/": fake_response(text=EPISODE_HTML),
            "contentsInfo": fake_response(payload={"totalPages": 2, "result": pages}),
        },
    )
    comici = Comici(session)
    result = comici.pages(comici.viewer(EPISODE_URL))

    assert [item["sort"] for item in result] == [0, 1]
    ranges = [(params["page-from"], params["page-to"]) for params in session.params_seen[1:]]
    assert ranges == [(0, 0), (0, 1)]


def test_an_episode_with_nothing_readable_has_no_pages(fake_session, fake_response):
    session = fake_session(
        {
            "/episodes/": fake_response(text=EPISODE_HTML),
            "contentsInfo": fake_response(payload={"totalPages": 0, "result": []}),
        },
    )
    assert Comici(session).episode(EPISODE_URL).pages == ()


def test_contents_info_sends_the_site_referer(fake_session, fake_response):
    # studio.booklista.co.jp answers 403 without one.
    session = fake_session(
        {
            "/episodes/": fake_response(text=EPISODE_HTML),
            "contentsInfo": fake_response(payload={"totalPages": 0, "result": []}),
        },
    )
    Comici(session).episode(EPISODE_URL)
    assert session.headers_seen[-1]["Referer"] == EPISODE_URL


def test_contents_info_raises_on_an_error_body(fake_session, fake_response):
    session = fake_session(
        {
            "/episodes/": fake_response(text=EPISODE_HTML),
            "contentsInfo": fake_response(payload={"message": "something went wrong"}),
        },
    )
    with pytest.raises(GetjmangaError, match="refused"):
        Comici(session).episode(EPISODE_URL)


def test_pages_sends_the_token_and_the_content_id_the_page_rendered(fake_session, fake_response):
    html = EPISODE_HTML.replace(
        'data-api-domain="/api"', 'data-api-domain="/api" data-member-jwt="page-jwt" data-content-id="37"'
    )
    session = fake_session(
        {
            "/episodes/": fake_response(text=html),
            "contentsInfo": fake_response(payload={"totalPages": 1, "result": []}),
        },
    )
    Comici(session).episode("https://rimacomiplus.jp/digitalmargaret/episodes/9e628415ccfc2")
    assert session.params_seen[-1]["user-id"] == "page-jwt"
    assert session.params_seen[-1]["contentId"] == "37"


# --- episodes rendered client-side ---------------------------------------------------------------

EPISODE_API = {
    "episode": {
        "id": "ad51b31190681",
        "previousEpisodeId": "9b3c8a1a0f2e1",
        "nextEpisodeId": "d05c9cd20ca35",
        "series": {"name": "IRUKA"},
        "summary": {"title": "1話"},
        "content": [
            {"type": "image", "url": "https://cdn.comici.jp/a.jpg", "width": 800, "height": 2560},
            {"type": "html", "html": ""},
            {"type": "image", "url": "https://cdn.comici.jp/b.jpg", "width": 800, "height": 1760},
        ],
    },
}

VIEWER_BLOCK_API = {
    "episode": {
        "id": "0071b2c5d21f4",
        "contentId": 20,
        "nextEpisodeId": "",
        "series": {"name": "僕の心のヤバイやつ"},
        "summary": {"title": "Karte.16"},
        "content": [{"type": "viewer", "viewerId": "73d687e4494a21d6"}],
    },
}


def hydrated_session(fake_session, fake_response, payload=EPISODE_API, **routes):
    """A site whose episode HTML carries no viewer, as corkagency's does not."""
    return fake_session(
        {
            "/api/episodes/": fake_response(payload=payload),
            "/episodes/": fake_response(text="<html><body>hydrated later</body></html>"),
            **routes,
        },
    )


def test_episode_falls_back_to_the_episode_api(fake_session, fake_response):
    session = hydrated_session(fake_session, fake_response)
    episode = Comici(session).episode("https://ebookstore.corkagency.com/episodes/ad51b31190681")

    assert episode.series_title == "IRUKA"
    assert episode.episode_title == "1話"
    assert (episode.prev_url, episode.next_url) == (
        "https://ebookstore.corkagency.com/episodes/9b3c8a1a0f2e1",
        "https://ebookstore.corkagency.com/episodes/d05c9cd20ca35",
    )
    assert episode.metadata["api_base"] == "https://ebookstore.corkagency.com/api"


def test_inline_pages_skip_non_image_blocks_and_never_scramble(fake_session, fake_response):
    session = hydrated_session(fake_session, fake_response)
    episode = Comici(session).episode("https://ebookstore.corkagency.com/episodes/ad51b31190681")

    assert [item.url for item in episode.pages] == ["https://cdn.comici.jp/a.jpg", "https://cdn.comici.jp/b.jpg"]
    # The identity permutation leaves every tile where it already was.
    assert parse_scramble(episode.pages[0].extra["scramble"]) == list(range(TILES))
    # No contentsInfo round trip is needed: the episode JSON already had them.
    assert not [url for url in session.calls if "contentsInfo" in url]


def test_a_viewer_block_is_looked_up_with_contents_info(fake_session, fake_response):
    """championcross.jp hands a viewer id over in the episode JSON, not the images."""
    pages = [page()]
    session = hydrated_session(
        fake_session,
        fake_response,
        VIEWER_BLOCK_API,
        contentsInfo=fake_response(payload={"totalPages": 1, "result": pages}),
    )
    comici = Comici(session)
    viewer = comici.viewer("https://championcross.jp/episodes/0071b2c5d21f4")

    assert viewer.viewer_id == "73d687e4494a21d6"
    assert viewer.content_id == "20"
    assert viewer.inline_pages is None
    assert comici.pages(viewer) == pages
    sent = [params for url, params in zip(session.calls, session.params_seen, strict=True) if "contentsInfo" in url]
    assert sent
    assert all(params["comici-viewer-id"] == "73d687e4494a21d6" for params in sent)
    assert all(params["contentId"] == "20" for params in sent)


def test_an_episode_without_content_is_not_readable(fake_session, fake_response):
    """The same site answers a locked episode with an empty `content`."""
    locked = {"episode": {**VIEWER_BLOCK_API["episode"], "content": []}}
    session = hydrated_session(fake_session, fake_response, locked)
    assert Comici(session).episode("https://championcross.jp/episodes/0071b2c5d21f4").pages == ()


# --- listing a series -----------------------------------------------------------------------------

SERIES_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<title><![CDATA[IRUKA]]></title>
<link>https://takecomic.jp/series/b167ea507d35f/new</link>
<item><title><![CDATA[2話]]></title>
<link>https://takecomic.jp/episodes/newest/?utm_source=rss&amp;utm_medium=referral</link></item>
<item><title><![CDATA[1話]]></title>
<link>https://takecomic.jp/episodes/older/</link></item>
</channel></rss>
"""

SERIES_PAGE = """
<html><body><ul>
<li><a href="/episodes/{first}"><img src="/thumb.jpg"></a><a href="/episodes/{first}">1</a></li>
<li><a href="/episodes/{second}">2</a></li>
<li><a href="/series/b167ea507d35f/new">new</a></li>
</ul></body></html>
"""


def listing_session(fake_session, fake_response, pages=2, prefix=""):
    """A site whose series list holds `pages` pages of two episodes each."""
    routes = {
        f"{prefix}/series/b167ea507d35f/{number}": fake_response(
            text=SERIES_PAGE.format(first=f"p{number}a", second=f"p{number}b").replace(
                "/episodes/", f"{prefix}/episodes/"
            ),
        )
        for number in range(1, pages + 1)
    }
    routes["/series/"] = fake_response(status_code=HTTPStatus.NOT_FOUND)
    return fake_session(routes)


@pytest.mark.parametrize(
    "url",
    [
        "https://takecomic.jp/series/b167ea507d35f",
        "https://takecomic.jp/series/b167ea507d35f/new",
        "https://takecomic.jp/series/b167ea507d35f/2",
    ],
)
def test_series_urls_walks_the_listing_from_page_one(fake_session, fake_response, url):
    session = listing_session(fake_session, fake_response)
    urls = Comici(session).series_urls(url)

    # Whatever the URL pointed at, the walk starts at page 1 and ends on the 404.
    assert session.calls == [
        "https://takecomic.jp/series/b167ea507d35f/1",
        "https://takecomic.jp/series/b167ea507d35f/2",
        "https://takecomic.jp/series/b167ea507d35f/3",
    ]
    assert urls == [
        "https://takecomic.jp/episodes/p1a",
        "https://takecomic.jp/episodes/p1b",
        "https://takecomic.jp/episodes/p2a",
        "https://takecomic.jp/episodes/p2b",
    ]


def test_series_urls_keeps_an_imprint_prefix(fake_session, fake_response):
    session = listing_session(fake_session, fake_response, pages=1, prefix="/digitalmargaret")
    urls = Comici(session).series_urls("https://rimacomiplus.jp/digitalmargaret/series/b167ea507d35f")

    assert session.calls[0] == "https://rimacomiplus.jp/digitalmargaret/series/b167ea507d35f/1"
    assert urls == [
        "https://rimacomiplus.jp/digitalmargaret/episodes/p1a",
        "https://rimacomiplus.jp/digitalmargaret/episodes/p1b",
    ]


def test_series_urls_stops_when_a_page_repeats_itself(fake_session, fake_response):
    # A site that clamps an out-of-range page number to the last page instead of
    # answering 404 would otherwise be walked forever.
    same = fake_response(text=SERIES_PAGE.format(first="only-a", second="only-b"))
    session = fake_session({"/series/": same})
    urls = Comici(session).series_urls("https://takecomic.jp/series/b167ea507d35f")

    assert len(session.calls) == 2
    assert urls == ["https://takecomic.jp/episodes/only-a", "https://takecomic.jp/episodes/only-b"]


@pytest.mark.parametrize(
    ("url", "html"),
    [
        ("https://takecomic.jp/series/b167ea507d35f/new", "<html><body>nope</body></html>"),
        (
            "https://takecomic.jp/series/b167ea507d35f/rss",
            SERIES_RSS[: SERIES_RSS.index("<item>")] + "</channel></rss>",
        ),
    ],
)
def test_series_urls_rejects_a_listing_without_episodes(fake_session, fake_response, url, html):
    session = fake_session({"/series/": fake_response(text=html)})
    with pytest.raises(NotAnEpisodePageError, match="lists no episode"):
        Comici(session).series_urls(url)


def test_series_urls_rejects_an_episode_url(fake_session):
    with pytest.raises(UnsupportedUrlError, match="not a series page"):
        Comici(fake_session({})).series_urls("https://takecomic.jp/episodes/1")


def test_series_urls_lists_every_episode_in_feed_order(fake_session, fake_response):
    session = fake_session({"/series/": fake_response(text=SERIES_RSS)})
    urls = Comici(session).series_urls("https://takecomic.jp/series/b167ea507d35f/rss")

    # Feed order is newest first, and the item links keep no utm query.
    assert urls == ["https://takecomic.jp/episodes/newest/", "https://takecomic.jp/episodes/older/"]


# --- downloading ------------------------------------------------------------------------------------


def test_download_writes_descrambled_pages_and_metadata(fake_session, fake_response, tmp_path):
    inverse = [0] * TILES
    for slot, source in enumerate(SCRAMBLE):
        inverse[source] = slot
    buffer = tmp_path / "src.png"
    tiled_image(inverse).save(buffer)

    scrambled = page(imageUrl="https://viewer.mangabu.jp/book/abc123/master-01.jpg", scramble=json.dumps(SCRAMBLE))
    session = fake_session(
        {
            "/episodes/": fake_response(text=EPISODE_HTML),
            "contentsInfo": fake_response(payload={"totalPages": 1, "result": [scrambled]}),
            "viewer.mangabu.jp": fake_response(buffer.read_bytes()),
        },
    )

    result = Downloader(Comici(session), tmp_path, save_metadata=True).download(EPISODE_URL)

    assert result.status == "saved"
    assert result.episode.next_url == "https://mangabu.jp/episodes/def456"
    assert result.save_dir == tmp_path / "mangabu.jp" / "IRUKA" / "prologue"
    assert (result.save_dir / "0.jpg").exists()
    assert session.headers_seen[-1]["Referer"] == EPISODE_URL
    metadata = json.loads((result.save_dir / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["metadata"]["pages"][0]["sort"] == 0


# --- logging in --------------------------------------------------------------------------------------

AUTH_ROUTES = {
    "/api/auth/csrf": {"csrfToken": "tok"},
    "/api/auth/callback/credentials": {"url": "https://mangabu.jp/"},
    "/api/auth/session": {"idToken": "id-token-value", "user": 1},
}


def auth_session(fake_session, fake_response, **overrides):
    routes = {needle: fake_response(payload=payload) for needle, payload in AUTH_ROUTES.items()}
    return fake_session({**routes, **overrides})


def test_login_posts_the_credentials_with_the_csrf_token(fake_session, fake_response):
    session = auth_session(fake_session, fake_response)
    Comici(session).login("https://mangabu.jp/episodes/1", "comici-id", "pw")

    url, data = session.posts[0]
    assert url == "https://mangabu.jp/api/auth/callback/credentials"
    assert data["id"] == "comici-id"
    assert data["password"] == "pw"
    assert data["csrfToken"] == "tok"


def test_login_raises_when_the_callback_says_unauthorized(fake_session, fake_response):
    session = auth_session(
        fake_session,
        fake_response,
        **{"/api/auth/callback/credentials": fake_response(status_code=HTTPStatus.UNAUTHORIZED)},
    )
    with pytest.raises(LoginError, match="refused the credentials"):
        Comici(session).login("https://mangabu.jp/episodes/1", "who", "wrong")


def test_login_raises_when_no_token_comes_back(fake_session, fake_response):
    session = auth_session(fake_session, fake_response, **{"/api/auth/session": fake_response(payload={})})
    with pytest.raises(LoginError, match="refused the credentials"):
        Comici(session).login("https://mangabu.jp/episodes/1", "who", "pw")


def test_a_signed_in_site_sends_the_token(fake_session, fake_response):
    session = auth_session(
        fake_session,
        fake_response,
        **{
            "/episodes/": fake_response(text=EPISODE_HTML),
            "contentsInfo": fake_response(payload={"totalPages": 1, "result": []}),
        },
    )
    comici = Comici(session)
    comici.login("https://mangabu.jp/episodes/1", "comici-id", "pw")
    comici.episode(EPISODE_URL)

    assert session.headers_seen[-1]["Authorization"] == "id-token-value"


def test_another_site_does_not_get_the_token(fake_session, fake_response):
    session = auth_session(
        fake_session,
        fake_response,
        **{
            "/episodes/": fake_response(text=EPISODE_HTML),
            "contentsInfo": fake_response(payload={"totalPages": 1, "result": []}),
        },
    )
    comici = Comici(session)
    comici.login("https://mangabu.jp/episodes/1", "comici-id", "pw")
    comici.viewer("https://younganimal.com/episodes/2")

    assert "Authorization" not in session.headers_seen[-1]


# --- the real sites ---------------------------------------------------------------------------------

# One episode per known site that is free to read without an account.
TEST_URLS: dict[str, str] = {
    "asacomi.jp": "https://asacomi.jp/episodes/d689876764c05",
    "bibibi-comic.com": "https://bibibi-comic.com/episodes/3fc263ee98e51",
    "bigcomics.jp": "https://bigcomics.jp/episodes/751a9656f6f2e",
    "championcross.jp": "https://championcross.jp/episodes/f79c98b6ede83",
    "comic-growl.com": "https://comic-growl.com/episodes/ae67f63a142b8",
    "comic-room-base.com": "https://comic-room-base.com/episodes/49e48489486b7",
    "comic-ryu.jp": "https://comic-ryu.jp/episodes/15b4bb6e23094",
    "comic.j-nbooks.jp": "https://comic.j-nbooks.jp/episodes/4c9428882f24a",
    "comicpash.jp": "https://comicpash.jp/episodes/3e051ee5500c3",
    "comicride.jp": "https://comicride.jp/episodes/e7137bf1e8b27",
    "comics.comici.jp": "https://comics.comici.jp/comicicomics/episodes/09f3f099e119d",
    "comics.manga-bang.com": "https://comics.manga-bang.com/episodes/3e3efa60aa9b9",
    "comirela.com": "https://comirela.com/episodes/78d565d1005a1",
    "ebookstore.corkagency.com": "https://ebookstore.corkagency.com/episodes/ad51b31190681",
    "g-comi.jp": "https://g-comi.jp/episodes/cb3488365c75c",
    "hanayume.com": "https://hanayume.com/episodes/bc43f35254f09",
    "hayacomic.jp": "https://hayacomic.jp/episodes/86dbdd38cbdba",
    "heros-web.com": "https://heros-web.com/episodes/3a2698c5efefe",
    "kansai.mag-garden.co.jp": "https://kansai.mag-garden.co.jp/episodes/24c0c8b3e0b5b",
    "kimicomi.com": "https://kimicomi.com/episodes/a5c306c4268e1",
    "manga-zegra.com": "https://manga-zegra.com/episodes/ce0950449d914",
    "mangabu.jp": "https://mangabu.jp/episodes/ff7e9e1616543",
    "mangalt.jp": "https://mangalt.jp/episodes/2e416d98ce780",
    "mangaspa.nikkan-spa.jp": "https://mangaspa.nikkan-spa.jp/episodes/5fbbe0d610d7b",
    "namicomic.jp": "https://namicomic.jp/episodes/6372afa2ba503",
    "piacomic.jp": "https://piacomic.jp/episodes/d3d3e7ba955dd",
    "rimacomiplus.jp": "https://rimacomiplus.jp/digitalmargaret/episodes/9e628415ccfc2",
    "studio.booklista.co.jp": "https://studio.booklista.co.jp/episodes/de1ce4d9cc0ae",
    "takecomic.jp": "https://takecomic.jp/episodes/6b35483f82ab3",
    "younganimal.com": "https://younganimal.com/episodes/b790a79dd70a7",
    "youngchampion.jp": "https://youngchampion.jp/episodes/2bd90287798ab",
}


@pytest.mark.network
@pytest.mark.parametrize("host", TEST_URLS)
def test_site_download(tmp_path, host):
    try:
        result = Downloader(Comici(), tmp_path, only_first=True).download(TEST_URLS[host])
    except HTTPStatusError as error:
        response = error.response
        if response is not None and response.status_code == HTTPStatus.FORBIDDEN:
            # A few sites refuse whole networks -- CI runners among them. That is
            # the site's call about where it serves from, not a bug in the client.
            pytest.skip(f"{host} refuses requests from this network ({response.status_code}).")
        raise

    assert result.status == "saved"
    assert (result.save_dir / "0.jpg").exists()
