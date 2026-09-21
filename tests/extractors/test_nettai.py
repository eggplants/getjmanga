from __future__ import annotations

from datetime import date
from io import BytesIO

import pytest
from PIL import Image

from getjmanga.downloader import Downloader
from getjmanga.errors import NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractors import nettai
from getjmanga.extractors.nettai import (
    BASE_URL,
    LAST_PAGE_URL,
    LICENSE_URL,
    Nettai,
    viewer_url,
)
from getjmanga.viewers.publus import Pack, page_seeds, tile_slices

CID = "eyJpdiI6InE2dXJxNnVycTZ1cnE2dXJxNnVycXc9PSIsInZhbHVlIjoiYkZYeHg3ZVNremNF"
NEXT_CID = "eyJpdiI6InE2dXJxNnVycTZ1cnE2dXJxNnVycXc9PSIsInZhbHVlIjoiNjFKSkszNTZ2NWNR"
LOCKED_CID = "locked-cid"
LAST_CID = "last-cid"
EPISODE_URL = f"{BASE_URL}/publus/viewer.html?cid={CID}"
NEXT_URL = f"{BASE_URL}/publus/viewer.html?cid={NEXT_CID}"
LOCKED_URL = f"{BASE_URL}/publus/viewer.html?cid={LOCKED_CID}"
LAST_URL = f"{BASE_URL}/publus/viewer.html?cid={LAST_CID}"
SERIES_URL = f"{BASE_URL}/book/1"
CONTENT_URL = "https://cdn.comicnettai.com/9_c2b3fc5aa6547df94ec500f29b54c231/epub/book_contents/c1/"
SERIES_TITLE = "グレイト トレイラーズ"
KEYS = (bytes(range(32)), bytes(range(32, 64)), bytes(range(64, 96)))

# The first page as the pack describes it.
PAGE = {
    "BlockHeight": 32,
    "BlockWidth": 32,
    "ContentArea": {"Height": 48, "Width": 63, "X": 0, "Y": 0},
    "DummyHeight": 0,
    "DummyWidth": 1,
    "LinkList": [],
    "No": 0,
    "Rect": {"Height": 48, "Width": 63, "X": 0, "Y": 0},
    "Shrink": 1.0,
    "Size": {"Height": 48, "Width": 63},
    "NS": 1,
    "PS": 2,
    "RS": 3,
}
# What the pack decodes to, without `file-name-version` so the page files keep their numbers.
PACK_JSON = {
    "configuration": {
        "contents": [
            {"file": "item/xhtml/p-cover.xhtml", "index": 1, "type": "jpeg"},
            {"file": "item/xhtml/p-000.xhtml", "index": 2, "type": "jpeg"},
            {"file": "item/xhtml/p-001.xhtml", "index": 3, "type": "jpeg"},
        ],
    },
    "item/xhtml/p-cover.xhtml": {
        "FileLinkInfo": {"PageCount": 1, "PageLinkInfoList": [{"Page": PAGE}]},
        "Linear": 1,
    },
    # A page without shuffle parameters is served as is.
    "item/xhtml/p-000.xhtml": {
        "FileLinkInfo": {"PageCount": 1, "PageLinkInfoList": [{"Page": {"No": 0, "Size": {"Width": 8, "Height": 8}}}]},
        "Linear": 1,
    },
    # A non-linear page is skipped.
    "item/xhtml/p-001.xhtml": {
        "FileLinkInfo": {"PageCount": 1, "PageLinkInfoList": [{"Page": PAGE}]},
        "Linear": 0,
    },
}

LICENSE = {"status": "200", "url": CONTENT_URL, "cti": "#001（前編）", "cty": 1, "lpd": 1, "lin": 0, "lp": None}
REFUSED = {"status": 400}


def last_page(content_id):
    return {"status": "200", "url": "invisible", "iframe": f"{BASE_URL}/colophon?book_content_id={content_id}"}


def colophon_html(next_url=None):
    next_link = (
        f'<a class="js-open-next-url btn-colophon-nextepisode" href="{next_url}">次の話を読む</a>' if next_url else ""
    )
    return f"""<html><body><div class="btn-colophon-wrapper">{next_link}
<a href="{SERIES_URL}" class="js-open-next-url btn-colophon-back" data-href="{SERIES_URL}">作品ページへ戻る</a>
</div></body></html>"""


