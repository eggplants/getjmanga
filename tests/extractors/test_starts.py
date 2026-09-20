from __future__ import annotations

import json
from http import HTTPStatus
from io import BytesIO

import pytest
from PIL import Image

from getjmanga.downloader import Downloader
from getjmanga.errors import GetjmangaError, NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractors.starts import (
    Starts,
    descramble,
    parse_comic_data,
)
from getjmanga.viewers.seedrandom import shuffle_order

ORIGIN = "https://www.berrys-cafe.jp"
SERIES_URL = f"{ORIGIN}/comic/serial/n53"
EPISODE_URL = f"{ORIGIN}/comic/serial/n53/n24/1"
CLOSED_URL = f"{ORIGIN}/comic/serial/n53/n23/1"
UPDATED_AT = 1770012000
CONTENT = f"{ORIGIN}/img/serial-comic/53/24/content"

# A series page cut down to the parts read: the title, an upcoming episode
# (no story number yet), an ebook-only one, two open ones, a closed one.
SERIES_HTML = f"""
<html><head><title>イジワル同居人は御曹司!?</title></head><body>
<section class="section"><dl>
<dt class="comicTit">イジワル同居人は御曹司!?</dt>
<dd class="comicCatch">catch</dd>
</dl></section>
<section class="section comicSerial"><div class="comicSerialList">
<article class="cs">
    <img src="/img/serial-comic/53/26/thumb.jpg" alt="13話-②">
    <p class="serialTit">13話-②</p>
    <div><p class="serialStatus">09/24公開予定</p></div>
</article>
<article class="close readByEbook">
    <img src="/img/serial-comic/53/33/thumb.jpg" alt="17話～">
    <p class="serialTit"><span class="storyTitle" data-story-number="33">
        17話～
    </span></p>
    <div><p class="serialStatus">各電子書店で読む</p></div>
</article>
<article>
    <a href="{ORIGIN}/comic/serial/n53/n25/1">
        <img src="/img/serial-comic/53/25/thumb.jpg" alt="13話-①">
        <p class="serialTit"><span data-story-number="25" class="storyTitle">
            13話-①
        </span></p>
        <div><p class="serialStatus ">11/19まで<br>無料公開中</p></div>
    </a>
</article>
<article>
    <a href="{ORIGIN}/comic/serial/n53/n24/1">
        <img src="/img/serial-comic/53/24/thumb.jpg" alt="12話-②">
        <p class="serialTit"><span data-story-number="24" class="storyTitle">
            12話-②
        </span></p>
        <div><p class="serialStatus ">無料公開中</p></div>
    </a>
</article>
<article class="close readByEbook">
    <img src="/img/serial-comic/53/23/thumb.jpg" alt="12話-①">
    <p class="serialTit"><span class="storyTitle" data-story-number="23">
        12話-①
    </span></p>
    <div><p class="serialStatus">各電子書店で読む</p></div>
</article>
</div></section>
</body></html>
"""

COMIC_DATA = {
    "serial_comic_id": 53,
    "serial_comic_label_id": 1,
    "story_title": "12話-②",
    "story_updated_at": UPDATED_AT,
    "story_number": 24,
    "page": 1,
    "assets_version": "1.0.964",
    "is_preview": False,
}
EPISODE_HTML = f"""
<html><head><title>イジワル同居人は御曹司!? 12話-②</title></head><body>
<div id="comicViewer"><comic-viewer></comic-viewer></div>
<script id="comic-data" type="application/json">
    {json.dumps(COMIC_DATA)}
</script>
</body></html>
"""
NOT_FOUND_HTML = "<html><head><title>ページが見つかりません | ベリーズカフェ</title></head><body>404</body></html>"

INDEX = [
    {"name": "cover.jpg", "seed": "1419cb7d", "size": 4, "spread": "left"},
    {"name": "i-001.jpg", "seed": "22f5c8d7", "size": 4, "spread": "right"},
    {"name": "i-002.jpg", "seed": "befb9e11", "size": 4},
]


