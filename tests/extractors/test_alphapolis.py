from __future__ import annotations

import base64
import json
import struct
from http import HTTPStatus
from io import BytesIO

import pytest
from PIL import Image

from getjmanga.downloader import Downloader
from getjmanga.errors import LoginError, NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractors.alphapolis import (
    BASE_URL,
    LOGIN_URL,
    AlphaPolis,
    Piece,
    descramble,
    parse_puzzle,
    parse_puzzles,
)

WORK_URL = f"{BASE_URL}/manga/official/166000762"
EPISODE_URL = f"{WORK_URL}/12255"
NEXT_URL = f"{WORK_URL}/12480"
USER_WORK_URL = f"{BASE_URL}/manga/140803433/627083503"
USER_EPISODE_URL = f"{USER_WORK_URL}/episode/11756968"

# The first page's puzzle table of a real official episode, as the viewer's
# `getArray` splits it out of the placeholder, and what its WebAssembly
# `createData` said about the first two pieces (decoded from the CSS values).
REAL_TABLE = (
    "3625ee48be0302f0050065c90c0303f0a12c77c8a00602f0c51d0048220503f0541600486e0101f0e30e7788a30103f0"
    "511677c8b10300f0a02c6549660603f0a12c00c8d60600f00600ee08b40001f033250048ca0301f07007dc09c80304f0"
    "7207eec86a0501f0e00edc89cb0004f00100dc092e0504f070076509d40002f054166589410402f0c51d65891a0102f0"
    "3025dc89cc0204f0c41ddc89bc0404f0a42ceec8450601f0720777c8020003f031256509950401f035257748320100f0"
    "07000008c90000f05416ee481d0403f002007788eb0202f0c31d77c8280200f0e60e6509d80400f0a42cdc899d0604f0"
    "5416dc493e0104f0e30eee88090201f077070008140502f0c41dee08500500f0e10e00c88a0203f0"
)
REAL_SERVED_SIZE = (1090, 1550)


def viewer_html(config: dict) -> str:
    return f"""<html><head><meta name="csrf-token" content="token-1"></head><body>
<div id="app-manga-viewer" class="manga-viewer">
    <script type="application/json">
        {json.dumps(config, ensure_ascii=False)}
    </script>
</div></body></html>"""


OFFICIAL_CONFIG = {
    "manga": {"mangaId": 166000762, "isVerticalManga": False},
    "episode": {"episodeNo": 12255, "mainTitle": "第1回『最下級スキルの力』"},
    "isPhone": False,
    "loginAccount": None,
    "isPreview": False,
    "isOfficialManga": True,
    "isPageImageHidden": False,
    "urls": {"getViewer": "/manga/official/viewer.json"},
}

USER_CONFIG = {
    "manga": {"mangaId": 627083503, "isVerticalManga": 0},
    "episode": {"episodeNo": 11756968, "mainTitle": "1話目"},
    "isOfficialManga": False,
    "isPageImageHidden": False,
    "isPreview": False,
    "urls": {"getViewer": f"{USER_EPISODE_URL}/viewer.json"},
    "viewerInfoData": None,
}

EPISODES = [
    {"episodeNo": 12255, "url": "/manga/official/166000762/12255", "mainTitle": "第1回『最下級スキルの力』"},
    {"episodeNo": 12480, "url": "/manga/official/166000762/12480", "mainTitle": "第2回『決別』"},
]

WORK_HTML = f"""<html><head><title>Ｆ級テイマー | 公式Web漫画 | アルファポリス</title></head><body>
<div class="author-label">
    <div class="authors">
        <a href="https://www.alphapolis.co.jp/author/detail/373494515?type=official_manga&amp;a_id=10747">石田総司</a><!--
        -->/漫画
        <a href="https://www.alphapolis.co.jp/author/detail/710199495?type=official_manga&amp;a_id=11339">ゆーき</a><!--
        -->/原作
    </div>
</div>
<div id="app-official-manga-toc">
    <script type="application/json">
        {
    json.dumps(
        {
            "episodeUrl": f"{BASE_URL}/manga/official/episodes.json",
            "mangaId": 166000762,
            "mangaTitle": "Ｆ級テイマーは数の暴力で世界を裏から支配する",
            "episodes": [*EPISODES, EPISODES[1]],
        },
        ensure_ascii=False,
    )
}
    </script>
</div></body></html>"""