# The empty stub an unknown `book_content_id` gets.
EMPTY_HTML = "<html><head></head><body></body></html>"


def listing_html(episodes, *, last, closed=()):
    """A work page: `episodes` as `(cid, title, content_id)`, `closed` as `(title, content_id)`."""
    items = [
        f"""<a class="js-open-next-url detail--product__item is-open" href={viewer_url(cid)}>
<div class="detail--product__item__left">
<img class="lazyload detail--product__thum" data-src="https://cdn.comicnettai.com/x/book_contents/{content_id}/v.jpg">
</div><div class="detail--product__item__center"><h2 class="detail--product__item__title">{title}</h2>
<p class="detail--product__item__sdate">2021.10.22</p></div></a>"""
        for cid, title, content_id in episodes
    ]
    items += [
        f"""<div class=" detail--product__item is-close">
<img class="lazyload detail--product__thum" data-src="https://cdn.comicnettai.com/x/book_contents/{content_id}/v.jpg">
<p class="detail--product__item__title_close-atention">公開は終了しました</p>
<h2 class="detail--product__item__title">{title}</h2></div>"""
        for title, content_id in closed
    ]
    hidden = " is-hidde" if last else ""
    return f"""<html><body><h1 class="detail--title">{SERIES_TITLE}</h1>
<div class="detail__author__list"><span class="detail__author__item" href="">宮川輝</span></div>
<div class="container detail--product__list">{"".join(items)}</div>
<ul class="pagenation_list">
<li class="pagenation__item is-active"><span class="pagenation__item__link">1</span></li>
<li class="pagenation__item{hidden}"><a class="pagenation__item__link pagenation__item__link--next"
 href="{SERIES_URL}?sort_type=priority_asc&amp;page=2"></a></li>
</ul></body></html>"""


def png_bytes(image):
    raw = BytesIO()
    image.save(raw, "PNG")
    return raw.getvalue()


def striped(size, block=8):
    """A page whose tiles are all different, so a misplaced one shows."""
    image = Image.new("RGB", size)
    image.putdata(
        [
            ((x // block * 37) % 256, (y // block * 59) % 256, (x + y) % 256)
            for y in range(size[1])
            for x in range(size[0])
        ]
    )
    return image


def scramble(image, pattern, seeds, block):
    """What the CDN serves: the inverse of `boost.descramble()`."""
    out = Image.new(image.mode, image.size)
    for piece in tile_slices(image.width, image.height, block[0], block[1], pattern=pattern, seeds=seeds):
        tile = image.crop((piece.dst_x, piece.dst_y, piece.dst_x + piece.width, piece.dst_y + piece.height))
        out.paste(tile, (piece.src_x, piece.src_y))
    return out


# The page as drawn (63x48) and as served: one dummy column wider, tiles shuffled
# with the parameters the pack's keys and the page's `NS`/`PS`/`RS` give.
CLEAN = striped((63, 48))
PADDED = Image.new("RGB", (64, 48), (255, 0, 255))
PADDED.paste(CLEAN, (0, 0))
SEEDS = page_seeds("item/xhtml/p-cover.xhtml", "0", PAGE, KEYS)
SERVED = png_bytes(scramble(PADDED, SEEDS["pattern"], tuple(SEEDS["seeds"]), tuple(SEEDS["block"])))


@pytest.fixture
def client(fake_session, fake_response, monkeypatch):
    """A `Nettai` over a session scripting episode 1, a locked one and the last one."""
    monkeypatch.setattr(nettai, "decode_pack", lambda _text: Pack(PACK_JSON, KEYS))

    def build(extra=None):
        listing = listing_html(
            [(CID, "#001（前編）", "1"), (NEXT_CID, "#001（後編）", "9")],
            last=True,
            closed=[("#002（前編）", "10")],
        )
        routes = {
            f"{LICENSE_URL}?cid={CID}": fake_response(payload=LICENSE),
            f"{LICENSE_URL}?cid={LAST_CID}": fake_response(payload={**LICENSE, "cti": "#027"}),
            LICENSE_URL: fake_response(payload=REFUSED),
            f"{LAST_PAGE_URL}?cid={CID}": fake_response(payload=last_page(1)),
            f"{LAST_PAGE_URL}?cid={LOCKED_CID}": fake_response(payload=last_page(10)),
            f"{LAST_PAGE_URL}?cid={LAST_CID}": fake_response(payload=last_page(663)),
            LAST_PAGE_URL: fake_response(payload=REFUSED),
            # Longer ids first: the routes match by substring.
            "book_content_id=10": fake_response(text=colophon_html(LAST_URL)),
            "book_content_id=663": fake_response(text=colophon_html()),
            "book_content_id=1": fake_response(text=colophon_html(NEXT_URL)),
            "/colophon": fake_response(text=EMPTY_HTML),
            "/book/1": fake_response(text=listing),
            "/book/": fake_response(text=EMPTY_HTML),
            "configuration_pack.json": fake_response(text='{"version": "1.0", "data": "..."}'),
            "p-cover.xhtml/": fake_response(SERVED, content_type="image/jpeg"),
            ".jpeg": fake_response(png_bytes(Image.new("RGB", (8, 8), (1, 2, 3))), content_type="image/jpeg"),
        }
        routes.update(extra or {})  # an override keeps the route's place in the match order
        session = fake_session(routes)
        return Nettai(session), session

    return build


# --- URLs ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        EPISODE_URL,
        f"{BASE_URL}/publus/viewer.html?cid=abc%2Fdef%3D&foo=1",
        SERIES_URL,
        f"{BASE_URL}/book/794/",
        f"{BASE_URL}/book/794?sort_type=priority_asc",
    ],
)
def test_suitable_accepts_viewer_and_work_urls(url):
    assert Nettai.suitable(url)