def close_enough(image, original, tile, tolerance=8):
    """Whether every tile's centre matches, which survives JPEG's ringing at the tile edges."""
    if image.size != original.size:
        return False
    for y in range(tile // 2, original.height, tile):
        for x in range(tile // 2, original.width, tile):
            got = image.crop((x, y, x + 1, y + 1)).convert("RGB").tobytes()
            want = original.crop((x, y, x + 1, y + 1)).convert("RGB").tobytes()
            if any(abs(a - b) >= tolerance for a, b in zip(got, want, strict=True)):
                return False
    return True


def jpeg_bytes(image):
    raw = BytesIO()
    image.save(raw, "JPEG", quality=95)
    return raw.getvalue()


def tiled_image(width, height, tile):
    """A page whose tiles are each one flat colour, so a shuffle shows."""
    image = Image.new("RGB", (width, height))
    columns = -(-width // tile)
    rows = -(-height // tile)
    for index in range(columns * rows):
        row, column = divmod(index, columns)
        color = (index * 40 % 256, 255 - index * 40 % 256, 128)
        image.paste(color, (column * tile, row * tile, min(width, (column + 1) * tile), min(height, (row + 1) * tile)))
    return image


def scramble(image, seed, size):
    """Shuffle a page the way the viewer's server side did, for the tests."""
    tile = max(image.size) // size
    columns = -(-image.width // tile)
    rows = -(-image.height // tile)
    groups: dict[tuple[int, int], list[tuple[int, int]]] = {}
    for index in range(columns * rows):
        row, column = divmod(index, columns)
        x, y = column * tile, row * tile
        shape = (min(tile, image.width - x), min(tile, image.height - y))
        groups.setdefault(shape, []).append((x, y))
    out = image.copy()
    for (tile_width, tile_height), corners in groups.items():
        group_x, group_y = corners[0]
        group_columns = sum(1 for _, y in corners if y == group_y)
        for (dest_x, dest_y), source in zip(corners, shuffle_order(len(corners), seed), strict=True):
            row, column = divmod(source, group_columns)
            x, y = group_x + column * tile_width, group_y + row * tile_height
            # The viewer draws source tile `source` at destination `dest`; the
            # scramble is the inverse: destination `dest`'s content goes to `source`.
            out.paste(image.crop((dest_x, dest_y, dest_x + tile_width, dest_y + tile_height)), (x, y))
    return out


@pytest.fixture
def client(fake_session, fake_response):
    def build(extra_routes=None):
        routes = {
            "/comic/serial/n53/n24/1": fake_response(text=EPISODE_HTML),
            "/comic/serial/n53/n23/1": fake_response(text=NOT_FOUND_HTML, status_code=HTTPStatus.NOT_FOUND),
            "/comic/serial/n53/n99/1": fake_response(text=NOT_FOUND_HTML, status_code=HTTPStatus.NOT_FOUND),
            "/img/serial-comic/53/24/content/index.json": fake_response(
                text=json.dumps(INDEX), payload=INDEX, content_type="application/json"
            ),
            **(extra_routes or {}),
            # Last, so the episode routes above win on the shared prefix.
            "/comic/serial/n53": fake_response(text=SERIES_HTML),
        }
        session = fake_session(routes)
        return Starts(session), session

    return build


# --- URLs ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://www.berrys-cafe.jp/comic/serial/n291/n1/1",
        "https://www.berrys-cafe.jp/comic/serial/n291/n1",
        "https://www.berrys-cafe.jp/comic/serial/n291/n12/3/",
        "https://www.berrys-cafe.jp/comic/serial/n291",
        "https://www.berrys-cafe.jp/comic/serial/n291/",
        "https://www.no-ichigo.jp/comic/serial/n294/n1/1",
        "https://www.no-ichigo.jp/comic/serial/n294",
        "https://novema.jp/comic/serial/n284/n1/1",
        "https://novema.jp/comic/serial/n284",
    ],
)
def test_suitable_accepts_episode_and_series_urls(url):
    assert Starts.suitable(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://www.berrys-cafe.jp/comic/serial/n291/n1/1",
        "https://berrys-cafe.jp/comic/serial/n291/n1/1",
        "https://www.berrys-cafe.jp/comic/serial",
        "https://www.berrys-cafe.jp/comic/serial/daily/friday",
        "https://www.berrys-cafe.jp/comic/comic-fantasy/book/n353",
        "https://www.berrys-cafe.jp/book/n1694657",
        "https://www.no-ichigo.jp/",
        "https://novema.jp/comic/serial/n284/1",
        "https://example.com/comic/serial/n291/n1/1",
    ],
)
def test_suitable_rejects_other_urls(url):
    assert not Starts.suitable(url)


def test_is_series_tells_a_series_page_from_an_episode():
    assert Starts.is_series(SERIES_URL)
    assert Starts.is_series(f"{SERIES_URL}/")
    assert not Starts.is_series(EPISODE_URL)
    assert not Starts.is_series(f"{ORIGIN}/comic/serial")


# --- parsers ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "html",
    [
        NOT_FOUND_HTML,
        '<script id="comic-data" type="application/json">not json</script>',
        '<script id="comic-data" type="application/json">[1, 2]</script>',
    ],
)
def test_parse_comic_data_returns_none_without_a_viewer(html):
    assert parse_comic_data(html) is None


# --- episode ------------------------------------------------------------------------


def test_episode_reads_the_titles_the_pages_and_the_next(client):
    starts, session = client()
    episode = starts.episode(EPISODE_URL)

    assert episode.url == EPISODE_URL
    assert episode.series_title == "イジワル同居人は御曹司!?"
    assert episode.episode_title == "12話-②"
    assert [page.url for page in episode.pages] == [
        f"{CONTENT}/cover.jpg?t={UPDATED_AT}",
        f"{CONTENT}/i-001.jpg?t={UPDATED_AT}",
        f"{CONTENT}/i-002.jpg?t={UPDATED_AT}",
    ]
    assert episode.pages[0].extra == {"seed": "1419cb7d", "size": 4}
    assert episode.next_url == f"{ORIGIN}/comic/serial/n53/n25/1"
    assert episode.metadata["comic_data"] == COMIC_DATA
    assert episode.metadata["images"] == INDEX
    assert episode.metadata["story"] == {"number": 24, "title": "12話-②", "url": EPISODE_URL, "readable": True}
    json.dumps(episode.metadata)
    json.dumps([dict(page.extra) for page in episode.pages])

    index_call = session.calls.index(f"{ORIGIN}/img/serial-comic/53/24/content/index.json")
    assert session.params_seen[index_call] == {"t": str(UPDATED_AT)}
    assert session.headers_seen[index_call]["Referer"] == EPISODE_URL
    assert session.headers_seen[index_call]["X-Requested-With"] == "XMLHttpRequest"


def test_episode_takes_a_url_without_the_page_number_and_canonicalises_it(client):
    starts, session = client()
    episode = starts.episode(f"{ORIGIN}/comic/serial/n53/n24")

    assert episode.url == EPISODE_URL
    assert session.calls[0] == EPISODE_URL


def test_closed_episode_has_no_pages_but_still_a_next(client):
    starts, _ = client()
    episode = starts.episode(CLOSED_URL)

    assert not episode.readable
    assert episode.pages == ()
    assert episode.episode_title == "12話-①"
    assert episode.series_title == "イジワル同居人は御曹司!?"
    assert episode.next_url == EPISODE_URL
    assert episode.metadata["comic_data"] is None


def test_last_listed_episode_has_no_next(client, fake_response):
    starts, _ = client(
        {"/comic/serial/n53/n33/1": fake_response(text=NOT_FOUND_HTML, status_code=HTTPStatus.NOT_FOUND)}
    )
    episode = starts.episode(f"{ORIGIN}/comic/serial/n53/n33/1")

    assert not episode.readable
    assert (episode.prev_url, episode.next_url) == (f"{ORIGIN}/comic/serial/n53/n25/1", None)


def test_episode_the_series_does_not_list_is_not_an_episode(client):
    starts, _ = client()
    with pytest.raises(NotAnEpisodePageError, match="no viewer"):
        starts.episode(f"{ORIGIN}/comic/serial/n53/n99/1")


def test_episode_without_index_json_is_locked(client, fake_response):
    starts, _ = client(
        {"/img/serial-comic/53/24/content/index.json": fake_response(text="", status_code=HTTPStatus.NOT_FOUND)}
    )
    episode = starts.episode(EPISODE_URL)

    assert not episode.readable
    assert episode.episode_title == "12話-②"
    assert episode.next_url == f"{ORIGIN}/comic/serial/n53/n25/1"


def test_episode_rejects_a_series_url(client):
    starts, _ = client()
    with pytest.raises(UnsupportedUrlError):
        starts.episode(SERIES_URL)


# --- series -------------------------------------------------------------------------


def test_series_urls_lists_every_story_in_order(client):
    starts, _ = client()
    urls = starts.series_urls(SERIES_URL)

    assert urls == [
        CLOSED_URL,
        EPISODE_URL,
        f"{ORIGIN}/comic/serial/n53/n25/1",
        f"{ORIGIN}/comic/serial/n53/n33/1",
    ]
    assert all(Starts.suitable(url) for url in urls)


def test_series_urls_rejects_an_episode_url(client):
    starts, _ = client()
    with pytest.raises(UnsupportedUrlError):
        starts.series_urls(EPISODE_URL)


def test_series_urls_raises_on_an_empty_listing(fake_session, fake_response):
    session = fake_session({"/comic/serial/n7": fake_response(text="<html><body></body></html>")})
    with pytest.raises(NotAnEpisodePageError, match="lists no episode"):
        Starts(session).series_urls(f"{ORIGIN}/comic/serial/n7")


# --- descrambling -------------------------------------------------------------------


@pytest.mark.parametrize(("width", "height", "size"), [(1055, 1500, 4), (1350, 1920, 4), (200, 120, 5)])
def test_descramble_restores_a_shuffled_page(width, height, size):
    original = tiled_image(width, height, max(width, height) // size)
    scrambled = scramble(original, "22f5c8d7", size)
    assert scrambled.tobytes() != original.tobytes()

    assert descramble(scrambled, "22f5c8d7", size).tobytes() == original.tobytes()


def test_descramble_refuses_a_page_that_does_not_split_into_whole_tiles():
    with pytest.raises(GetjmangaError, match="whole tiles"):
        descramble(Image.new("RGB", (100, 150)), "22f5c8d7", 4)


def test_image_descrambles_with_the_page_seed(client, fake_response):
    original = tiled_image(120, 160, 40)
    starts, session = client(
        {"/content/i-001.jpg": fake_response(jpeg_bytes(scramble(original, "22f5c8d7", 4)), content_type="image/jpeg")}
    )
    episode = starts.episode(EPISODE_URL)
    image = starts.image(episode.pages[1], episode)

    # JPEG is lossy, so allow for its noise.
    assert close_enough(image, original, 40)
    assert session.headers_seen[-1]["Referer"] == EPISODE_URL


def test_image_hands_an_unshuffled_page_over_as_is(client, fake_response):
    original = tiled_image(120, 160, 40)
    starts, _ = client({"/content/i-001.jpg": fake_response(jpeg_bytes(original), content_type="image/jpeg")})
    episode = starts.episode(EPISODE_URL)
    page = episode.pages[1]
    plain = type(page)(url=page.url, extra={"seed": "", "size": 1})

    assert starts.image(plain, episode).size == original.size


# --- download -----------------------------------------------------------------------


# --- the real site --------------------------------------------------------------------

# One free-forever first episode per host (later episodes are free for a
# window of weeks and then close).
TEST_URLS: dict[str, str] = {
    "novema.jp": "https://novema.jp/comic/serial/n284/n1/1",
    "www.berrys-cafe.jp": "https://www.berrys-cafe.jp/comic/serial/n291/n1/1",
    "www.no-ichigo.jp": "https://www.no-ichigo.jp/comic/serial/n294/n1/1",
}


@pytest.mark.network
@pytest.mark.parametrize("host", TEST_URLS)
def test_site_download(tmp_path, host):
    result = Downloader(Starts(), tmp_path, only_first=True).download(TEST_URLS[host])
    assert result.status == "saved"
    assert (result.save_dir / "0.jpg").stat().st_size > 0


@pytest.mark.network
def test_series_page_lists_episodes():
    urls = Starts().series_urls("https://www.berrys-cafe.jp/comic/serial/n291")
    assert urls[0] == "https://www.berrys-cafe.jp/comic/serial/n291/n1/1"
    assert all(Starts.suitable(url) for url in urls)


@pytest.mark.network
def test_closed_episode_is_locked_not_an_error():
    # Episode 23 of this finished series is "各電子書店で読む": listed, but 404.
    episode = Starts().episode("https://www.berrys-cafe.jp/comic/serial/n53/n23/1")
    assert not episode.readable
    assert episode.next_url == "https://www.berrys-cafe.jp/comic/serial/n53/n24/1"