USER_WORK_HTML = f"""<html><body>
<div class="p-content-info__author-diary">
    <a href="https://www.alphapolis.co.jp/author/detail/140803433" class="p-content-info__author c-link">
        梅星かぼす
    </a>
</div>
<script type="application/json" id="app-cover-data">
{
    json.dumps(
        {
            "content": {"id": 627083503, "title": "ghost", "url": "/manga/140803433/627083503"},
            "chapterEpisodes": [
                {
                    "chapterId": None,
                    "title": "",
                    "isRental": False,
                    "episodes": [
                        {
                            "episodeNo": 11756968,
                            "url": "/manga/140803433/627083503/episode/11756968",
                            "mainTitle": "1話目",
                        },
                        {
                            "episodeNo": 11788108,
                            "url": "/manga/140803433/627083503/episode/11788108",
                            "mainTitle": "2話目",
                        },
                    ],
                    "extraEpisode": {
                        "episodeNo": 11800000,
                        "url": "/manga/140803433/627083503/episode/11800000",
                        "mainTitle": "番外編",
                    },
                },
            ],
        },
        ensure_ascii=False,
    )
}
</script>
</body></html>"""

LOGIN_HTML = """<html><body>
<form action="https://www.alphapolis.co.jp/login" method="post" id="UserLoginForm">
  <input type="hidden" name="_token" value="form-token">
  <input type="email" name="email"><input type="password" name="password">
</form>
</body></html>"""

REFUSED_HTML = """<html><body>
<script type="application/json" id="app-login-account-data">{"loginAccount":null}</script>
<div class="flash-message message error">
    ログインに失敗しました。入力内容を確認してください。
</div>
</body></html>"""

SIGNED_IN_HTML = """<html><body>
<script type="application/json" id="app-login-account-data">{"loginAccount":{"user":{"name":"someone"}}}</script>
</body></html>"""


# --- a synthetic puzzle -------------------------------------------------------------

TILE = 8
BORDER = 1
CONTENT = TILE - 2 * BORDER
#: A page of two full columns plus a 4px one, and one full row plus a 3px one.
PAGE_SIZE = (2 * CONTENT + 4, CONTENT + 3)
SERVED_SIZE = (PAGE_SIZE[0] + 3 * 2 * BORDER, PAGE_SIZE[1] + 2 * 2 * BORDER)

#: (destination x, destination y, source column, source row, rotation, flipped)
PIECES = [
    (0, 0, 1, 0, 3, True),
    (CONTENT, 0, 0, 0, 1, False),
    (2 * CONTENT, 0, 2, 0, 0, True),
    (0, CONTENT, 1, 1, 2, True),
    (CONTENT, CONTENT, 0, 1, 0, False),
    (2 * CONTENT, CONTENT, 2, 1, 2, False),
]


def pack_piece(piece, *, tile=TILE, border=BORDER):
    """Pack one entry of `PIECES` the way the site's table does."""
    x, y, source_column, source_row, rotation, flipped = piece
    placement = (border << 27) | (x << 15) | (y << 3) | (rotation << 1) | int(flipped)
    source = (tile << 24) | (source_column << 16) | (source_row << 8)
    return struct.pack("<II", placement, source)


TABLE = b"".join(pack_piece(piece) for piece in PIECES)