@pytest.mark.parametrize(
    "url",
    [
        f"http://www.comicnettai.com/publus/viewer.html?cid={CID}",
        f"https://comicnettai.com/publus/viewer.html?cid={CID}",
        f"{BASE_URL}/publus/viewer.html",
        f"{BASE_URL}/publus/viewer.html?cid=",
        f"{BASE_URL}/",
        f"{BASE_URL}/book",
        f"{BASE_URL}/book/",
        f"{BASE_URL}/series",
        f"{BASE_URL}/colophon?book_content_id=1",
        f"https://comic-boost.com/viewer/viewer.html?cid={CID}",
    ],
)
def test_suitable_rejects_other_urls(url):
    assert not Nettai.suitable(url)


@pytest.mark.parametrize(
    ("url", "expected"),
    [(EPISODE_URL, False), (SERIES_URL, True), (f"{BASE_URL}/book/794/", True)],
)
def test_is_series(url, expected):
    assert Nettai.is_series(url) is expected


# --- the pages the site serves -----------------------------------------------------


# --- episode() ---------------------------------------------------------------------


def test_episode_reads_the_titles_the_pages_and_the_next_episode(client):
    nettai_, session = client()
    episode = nettai_.episode(EPISODE_URL)

    assert episode.url == EPISODE_URL
    assert episode.series_title == SERIES_TITLE
    assert (episode.writer, episode.publisher) == ("宮川輝", "光文社")
    assert (episode.published, episode.number) == (date(2021, 10, 22), 1)
    assert episode.episode_title == "#001（前編）"
    assert (episode.prev_url, episode.next_url) == (None, NEXT_URL)
    assert [page.url for page in episode.pages] == [
        f"{CONTENT_URL}item/xhtml/p-cover.xhtml/0.jpeg",
        f"{CONTENT_URL}item/xhtml/p-000.xhtml/0.jpeg",
    ]
    assert (episode.pages[0].width, episode.pages[0].height) == (63, 48)
    assert episode.pages[0].extra == SEEDS
    assert episode.pages[1].extra == {}
    assert episode.metadata["book_content_id"] == "1"
    assert episode.metadata["license"] == LICENSE
    assert episode.metadata["configuration"] == PACK_JSON["configuration"]

    # The viewer's API calls carry the cid as a parameter and the viewer as Referer. The work page
    # is read twice: for the titles, and walked for the episode before this one.
    assert session.calls[:6] == [
        f"{LICENSE_URL}?cid={CID}",
        f"{LAST_PAGE_URL}?cid={CID}",
        f"{BASE_URL}/colophon?book_content_id=1",
        f"{BASE_URL}/book/1?sort_type=priority_asc&page=1",
        f"{BASE_URL}/book/1?sort_type=priority_asc&page=1",
        f"{CONTENT_URL}configuration_pack.json",
    ]
    assert session.headers_seen[0]["Referer"] == EPISODE_URL
    assert session.headers_seen[1]["Referer"] == EPISODE_URL
    assert session.params_seen[0] is None