def placeholder(*tables: bytes) -> str:
    payload = b"\x89PNG" + bytes(29)
    for table in tables:
        payload += struct.pack("<H", len(table) // 8) + table
    return "data:image/png;base64," + base64.b64encode(payload).decode()


def page_image() -> Image.Image:
    """A page whose every pixel is its own colour, so a misplaced one shows."""
    width, height = PAGE_SIZE
    image = Image.new("RGB", PAGE_SIZE)
    image.putdata([(x * 16, y * 24, (x + y) * 8) for y in range(height) for x in range(width)])
    return image


def scramble(page: Image.Image) -> Image.Image:
    """Cut `page` into bordered tiles and lay them out the way the CDN serves them."""
    padded = Image.new("RGB", (page.width + 2 * BORDER, page.height + 2 * BORDER), (255, 0, 255))
    padded.paste(page, (BORDER, BORDER))
    served = Image.new("RGB", SERVED_SIZE, (255, 0, 255))
    for x, y, column, row, rotation, flipped in PIECES:
        width = min(CONTENT, page.width - x) + 2 * BORDER
        height = min(CONTENT, page.height - y) + 2 * BORDER
        tile = padded.crop((x, y, x + width, y + height))
        # The viewer flips, then turns counterclockwise; undo that in reverse.
        if rotation:
            tile = tile.rotate(-90 * rotation, expand=True)
        if flipped:
            tile = tile.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
        served.paste(tile, (column * TILE, row * TILE))
    return served


def png_bytes(image: Image.Image) -> bytes:
    raw = BytesIO()
    image.save(raw, "PNG")
    return raw.getvalue()


@pytest.fixture
def client(fake_session, fake_response):
    def build(routes=None, *, images=None):
        served = png_bytes(scramble(page_image()))
        payload = {
            "manga": {
                "mangaId": 166000762,
                "title": "Ｆ級テイマーは数の暴力で世界を裏から支配する",
                "isOfficialManga": True,
            },
            "episode": {"episodeNo": 12255, "title": "第1回", "mainTitle": "第1回『最下級スキルの力』"},
            "episodes": EPISODES,
            "page": {
                "placeholder": placeholder(TABLE, b""),
                "images": images
                if images is not None
                else [
                    {"url": "https://ot-image.alphapolis.co.jp/p/1.webp?Expires=1", "width": 16, "height": 9},
                    {"url": "https://ot-image.alphapolis.co.jp/p/2.webp?Expires=1", "width": 16, "height": 9},
                ],
                "size": {"width": 16, "height": 9},
            },
            "isLogin": False,
        }
        merged = dict(routes or {})
        for needle, response in (
            ("viewer.json", fake_response(payload=payload)),
            ("ot-image.alphapolis.co.jp/p/1.webp", fake_response(served, content_type="image/webp")),
            ("ot-image.alphapolis.co.jp/p/2.webp", fake_response(png_bytes(page_image()), content_type="image/webp")),
            (EPISODE_URL, fake_response(text=viewer_html(OFFICIAL_CONFIG))),
            (WORK_URL, fake_response(text=WORK_HTML)),
            (USER_EPISODE_URL, fake_response(text=viewer_html(USER_CONFIG))),
            (USER_WORK_URL, fake_response(text=USER_WORK_HTML)),
        ):
            merged.setdefault(needle, response)
        session = fake_session(merged)
        return AlphaPolis(session), session

    return build


# --- URLs -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (EPISODE_URL, True),
        (EPISODE_URL + "/", True),
        (WORK_URL, True),
        (USER_EPISODE_URL, True),
        (USER_WORK_URL, True),
        ("http://www.alphapolis.co.jp/manga/official/166000762/12255", False),
        ("https://alphapolis.co.jp/manga/official/166000762/12255", False),
        ("https://www.alphapolis.co.jp/manga/official", False),
        ("https://www.alphapolis.co.jp/manga/official/ranking?category=total", False),
        ("https://www.alphapolis.co.jp/manga/user/vertical", False),
        ("https://www.alphapolis.co.jp/manga/guide", False),
        ("https://www.alphapolis.co.jp/novel/140803433/627083503", False),
        ("https://www.alphapolis.co.jp/manga/140803433/627083503/comment", False),
        ("https://www.alphapolis.co.jp/", False),
    ],
)
def test_suitable(url, expected):
    assert AlphaPolis.suitable(url) is expected


@pytest.mark.parametrize(
    ("url", "expected"),
    [(WORK_URL, True), (USER_WORK_URL, True), (EPISODE_URL, False), (USER_EPISODE_URL, False)],
)
def test_is_series(url, expected):
    assert AlphaPolis.is_series(url) is expected


# --- the puzzle ---------------------------------------------------------------------------


def test_parse_puzzles_splits_the_placeholder_per_page():
    assert parse_puzzles(placeholder(TABLE, b"", TABLE[:8])) == [TABLE.hex(), "", TABLE[:8].hex()]
    assert parse_puzzles("") == []


def test_parse_puzzle_reads_a_real_table_like_the_viewer_module():
    puzzle = parse_puzzle(bytes.fromhex(REAL_TABLE), *REAL_SERVED_SIZE)

    assert (puzzle.width, puzzle.height, puzzle.border) == (1080, 1536, 1)
    assert len(puzzle.pieces) == 35
    assert puzzle.pieces[0] == Piece(
        x=2 * 238,
        y=5 * 238,
        source_x=480,
        source_y=720,
        source_width=240,
        source_height=240,
        rotation=3,
        flipped=False,
        order=27,
    )
    assert puzzle.pieces[1] == Piece(
        x=3 * 238,
        y=0,
        source_x=720,
        source_y=720,
        source_width=240,
        source_height=240,
        rotation=2,
        flipped=True,
        order=3,
    )
    # The bottom row is cut short, the right column too, and a turned piece
    # has its sides swapped in the served image.
    assert {(piece.source_width, piece.source_height) for piece in puzzle.pieces} == {
        (240, 240),
        (130, 240),
        (240, 110),
        (130, 110),
    }
    assert {piece.rotation for piece in puzzle.pieces if piece.source_width != piece.source_height} <= {0, 2}
    assert sorted(piece.order for piece in puzzle.pieces) == list(range(35))


def test_parse_puzzle_of_an_empty_table_has_no_pieces():
    assert parse_puzzle(b"", 10, 10).pieces == ()


def test_descramble_restores_a_synthetic_page():
    page = page_image()
    served = scramble(page)
    assert served.size == SERVED_SIZE

    restored = descramble(served, TABLE.hex())
    assert restored.size == PAGE_SIZE
    assert restored.tobytes() == page.tobytes()


def test_descramble_leaves_an_unscrambled_page_alone():
    page = page_image()
    assert descramble(page, "") is page


# --- episodes -----------------------------------------------------------------------------


def test_episode_reads_the_titles_and_the_pages(client):
    alphapolis, session = client()
    episode = alphapolis.episode(EPISODE_URL)

    assert episode.series_title == "Ｆ級テイマーは数の暴力で世界を裏から支配する"
    assert episode.episode_title == "第1回『最下級スキルの力』"
    assert (episode.writer, episode.publisher) == ("石田総司 (漫画), ゆーき (原作)", "アルファポリス")
    assert [page.url for page in episode.pages] == [
        "https://ot-image.alphapolis.co.jp/p/1.webp?Expires=1",
        "https://ot-image.alphapolis.co.jp/p/2.webp?Expires=1",
    ]
    assert episode.pages[0].extra == {"puzzle": TABLE.hex()}
    assert episode.pages[1].extra == {"puzzle": ""}
    assert (episode.pages[0].width, episode.pages[0].height) == (16, 9)
    assert (episode.prev_url, episode.next_url) == (None, NEXT_URL)
    assert episode.number == 1
    assert episode.metadata["episode"]["episodeNo"] == 12255
    json.dumps(episode.metadata)

    url, body = session.posts[-1]
    assert url == f"{BASE_URL}/manga/official/viewer.json"
    assert body == {
        "manga_sele_id": 166000762,
        "episode_no": 12255,
        "resolution": "full_hd",
        "hide_page": False,
        "preview": False,
    }


def test_episode_of_a_user_work_posts_to_its_own_viewer_endpoint(client, fake_response):
    payload = {
        "manga": {"mangaId": 627083503, "title": "ghost", "isOfficialManga": False},
        "episode": {"episodeNo": 11756968, "title": "1話目", "mainTitle": "1話目"},
        "episodes": [
            {"episodeNo": 11756968, "url": USER_EPISODE_URL, "mainTitle": "1話目"},
            {"episodeNo": 11788108, "url": f"{USER_WORK_URL}/episode/11788108", "mainTitle": "2話目"},
        ],
        "page": {
            "images": [{"url": "https://ot-image.alphapolis.co.jp/comic/public/1", "width": 1080, "height": 1536}],
            "size": {"width": 1080, "height": 1536},
        },
    }
    alphapolis, session = client({"viewer.json": fake_response(payload=payload)})
    episode = alphapolis.episode(USER_EPISODE_URL)

    assert episode.series_title == "ghost"
    assert episode.episode_title == "1話目"
    assert (episode.writer, episode.publisher) == ("梅星かぼす", "アルファポリス")
    assert [page.extra for page in episode.pages] == [{"puzzle": ""}]
    assert episode.next_url == f"{USER_WORK_URL}/episode/11788108"
    assert session.posts[-1][0] == f"{USER_EPISODE_URL}/viewer.json"
    assert session.posts[-1][1]["manga_sele_id"] == 627083503