def test_episode_normalises_the_url_to_the_cid(client):
    nettai_, _ = client()
    episode = nettai_.episode(f"{BASE_URL}/publus/viewer.html?cid={CID}&com-access-no-history")
    assert episode.url == EPISODE_URL


def test_locked_episode_has_no_pages_but_keeps_its_titles_and_the_next(client):
    nettai_, session = client()
    episode = nettai_.episode(LOCKED_URL)

    assert not episode.readable
    assert episode.series_title == SERIES_TITLE
    # The license names nothing, so the title comes off the work page by content id.
    assert episode.episode_title == "#002（前編）"
    assert episode.next_url == LAST_URL
    assert episode.metadata["license"] == REFUSED
    assert not any("configuration_pack" in url for url in session.calls)


def test_locked_episode_off_the_listing_is_named_by_its_content_id(client, fake_response):
    nettai_, _ = client({f"{LAST_PAGE_URL}?cid={LOCKED_CID}": fake_response(payload=last_page(12345))})
    episode = nettai_.episode(LOCKED_URL)
    assert not episode.readable
    assert episode.episode_title == "12345"
    assert episode.series_title == SERIES_TITLE


def test_last_episode_has_no_next(client):
    nettai_, _ = client()
    episode = nettai_.episode(LAST_URL)
    assert episode.readable
    assert episode.episode_title == "#027"
    assert episode.next_url is None


def test_unknown_cid_is_not_an_episode_page(client):
    nettai_, _ = client()
    with pytest.raises(NotAnEpisodePageError, match="no episode behind"):
        nettai_.episode(f"{BASE_URL}/publus/viewer.html?cid=bogus")


def test_readable_episode_without_a_colophon_falls_back_to_the_license_title(client, fake_response):
    nettai_, _ = client({f"{LAST_PAGE_URL}?cid={CID}": fake_response(payload=REFUSED)})
    episode = nettai_.episode(EPISODE_URL)
    assert episode.readable
    assert episode.series_title == "#001（前編）"
    assert episode.episode_title == "#001（前編）"
    assert episode.next_url is None
    assert episode.metadata["book_content_id"] == ""


def test_readable_episode_of_an_unknown_work_falls_back_to_the_license_title(client, fake_response):
    nettai_, _ = client({"/book/1": fake_response(text=EMPTY_HTML)})
    episode = nettai_.episode(EPISODE_URL)
    assert episode.readable
    assert episode.series_title == "#001（前編）"
    assert episode.next_url == NEXT_URL


def test_episode_rejects_a_work_url(client):
    nettai_, session = client()
    with pytest.raises(UnsupportedUrlError):
        nettai_.episode(SERIES_URL)
    assert session.calls == []


# --- series_urls() -----------------------------------------------------------------


def test_series_urls_walks_the_pages_first_episode_first(fake_session, fake_response):
    session = fake_session(
        {
            "page=1": fake_response(
                text=listing_html([(CID, "#001（前編）", "1"), (NEXT_CID, "#001（後編）", "9")], last=False)
            ),
            "page=2": fake_response(
                text=listing_html([(NEXT_CID, "#001（後編）", "9"), (LAST_CID, "#027", "663")], last=True)
            ),
            "page=3": fake_response(text=listing_html([("never", "never", "0")], last=True)),
        }
    )
    urls = Nettai(session).series_urls(f"{BASE_URL}/book/1/")

    assert urls == [EPISODE_URL, NEXT_URL, LAST_URL]
    assert session.calls == [
        f"{BASE_URL}/book/1?sort_type=priority_asc&page=1",
        f"{BASE_URL}/book/1?sort_type=priority_asc&page=2",
    ]