def test_episode_is_the_last_of_its_work(client, fake_response):
    alphapolis, _ = client(
        {NEXT_URL: fake_response(text=viewer_html({**OFFICIAL_CONFIG, "episode": {"episodeNo": 12480}}))}
    )
    episode = alphapolis.episode(NEXT_URL)
    assert (episode.prev_url, episode.next_url) == (EPISODE_URL, None)
    assert episode.number == 2


def test_a_forbidden_episode_is_locked_and_still_names_the_next(client, fake_response):
    alphapolis, session = client(
        {NEXT_URL: fake_response(text="<html>rental</html>", status_code=HTTPStatus.FORBIDDEN)}
    )
    episode = alphapolis.episode(NEXT_URL)

    assert episode.pages == ()
    assert episode.series_title == "Ｆ級テイマーは数の暴力で世界を裏から支配する"
    assert episode.episode_title == "第2回『決別』"
    assert episode.next_url is None
    assert session.posts == []
    # The work page was read for the list, once.
    assert session.calls.count(WORK_URL) == 1


def test_an_access_denied_answer_is_locked(client, fake_response):
    alphapolis, _ = client({"viewer.json": fake_response(payload={"isAccessDenied": True})})
    episode = alphapolis.episode(EPISODE_URL)

    assert episode.pages == ()
    assert episode.episode_title == "第1回『最下級スキルの力』"
    assert episode.next_url == NEXT_URL


def test_an_expired_answer_is_locked(client, fake_response):
    alphapolis, _ = client({"viewer.json": fake_response(payload={"isExpired": True})})
    episode = alphapolis.episode(EPISODE_URL)

    assert episode.pages == ()
    assert episode.next_url == NEXT_URL


def test_a_page_without_a_viewer_is_not_an_episode(client, fake_response):
    alphapolis, _ = client({EPISODE_URL: fake_response(text="<html><body>nothing here</body></html>")})
    with pytest.raises(NotAnEpisodePageError, match="no viewer"):
        alphapolis.episode(EPISODE_URL)


def test_a_missing_episode_is_not_an_episode(client, fake_response):
    alphapolis, _ = client({EPISODE_URL: fake_response(text="", status_code=HTTPStatus.NOT_FOUND)})
    with pytest.raises(NotAnEpisodePageError, match="404"):
        alphapolis.episode(EPISODE_URL)


def test_episode_refuses_a_work_url(client):
    alphapolis, _ = client()
    with pytest.raises(UnsupportedUrlError):
        alphapolis.episode(WORK_URL)


# --- series -------------------------------------------------------------------------------


def test_series_urls_lists_an_official_work_in_order_without_duplicates(client):
    alphapolis, session = client()
    assert alphapolis.series_urls(WORK_URL) == [EPISODE_URL, NEXT_URL]
    assert alphapolis.series_urls(WORK_URL + "/") == [EPISODE_URL, NEXT_URL]
    assert session.calls == [WORK_URL]
    assert all(AlphaPolis.suitable(url) for url in alphapolis.series_urls(WORK_URL))


def test_series_urls_flattens_the_chapters_of_a_user_work(client):
    alphapolis, _ = client()
    assert alphapolis.series_urls(USER_WORK_URL) == [
        USER_EPISODE_URL,
        f"{USER_WORK_URL}/episode/11788108",
        f"{USER_WORK_URL}/episode/11800000",
    ]


def test_series_urls_with_an_empty_listing(client, fake_response):
    html = WORK_HTML.replace(json.dumps([*EPISODES, EPISODES[1]], ensure_ascii=False), "[]")
    alphapolis, _ = client({WORK_URL: fake_response(text=html)})
    with pytest.raises(NotAnEpisodePageError, match="lists no episode"):
        alphapolis.series_urls(WORK_URL)


def test_series_urls_without_a_listing(client, fake_response):
    alphapolis, _ = client({WORK_URL: fake_response(text="<html><body>gone</body></html>")})
    with pytest.raises(NotAnEpisodePageError, match="no episode list"):
        alphapolis.series_urls(WORK_URL)


def test_series_urls_refuses_an_episode_url(client):
    alphapolis, _ = client()
    with pytest.raises(UnsupportedUrlError):
        alphapolis.series_urls(EPISODE_URL)


# --- downloading --------------------------------------------------------------------------


def test_download_descrambles_and_saves_every_page(client, tmp_path):
    alphapolis, session = client()
    result = Downloader(alphapolis, tmp_path).download(EPISODE_URL)

    assert result.status == "saved"
    assert (
        result.save_dir
        == tmp_path
        / "www.alphapolis.co.jp"
        / "Ｆ級テイマーは数の暴力で世界を裏から支配する"
        / "第1回『最下級スキルの力』"
    )
    assert sorted(path.name for path in result.save_dir.iterdir()) == ["0.jpg", "1.jpg"]
    assert session.headers_seen[-1]["Referer"] == EPISODE_URL

    expected = page_image()
    for name in ("0.jpg", "1.jpg"):
        with Image.open(result.save_dir / name) as saved:
            assert saved.size == PAGE_SIZE
            # JPEG blurs the gradient a little; a misplaced tile is off by far more.
            diffs = [abs(a - b) for a, b in zip(saved.convert("RGB").tobytes(), expected.tobytes(), strict=True)]
            assert max(diffs) < 40


# --- logging in ---------------------------------------------------------------------------


def test_login_posts_the_form(client, fake_response):
    alphapolis, session = client(
        {LOGIN_URL: [fake_response(text=LOGIN_HTML), fake_response(text=SIGNED_IN_HTML, url=f"{BASE_URL}/mypage")]},
    )
    alphapolis.login(EPISODE_URL, "someone@example.com", "hunter2")

    assert session.posts == [
        (LOGIN_URL, {"_token": "form-token", "email": "someone@example.com", "password": "hunter2"})
    ]


def test_login_raises_with_the_site_reason(client, fake_response):
    alphapolis, _ = client({LOGIN_URL: [fake_response(text=LOGIN_HTML), fake_response(text=REFUSED_HTML)]})
    with pytest.raises(LoginError, match="ログインに失敗しました"):
        alphapolis.login(EPISODE_URL, "someone@example.com", "wrong")


def test_login_raises_when_the_landing_page_knows_no_account(client, fake_response):
    alphapolis, _ = client(
        {LOGIN_URL: [fake_response(text=LOGIN_HTML), fake_response(text=REFUSED_HTML, url=f"{BASE_URL}/")]},
    )
    with pytest.raises(LoginError, match="refused"):
        alphapolis.login(EPISODE_URL, "someone@example.com", "wrong")


def test_login_raises_without_a_form(client, fake_response):
    alphapolis, _ = client({LOGIN_URL: fake_response(text="<html><body></body></html>")})
    with pytest.raises(LoginError, match="no sign-in form"):
        alphapolis.login(EPISODE_URL, "someone@example.com", "hunter2")


# --- the real site ------------------------------------------------------------------------

# One free episode per known host: the first, scrambled, episode of an official
# serialisation. `test_user_work_download` covers the unscrambled user works.
TEST_URLS: dict[str, str] = {
    "www.alphapolis.co.jp": "https://www.alphapolis.co.jp/manga/official/166000762/12255",
}


@pytest.mark.network
@pytest.mark.geoblocked
@pytest.mark.parametrize("host", TEST_URLS)
def test_site_download(tmp_path, host):
    result = Downloader(AlphaPolis(), tmp_path, only_first=True).download(TEST_URLS[host])
    assert result.status == "saved"
    assert (result.save_dir / "0.jpg").stat().st_size > 0


@pytest.mark.network
@pytest.mark.geoblocked
def test_user_work_download(tmp_path):
    result = Downloader(AlphaPolis(), tmp_path, only_first=True).download(USER_EPISODE_URL)
    assert result.status == "saved"
    assert (result.save_dir / "0.jpg").stat().st_size > 0


@pytest.mark.network
@pytest.mark.geoblocked
def test_work_page_lists_episodes():
    urls = AlphaPolis().series_urls("https://www.alphapolis.co.jp/manga/official/71000731")
    assert "https://www.alphapolis.co.jp/manga/official/71000731/10869" in urls
    assert all(AlphaPolis.suitable(url) for url in urls)


@pytest.mark.network
@pytest.mark.geoblocked
def test_a_rental_episode_is_locked():
    episode = AlphaPolis().episode("https://www.alphapolis.co.jp/manga/official/71000731/10870")
    assert episode.pages == ()
    assert episode.next_url == "https://www.alphapolis.co.jp/manga/official/71000731/11247"