def test_series_urls_stops_when_a_page_brings_nothing_new(fake_session, fake_response):
    repeated = listing_html([(CID, "#001（前編）", "1")], last=False)
    session = fake_session({"/book/1": fake_response(text=repeated)})
    assert Nettai(session).series_urls(SERIES_URL) == [EPISODE_URL]
    assert len(session.calls) == 2


def test_series_urls_of_a_work_with_no_open_episode_is_not_an_episode_page(fake_session, fake_response):
    session = fake_session({"/book/1": fake_response(text=listing_html([], last=True, closed=[("#001", "1")]))})
    with pytest.raises(NotAnEpisodePageError, match="no open episode"):
        Nettai(session).series_urls(SERIES_URL)


def test_series_urls_of_an_unknown_work_is_not_an_episode_page(fake_session, fake_response):
    session = fake_session({"/book/": fake_response(text=EMPTY_HTML)})
    with pytest.raises(NotAnEpisodePageError):
        Nettai(session).series_urls(f"{BASE_URL}/book/999999")


def test_series_urls_rejects_a_viewer_url(client):
    nettai_, _ = client()
    with pytest.raises(UnsupportedUrlError):
        nettai_.series_urls(EPISODE_URL)


# --- image() and the download ------------------------------------------------------


def test_image_unshuffles_the_tiles_and_trims_the_dummy_column(client):
    nettai_, session = client()
    episode = nettai_.episode(EPISODE_URL)

    image = nettai_.image(episode.pages[0], episode)

    assert image.size == (63, 48)
    assert image.tobytes() == CLEAN.tobytes()
    assert session.headers_seen[-1]["Referer"] == EPISODE_URL


# --- the real site -----------------------------------------------------------------

# One episode per known host, free to read without an account: the first half of グレイト トレイラーズ #001.
TEST_URLS: dict[str, str] = {
    "www.comicnettai.com": (
        "https://www.comicnettai.com/publus/viewer.html?cid="
        "eyJpdiI6InE2dXJxNnVycTZ1cnE2dXJxNnVycXc9PSIsInZhbHVlIjoiYkZYeHg3ZVNremNFd3QwbkQrV0NLQUx0UUFvSFZOTWhtMjk0akhm"
        "R2JVTnVLTkVCSi9mSzZ6WUJNYkJhcHpsQk1hZmtEUE1Gb3F6dGEzbG5wY1VxUXFpb0MvUTFNZVJiRTU3SVdBWjBHcU09IiwibWFjIjoiMjQ4"
        "ZjFlZTdjNzcxMzhlODliMzg4Nzk4MTlmNGRhNWRkMmJkOTg5YTA0ZGI3YzdlMjk3NjMwYmEyNGJmZDc2OCJ9"
    ),
}


@pytest.mark.network
@pytest.mark.parametrize("host", TEST_URLS)
def test_site_download(tmp_path, host):
    result = Downloader(Nettai(), tmp_path, only_first=True).download(TEST_URLS[host])
    assert result.status == "saved"
    assert result.episode.series_title == SERIES_TITLE
    assert (result.save_dir / "0.jpg").stat().st_size > 0


@pytest.mark.network
def test_site_series_lists_open_episodes_that_download():
    nettai_ = Nettai()
    urls = nettai_.series_urls(SERIES_URL)
    assert len(urls) >= 10
    assert len(set(urls)) == len(urls)
    assert all(Nettai.suitable(url) for url in urls)
    # The cids the work page mints are as good as the one in TEST_URLS.
    episode = nettai_.episode(urls[0])
    assert episode.readable
    assert episode.episode_title == "#001（前編）"
    assert episode.next_url is not None


@pytest.mark.network
def test_site_unknown_cid_is_not_an_episode_page():
    with pytest.raises(NotAnEpisodePageError):
        Nettai().episode(f"{BASE_URL}/publus/viewer.html?cid=bogus")
