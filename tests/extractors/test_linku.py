from __future__ import annotations

import base64
import json
import struct
from http import HTTPStatus
from io import BytesIO

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from PIL import Image

from getjmanga.downloader import Downloader
from getjmanga.errors import LoginError, NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractors.linku import (
    FLOWERCOMICS_URL,
    GANGANONLINE_URL,
    MANGALAB_URL,
    MANGAONE_URL,
    MANGAPARK_URL,
    FlowerComics,
    GanganOnline,
    MangaLab,
    MangaOne,
    MangaPark,
    flight_object,
    flight_text,
    next_data,
)
from getjmanga.protobuf import encode_bytes_field, encode_varint_field

KEY = "d6e982ab122e0353a759497da5a3cbf5faadbf6d4b4bf8a1ca5484309c7061a3"
IV = "eff6d3784c6358a5d92b47aab585387c"


def png_bytes(size=(8, 8), colour=(10, 20, 30)):
    raw = BytesIO()
    Image.new("RGB", size, colour).save(raw, "PNG")
    return raw.getvalue()


def encrypted_png(**kwargs):
    plain = png_bytes(**kwargs)
    padding = 16 - len(plain) % 16
    plain += bytes([padding]) * padding
    encryptor = Cipher(algorithms.AES(bytes.fromhex(KEY)), modes.CBC(bytes.fromhex(IV))).encryptor()
    return encryptor.update(plain) + encryptor.finalize()


# --- the Next.js payload helpers --------------------------------------------------


def flight_html(*lines):
    """An app-router page: each line pushed as its own flight chunk, escaped the way Next does."""
    chunks = "".join(f"<script>self.__next_f.push([1,{json.dumps(line)}])</script>" for line in lines)
    return f"<html><body>{chunks}</body></html>"


def next_data_html(payload):
    return (
        f'<html><body><script id="__NEXT_DATA__" type="application/json">{json.dumps(payload)}</script></body></html>'
    )


def test_flight_text_joins_the_chunks_and_undoes_the_escapes():
    html = flight_html('1:["$","div",null,{"children":"a \\u003c b"}]\n', '2:{"x":"\\"quoted\\""}\n')
    assert flight_text(html) == '1:["$","div",null,{"children":"a \\u003c b"}]\n2:{"x":"\\"quoted\\""}\n'


def test_flight_object_reads_a_nested_object_out_of_a_line():
    text = '3:["$","$L5",null,{"viewerSection":{"titleID":1,"pages":[{"src":"x"}]},"other":{"a":[1,2]}}]\n'
    assert flight_object(text, "viewerSection") == {"titleID": 1, "pages": [{"src": "x"}]}
    assert flight_object(text, "other") == {"a": [1, 2]}
    assert flight_object(text, "missing") is None
    assert flight_object('"broken":{"a":', "broken") is None


def test_next_data_reads_the_page_json():
    assert next_data(next_data_html({"props": {"pageProps": {}}, "isFallback": False}))["isFallback"] is False
    assert next_data("<html></html>") == {}
    assert next_data('<script id="__NEXT_DATA__" type="application/json">nope</script>') == {}


# --- the shared machinery -------------------------------------------------------------


# ==========================================================================================
# マンガワン
# ==========================================================================================

MO_CHAPTER_URL = f"{MANGAONE_URL}/manga/28024/chapter/353545"
MO_TITLE_URL = f"{MANGAONE_URL}/manga/28024"
MO_IMAGE = "https://app.manga-one.com/secure/1/webp/manga_page_low/353545/{}.webp.enc?hash=h&expires=1"

# (id, name, required points); the API lists them newest first.
MO_CHAPTERS = [(354637, "第3話", 30), (353548, "第2話", 0), (353545, "第1話", 0)]


def mo_chapter(chapter_id, name, points=0, description=""):
    fields = encode_varint_field(1, chapter_id) + encode_bytes_field(2, name)
    if description:
        fields += encode_bytes_field(3, description)
    fields += encode_bytes_field(16, encode_varint_field(1, 1) + encode_varint_field(2, points) if points else b"")
    return fields


def mo_page(url, width=720, height=1020):
    image = encode_bytes_field(1, url) + encode_varint_field(2, width) + encode_varint_field(3, height)
    return encode_bytes_field(1, encode_bytes_field(1, image))


def mo_viewer(chapter_id=353545, pages=None, next_id=353548, chapters=MO_CHAPTERS, *, key=KEY, iv=IV):
    """A `WebViewerResponse` with the fields the extractor reads."""
    if pages is None:
        pages = [mo_page(MO_IMAGE.format(1)), mo_page(MO_IMAGE.format(2))]
    body = b"".join(pages)
    # A promo slotted in after the pages, which is an `imageWithAction`, and a purchase card.
    body += encode_bytes_field(1, encode_bytes_field(2, encode_bytes_field(1, "https://app.manga-one.com/promo.webp")))
    body += encode_bytes_field(1, encode_bytes_field(3, b""))
    if key:
        body += encode_bytes_field(3, key) + encode_bytes_field(4, iv)
    title = (
        encode_varint_field(1, 28024)
        + encode_bytes_field(2, "女の子を天国に連れていくには")
        + encode_bytes_field(5, "高見奈緒")
    )
    body += encode_bytes_field(5, encode_bytes_field(1, title) + encode_varint_field(2, chapter_id))
    current = next((c for c in chapters if c[0] == chapter_id), (chapter_id, "第1話", 0))
    body += encode_bytes_field(7, mo_chapter(*current, description="だって僕の世界は平和だから"))
    if next_id:
        body += encode_bytes_field(8, mo_chapter(next_id, "next"))
    listing = b"".join(encode_bytes_field(1, mo_chapter(*c)) for c in chapters)
    listing += encode_bytes_field(2, encode_varint_field(1, 1) + encode_varint_field(2, 1))
    return body + encode_bytes_field(11, listing)


def mo_chapter_list(chapters, page=1, total_pages=1):
    listing = b"".join(encode_bytes_field(1, mo_chapter(*c)) for c in chapters)
    listing += encode_bytes_field(2, encode_varint_field(1, page) + encode_varint_field(2, total_pages))
    return encode_bytes_field(1, listing)


@pytest.fixture
def recording_session(fake_session):
    """A fake session that also keeps the query parameters of a POST."""

    class RecordingSession(fake_session):
        def __init__(self, routes):
            super().__init__(routes)
            self.post_params = []

        def post(self, url, data=None, json=None, **kwargs):
            self.post_params.append(kwargs.get("params"))
            return super().post(url, data, json, **kwargs)

    return RecordingSession


@pytest.fixture
def mangaone(recording_session, fake_response):
    def build(routes=None):
        # The chapter list (a GET with `rq` in the URL) goes before the viewer (a POST to the bare path).
        listing = {"/api/client?rq=viewer/chapter_list": fake_response(mo_chapter_list(MO_CHAPTERS[::-1]))}
        merged = {**listing, **(routes or {})}
        merged.setdefault("/api/client", fake_response(mo_viewer(), content_type="application/x-protobuf"))
        merged.setdefault("app.manga-one.com", fake_response(encrypted_png()))
        session = recording_session(merged)
        return MangaOne(session), session

    return build


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (MO_CHAPTER_URL, True),
        (MO_CHAPTER_URL + "/", True),
        (f"{MANGAONE_URL}/manga/28024/chapter/first", True),
        (MO_TITLE_URL, True),
        ("http://manga-one.com/manga/28024/chapter/353545", False),
        (f"{MANGAONE_URL}/manga/28024/choitashi/1", False),
        (f"{MANGAONE_URL}/manga/28024/volume/1", False),
        (f"{MANGAONE_URL}/titles/rensai", False),
        ("https://app.manga-one.com/manga/28024/chapter/353545", False),
    ],
)
def test_mangaone_suitable(url, expected):
    assert MangaOne.suitable(url) is expected


def test_mangaone_is_series_means_a_title_id():
    assert MangaOne.is_series(MO_TITLE_URL)
    assert not MangaOne.is_series(MO_CHAPTER_URL)
    assert not MangaOne.is_series(f"{MANGAONE_URL}/manga/28024/chapter/first")


def test_mangaone_episode_reads_the_titles_the_pages_and_the_next_chapter(mangaone):
    extractor, session = mangaone()
    episode = extractor.episode(MO_CHAPTER_URL)

    assert episode.series_title == "女の子を天国に連れていくには"
    assert episode.episode_title == "第1話"
    assert [page.url for page in episode.pages] == [MO_IMAGE.format(1), MO_IMAGE.format(2)]
    assert episode.pages[0].extra == {"key": KEY, "iv": IV}
    assert (episode.pages[0].width, episode.pages[0].height) == (720, 1020)
    assert (episode.prev_url, episode.next_url) == (None, f"{MANGAONE_URL}/manga/28024/chapter/353548")
    assert episode.metadata["free"] is True
    assert episode.metadata["description"] == "だって僕の世界は平和だから"
    assert [c["id"] for c in episode.metadata["chapters"]] == [354637, 353548, 353545]
    assert episode.metadata["chapters"][0]["free"] is False
    json.dumps(episode.metadata)

    url, _ = session.posts[0]
    assert url == f"{MANGAONE_URL}/api/client"
    assert session.post_params[0] == {"rq": "viewer_v2", "title_id": 28024, "chapter_id": 353545}


def test_mangaone_first_opens_the_title_without_a_chapter_id(mangaone):
    extractor, session = mangaone()
    episode = extractor.episode(f"{MANGAONE_URL}/manga/28024/chapter/first")

    assert episode.episode_title == "第1話"
    assert session.post_params[0] == {"rq": "viewer_v2", "title_id": 28024}


def test_mangaone_locked_chapter_has_no_pages_but_keeps_its_names(mangaone, fake_response):
    answer = mo_viewer(chapter_id=354637, pages=[], next_id=0)
    extractor, _ = mangaone({"/api/client": fake_response(answer)})
    episode = extractor.episode(f"{MANGAONE_URL}/manga/28024/chapter/354637")

    assert episode.pages == ()
    assert episode.series_title == "女の子を天国に連れていくには"
    assert episode.episode_title == "第3話"
    assert (episode.prev_url, episode.next_url) == (f"{MANGAONE_URL}/manga/28024/chapter/353548", None)
    assert episode.metadata["free"] is False


def test_mangaone_unknown_chapter_is_not_an_episode(mangaone, fake_response):
    extractor, _ = mangaone({"/api/client": fake_response(b"", status_code=HTTPStatus.NOT_FOUND)})
    with pytest.raises(NotAnEpisodePageError, match="no chapter"):
        extractor.episode(f"{MANGAONE_URL}/manga/28024/chapter/1")


def test_mangaone_episode_refuses_a_title_url(mangaone):
    extractor, _ = mangaone()
    with pytest.raises(UnsupportedUrlError, match="not a chapter"):
        extractor.episode(MO_TITLE_URL)


def test_mangaone_series_urls_walks_the_pages_oldest_first(mangaone, fake_response):
    pages = [
        fake_response(mo_chapter_list([(353545, "第1話", 0), (353548, "第2話", 0)], page=1, total_pages=2)),
        fake_response(mo_chapter_list([(353548, "第2話", 0), (354637, "第3話", 30)], page=2, total_pages=2)),
    ]
    extractor, session = mangaone({"/api/client?rq=viewer/chapter_list": pages})
    urls = extractor.series_urls(MO_TITLE_URL)

    assert urls == [f"{MANGAONE_URL}/manga/28024/chapter/{i}" for i in (353545, 353548, 354637)]
    assert session.calls[0] == f"{MANGAONE_URL}/api/client?rq=viewer/chapter_list"
    assert session.params_seen[0] == {
        "title_id": 28024,
        "type": "chapter",
        "sort_type": "asc",
        "page": 1,
        "limit": 100,
    }
    assert session.params_seen[1]["page"] == 2


def test_mangaone_series_urls_rejects_an_empty_title(mangaone, fake_response):
    extractor, _ = mangaone({"/api/client?rq=viewer/chapter_list": fake_response(mo_chapter_list([]))})
    with pytest.raises(NotAnEpisodePageError, match="lists no chapter"):
        extractor.series_urls(MO_TITLE_URL)


def test_mangaone_series_urls_rejects_a_chapter_url(mangaone):
    extractor, _ = mangaone()
    with pytest.raises(UnsupportedUrlError, match="not a title"):
        extractor.series_urls(MO_CHAPTER_URL)


def test_mangaone_image_decrypts_a_page(mangaone):
    extractor, session = mangaone()
    episode = extractor.episode(MO_CHAPTER_URL)

    image = extractor.image(episode.pages[0], episode)

    assert image.getpixel((4, 4)) == (10, 20, 30)
    assert session.headers_seen[-1]["Referer"] == MO_CHAPTER_URL


def test_image_leaves_an_unencrypted_page_alone(mangaone, fake_response):
    extractor, _ = mangaone(
        {
            "/api/client": fake_response(mo_viewer(pages=[mo_page("https://app.manga-one.com/plain.webp")], key="")),
            "app.manga-one.com": fake_response(png_bytes(colour=(1, 2, 3))),
        },
    )
    episode = extractor.episode(MO_CHAPTER_URL)
    assert episode.pages[0].extra == {"key": "", "iv": ""}
    assert extractor.image(episode.pages[0], episode).getpixel((0, 0)) == (1, 2, 3)


# ==========================================================================================
# フラコミlike!
# ==========================================================================================

FC_CHAPTER_URL = f"{FLOWERCOMICS_URL}/chapter/96932"
FC_TITLE_URL = f"{FLOWERCOMICS_URL}/title/2915"
FC_IMAGE = "https://img.flowercomics.jp/chapter_page/96932/{}.webp.enc?h=x&e=1"


def fc_row(chapter_id, title, priority, chapter_type=0):
    return {
        "id": chapter_id,
        "thumbnail": {"src": f"https://img.flowercomics.jp/chapter/{chapter_id}.webp"},
        "title": title,
        "subTitle": "",
        "chapterType": chapter_type,
        "dialog": "$undefined" if chapter_type == 0 else {"chapterId": chapter_id},
        "priority": priority,
    }


# The title page lists three runs, each newest first.
FC_CHAPTERS = {
    "earlyChapters": [fc_row(139182, "第27話", 5, chapter_type=4), fc_row(132343, "第23話", 4, chapter_type=3)],
    "omittedMiddleChapters": [fc_row(98036, "第3話", 3)],
    "latestChapters": [fc_row(96935, "第1話 -2", 2), fc_row(96932, "第1話 -1", 1)],
}


def fc_page(index, *, key=KEY, iv=IV):
    page = {"src": FC_IMAGE.format(index), "type": "image", "sizeRatio": 1.41}
    if key:
        page["crypto"] = {"method": "aes-cbc", "key": key, "iv": iv}
    return page


def fc_viewer(pages=None, next_id=96935):
    if pages is None:
        pages = [fc_page(1), fc_page(2)]
    # The viewer appends a banner page, which links somewhere and is not a manga page.
    banner = {"src": "https://img.flowercomics.jp/banner/1.webp", "type": "image", "anchor": {"url": "/register"}}
    return {
        "titleID": 2915,
        "titleName": "死神の初恋 〜没落華族の令嬢は愛を知らない死神に嫁ぐ〜",
        "currentChapterName": "第1話 -1",
        "orientation": "horizontal",
        "pages": [*pages, banner],
        "directionRightToLeft": True,
        "nextChapter": {"id": next_id, "chapterType": 0} if next_id else "$undefined",
    }


def fc_chapter_html(viewer=None):
    viewer = fc_viewer() if viewer is None else viewer
    line = json.dumps(["$", "$L2c", None, {"chapters": FC_CHAPTERS, "viewerSection": viewer}], ensure_ascii=False)
    return flight_html('0:["$","$L1",null,{}]\n', f"2c:{line}\n")


def fc_title_html(chapters=FC_CHAPTERS, heading="死神の初恋 〜没落華族の令嬢は愛を知らない死神に嫁ぐ〜"):
    line = json.dumps(["$", "$L40", None, {"section": {"chapters": chapters}}], ensure_ascii=False)
    return f'<html><body><h1 class="x">{heading}</h1>{flight_html(f"36:{line}" + chr(10))}</body></html>'


@pytest.fixture
def flower(fake_session, fake_response):
    def build(routes=None):
        merged = dict(routes or {})
        merged.setdefault("/chapter/", fake_response(text=fc_chapter_html()))
        merged.setdefault("/title/", fake_response(text=fc_title_html()))
        merged.setdefault("img.flowercomics.jp", fake_response(encrypted_png()))
        session = fake_session(merged)
        return FlowerComics(session), session

    return build


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (FC_CHAPTER_URL, True),
        (FC_CHAPTER_URL + "/", True),
        (FC_TITLE_URL, True),
        ("http://flowercomics.jp/chapter/96932", False),
        (f"{FLOWERCOMICS_URL}/rensai?day=wed", False),
        (f"{FLOWERCOMICS_URL}/title/2915/chapter/96932", False),
        ("https://img.flowercomics.jp/chapter/96932", False),
    ],
)
def test_flower_suitable(url, expected):
    assert FlowerComics.suitable(url) is expected


def test_flower_is_series_means_a_title_page():
    assert FlowerComics.is_series(FC_TITLE_URL)
    assert not FlowerComics.is_series(FC_CHAPTER_URL)


def test_flower_episode_reads_the_viewer_props(flower):
    extractor, session = flower()
    episode = extractor.episode(FC_CHAPTER_URL)

    assert episode.series_title == "死神の初恋 〜没落華族の令嬢は愛を知らない死神に嫁ぐ〜"
    assert episode.episode_title == "第1話 -1"
    assert [page.url for page in episode.pages] == [FC_IMAGE.format(1), FC_IMAGE.format(2)]
    assert episode.pages[0].extra == {"key": KEY, "iv": IV}
    assert episode.next_url == f"{FLOWERCOMICS_URL}/chapter/96935"
    assert episode.metadata == {
        "title_id": 2915,
        "chapter_id": 96932,
        "orientation": "horizontal",
        "right_to_left": True,
    }
    assert session.calls == [FC_CHAPTER_URL]


def test_flower_episode_stops_at_the_last_chapter(flower, fake_response):
    extractor, _ = flower({"/chapter/": fake_response(text=fc_chapter_html(fc_viewer(next_id=0)))})
    assert extractor.episode(FC_CHAPTER_URL).next_url is None


def test_flower_locked_chapter_is_the_title_page_it_redirects_to(flower, fake_response):
    landed = f"{FC_TITLE_URL}?redirect_type=1&chapter_id=132343"
    extractor, _ = flower({"/chapter/": fake_response(text=fc_title_html(), url=landed)})
    episode = extractor.episode(f"{FLOWERCOMICS_URL}/chapter/132343")

    assert episode.pages == ()
    assert episode.series_title == "死神の初恋 〜没落華族の令嬢は愛を知らない死神に嫁ぐ〜"
    assert episode.episode_title == "第23話"
    assert episode.prev_url is not None
    assert episode.next_url == f"{FLOWERCOMICS_URL}/chapter/139182"
    assert [c["free"] for c in episode.metadata["chapters"]] == [True, True, True, False, False]
    json.dumps(episode.metadata)


def test_flower_page_without_a_viewer_is_not_an_episode(flower, fake_response):
    extractor, _ = flower({"/chapter/": fake_response(text=flight_html('0:["$","$L1",null,{}]\n'))})
    with pytest.raises(NotAnEpisodePageError, match="no viewer"):
        extractor.episode(FC_CHAPTER_URL)


def test_flower_missing_chapter_is_not_an_episode(flower, fake_response):
    extractor, _ = flower({"/chapter/": fake_response(text="", status_code=HTTPStatus.INTERNAL_SERVER_ERROR)})
    with pytest.raises(NotAnEpisodePageError, match="HTTP 500"):
        extractor.episode(FC_CHAPTER_URL)


def test_flower_episode_refuses_a_title_url(flower):
    extractor, _ = flower()
    with pytest.raises(UnsupportedUrlError, match="not a chapter"):
        extractor.episode(FC_TITLE_URL)


def test_flower_series_urls_orders_the_three_runs_by_priority(flower):
    extractor, _ = flower()
    urls = extractor.series_urls(FC_TITLE_URL)
    assert urls == [f"{FLOWERCOMICS_URL}/chapter/{i}" for i in (96932, 96935, 98036, 132343, 139182)]


def test_flower_series_urls_rejects_an_empty_title(flower, fake_response):
    empty = {"earlyChapters": [], "omittedMiddleChapters": [], "latestChapters": []}
    extractor, _ = flower({"/title/": fake_response(text=fc_title_html(empty))})
    with pytest.raises(NotAnEpisodePageError, match="lists no chapter"):
        extractor.series_urls(FC_TITLE_URL)


def test_flower_series_urls_rejects_a_chapter_url(flower):
    extractor, _ = flower()
    with pytest.raises(UnsupportedUrlError, match="not a title"):
        extractor.series_urls(FC_CHAPTER_URL)


def test_flower_image_decrypts_a_page(flower):
    extractor, session = flower()
    episode = extractor.episode(FC_CHAPTER_URL)

    image = extractor.image(episode.pages[0], episode)

    assert image.getpixel((4, 4)) == (10, 20, 30)
    assert session.headers_seen[-1]["Referer"] == FC_CHAPTER_URL


# ==========================================================================================
# ガンガンONLINE
# ==========================================================================================

GG_CHAPTER_URL = f"{GANGANONLINE_URL}/title/2580/chapter/127641"
GG_TITLE_URL = f"{GANGANONLINE_URL}/title/2580"
GG_BUILD = "Ito7jBvYWql29rx6-REPS"
GG_IMAGE = "/secure/title/2580/chapter/127641/manga_page/high/{}.webp?hash=h&expires=1"

# The title page lists its chapters newest first.
GG_CHAPTERS = [
    {"id": 132794, "status": 3, "mainText": "次回更新：10月15日", "subText": "第5話-1 "},
    {"id": 131954, "mainText": "第4話-4", "publishingPeriod": "2026.09.17〜2026.10.14"},
    {"id": 131552, "status": 2, "mainText": "第4話-3", "appLaunchUrl": "https://ganganonline.onelink.me/x"},
    {"id": 127641, "mainText": "第1話"},
]


def gg_chapter_data(pages=None, next_id=131954):
    if pages is None:
        pages = [{"image": {"imageUrl": GG_IMAGE.format(1)}}, {"image": {"imageUrl": GG_IMAGE.format(2)}}]
    return {
        "pages": [*pages, {"linkImage": {"imageUrl": "/secure/extra_manga_page/1.webp", "url": "/title/1507"}}],
        "lastPage": {"nextChapterId": next_id, "sns": {}},
        "chapterName": "第1話",
        "ifLeftStart": True,
        "titleDetailUrl": "/title/2580",
        "titleName": "ヤンデレ化を回避したはずの天使な義弟は期待を裏切らない",
        "author": "原作／夜明星良　漫画／宮鈴りうむ",
    }


def gg_page(page_props, page="/title/[titleId]/chapter/[chapterId]", *, fallback=False):
    return next_data_html(
        {
            "props": {"pageProps": page_props, "__N_SSG": True},
            "page": page,
            "query": {},
            "buildId": GG_BUILD,
            "isFallback": fallback,
            "gsp": True,
        },
    )


def gg_title_props(chapters=GG_CHAPTERS):
    return {
        "data": {
            "default": {"chapters": chapters, "titleName": "ヤンデレ化を回避したはずの天使な義弟は期待を裏切らない"}
        },
        "error": None,
    }


@pytest.fixture
def gangan(fake_session, fake_response):
    def build(routes=None):
        merged = dict(routes or {})
        # The page images live under `/secure/title/<id>/chapter/<id>/`, so they go first.
        merged = {"/secure/": fake_response(png_bytes()), **merged}
        merged.setdefault("/chapter/", fake_response(text=gg_page({"data": gg_chapter_data()})))
        merged.setdefault("/title/", fake_response(text=gg_page(gg_title_props(), "/title/[titleId]")))
        session = fake_session(merged)
        return GanganOnline(session), session

    return build


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (GG_CHAPTER_URL, True),
        (GG_CHAPTER_URL + "/", True),
        (GG_TITLE_URL, True),
        ("http://www.ganganonline.com/title/2580/chapter/127641", False),
        ("https://ganganonline.com/title/2580/chapter/127641", False),
        (f"{GANGANONLINE_URL}/chapter/127641", False),
        (f"{GANGANONLINE_URL}/rensai", False),
    ],
)
def test_gangan_suitable(url, expected):
    assert GanganOnline.suitable(url) is expected


def test_gangan_is_series_means_a_title_page():
    assert GanganOnline.is_series(GG_TITLE_URL)
    assert not GanganOnline.is_series(GG_CHAPTER_URL)


def test_gangan_episode_reads_the_page_json(gangan):
    extractor, session = gangan()
    episode = extractor.episode(GG_CHAPTER_URL)

    assert episode.series_title == "ヤンデレ化を回避したはずの天使な義弟は期待を裏切らない"
    assert episode.episode_title == "第1話"
    assert [page.url for page in episode.pages] == [GANGANONLINE_URL + GG_IMAGE.format(i) for i in (1, 2)]
    assert episode.pages[0].extra == {}
    assert (episode.prev_url, episode.next_url) == (None, f"{GANGANONLINE_URL}/title/2580/chapter/131954")
    assert episode.metadata["author"] == "原作／夜明星良　漫画／宮鈴りうむ"
    assert episode.metadata["left_start"] is True
    # The chapter page, then the title page for the chapter before, which only that lists.
    assert session.calls == [GG_CHAPTER_URL, GG_TITLE_URL]


def test_gangan_episode_stops_at_the_last_chapter(gangan, fake_response):
    extractor, _ = gangan({"/chapter/": fake_response(text=gg_page({"data": gg_chapter_data(next_id=0)}))})
    assert extractor.episode(GG_CHAPTER_URL).next_url is None


def test_gangan_locked_chapter_is_the_title_page_it_redirects_to(gangan, fake_response):
    page = fake_response(text=gg_page(gg_title_props(), "/title/[titleId]"), url=GG_TITLE_URL)
    extractor, _ = gangan({"/chapter/": page})
    episode = extractor.episode(f"{GANGANONLINE_URL}/title/2580/chapter/131552")

    assert episode.pages == ()
    assert episode.series_title == "ヤンデレ化を回避したはずの天使な義弟は期待を裏切らない"
    assert episode.episode_title == "第4話-3"
    assert (episode.prev_url, episode.next_url) == (
        f"{GANGANONLINE_URL}/title/2580/chapter/127641",
        f"{GANGANONLINE_URL}/title/2580/chapter/131954",
    )
    assert [(c["title"], c["free"]) for c in episode.metadata["chapters"]] == [
        ("第1話", True),
        ("第4話-3", False),
        ("第4話-4", True),
        ("第5話-1", False),
    ]
    json.dumps(episode.metadata)


def test_gangan_fallback_shell_is_filled_from_the_build_json(gangan, fake_response):
    extractor, session = gangan(
        {
            "/_next/data/": fake_response(payload={"pageProps": {"data": gg_chapter_data()}, "__N_SSG": True}),
            "/chapter/": fake_response(text=gg_page({}, fallback=True)),
        },
    )
    episode = extractor.episode(GG_CHAPTER_URL)

    assert len(episode.pages) == 2
    assert session.calls[1] == f"{GANGANONLINE_URL}/_next/data/{GG_BUILD}/title/2580/chapter/127641.json"


def test_gangan_fallback_shell_that_redirects_is_locked(gangan, fake_response):
    extractor, session = gangan(
        {
            "/_next/data/": fake_response(
                payload={"pageProps": {"__N_REDIRECT": "/title/2580", "__N_REDIRECT_STATUS": 307}}
            ),
            "/chapter/": fake_response(text=gg_page({}, fallback=True)),
        },
    )
    episode = extractor.episode(f"{GANGANONLINE_URL}/title/2580/chapter/131552")

    assert episode.pages == ()
    assert episode.episode_title == "第4話-3"
    assert session.calls[-1] == GG_TITLE_URL


def test_gangan_page_without_a_chapter_is_not_an_episode(gangan, fake_response):
    extractor, _ = gangan({"/chapter/": fake_response(text=gg_page({"data": {"default": {}}}))})
    with pytest.raises(NotAnEpisodePageError, match="no viewer"):
        extractor.episode(GG_CHAPTER_URL)


def test_gangan_missing_page_is_not_an_episode(gangan, fake_response):
    extractor, _ = gangan({"/chapter/": fake_response(text="", status_code=HTTPStatus.NOT_FOUND)})
    with pytest.raises(NotAnEpisodePageError, match="HTTP 404"):
        extractor.episode(GG_CHAPTER_URL)


def test_gangan_episode_refuses_a_title_url(gangan):
    extractor, _ = gangan()
    with pytest.raises(UnsupportedUrlError, match="not a chapter"):
        extractor.episode(GG_TITLE_URL)


def test_gangan_series_urls_lists_the_title_page_oldest_first(gangan):
    extractor, _ = gangan()
    urls = extractor.series_urls(GG_TITLE_URL)
    assert urls == [f"{GANGANONLINE_URL}/title/2580/chapter/{i}" for i in (127641, 131552, 131954, 132794)]


def test_gangan_series_urls_rejects_an_empty_title(gangan, fake_response):
    extractor, _ = gangan({"/title/": fake_response(text=gg_page(gg_title_props([]), "/title/[titleId]"))})
    with pytest.raises(NotAnEpisodePageError, match="lists no chapter"):
        extractor.series_urls(GG_TITLE_URL)


def test_gangan_series_urls_rejects_a_chapter_url(gangan):
    extractor, _ = gangan()
    with pytest.raises(UnsupportedUrlError, match="not a title"):
        extractor.series_urls(GG_CHAPTER_URL)


def test_gangan_image_is_served_as_is(gangan):
    extractor, session = gangan()
    episode = extractor.episode(GG_CHAPTER_URL)

    image = extractor.image(episode.pages[0], episode)

    assert image.getpixel((4, 4)) == (10, 20, 30)
    assert session.headers_seen[-1]["Referer"] == GG_CHAPTER_URL


# ==========================================================================================
# マンガPark
# ==========================================================================================

MP_CHAPTER_URL = f"{MANGAPARK_URL}/title/33142/397003"
MP_TITLE_URL = f"{MANGAPARK_URL}/title/33142"
MP_IMAGE = "https://manga-park.com:443/static/m/8ibv/{}/2h$4piq9ws.jpg.enc?zM7lqg3usN6X"
#: Base64 of the 16 bytes the pages of the fixture are XORed with.
MP_KEY = base64.b64encode(bytes(range(16))).decode()

# (id, name, subname, free); the title page lists them oldest first.
MP_CHAPTERS = [(397003, "1", "#１①", True), (397006, "2", "#１②", True), (399418, "5", "#2②", False)]


def mp_row(chapter_id, name, subname, free):
    badge = '<img src="/assets/img/mangadetail_badge_free.svg">' if free else ""
    return f"""
        <li data-url="/chapter/{chapter_id}" data-chapter-id="{chapter_id}" data-chapter-name="{name}">
          <div class="chapter-container pointer">
            <div class="free-badge">{badge}</div>
            <div class="thumbnail"><img class="chapterThumb" src="https://manga-park.com:443/static/c/x/t/1.jpg"></div>
            <div class="info"><div class="info-body">
              <p class="chapterTitle txtColorSubjectSP" data-truncation="2">{subname}</p>
            </div></div>
          </div>
        </li>"""


def mp_title_html(chapters=MP_CHAPTERS, name="アクトジジョウ"):
    rows = "".join(mp_row(*c) for c in chapters)
    return f"""<html><body>
      <div class="row title header-sp">
        <div data-title-id="33142"></div>
        <div data-title-name="{name}"></div>
        <h1 class="txtColorSubject">{name}</h1>
      </div>
      <div class="row title"><div class="chapter"><ul>{rows}</ul></div></div>
      <div class="row viewer-end"><div class="chapter"><ul>
        <li><div class="chapter-container pointer" data-url="/chapter/1"><p class="chapterTitle">dummy</p></div></li>
      </ul></div></div>
    </body></html>"""


def mp_image(index, key=MP_KEY):
    return {"path": MP_IMAGE.format(index), "width": 0, "height": 0, "key": key}


def mp_chapter_payload(pages=None, consume_type="free"):
    if pages is None:
        pages = [mp_image(0), mp_image(1)]
    # A Minobi page may also be a piece of HTML, which has no images.
    chapter = [*({"images": [image], "width": 0, "height": 0} for image in pages), {"src": "<div>ad</div>"}]
    return {
        "csrf_token": "tok",
        "is_logged_in": False,
        "data": {"chapter": chapter, "consume_type": consume_type, "page_start": "LEFT"},
    }


def mp_masked_png(key=MP_KEY, **kwargs):
    plain, mask = png_bytes(**kwargs), base64.b64decode(key)
    return bytes(byte ^ mask[i % len(mask)] for i, byte in enumerate(plain))


MP_LOGIN_REQUIRED = {"error": {"code": 401, "message": "login required"}, "csrf_token": "tok", "is_logged_in": False}


@pytest.fixture
def park(fake_session, fake_response):
    def build(routes=None):
        merged = dict(routes or {})
        merged.setdefault("/api/chapter/", fake_response(payload=mp_chapter_payload()))
        merged.setdefault("/api/csrf_token", fake_response(payload={"csrf_token": "tok", "is_logged_in": False}))
        merged.setdefault("/title/", fake_response(text=mp_title_html()))
        merged.setdefault("/static/", fake_response(mp_masked_png()))
        session = fake_session(merged)
        return MangaPark(session), session

    return build


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (MP_CHAPTER_URL, True),
        (MP_CHAPTER_URL + "/", True),
        (MP_TITLE_URL, True),
        ("http://manga-park.com/title/33142/397003", False),
        (f"{MANGAPARK_URL}/chapter/397003", False),
        (f"{MANGAPARK_URL}/title/33142/397003/comments", False),
        (f"{MANGAPARK_URL}/all_chapters", False),
        ("https://www.manga-park.com/title/33142", False),
    ],
)
def test_park_suitable(url, expected):
    assert MangaPark.suitable(url) is expected


def test_park_is_series_means_a_title_page():
    assert MangaPark.is_series(MP_TITLE_URL)
    assert not MangaPark.is_series(MP_CHAPTER_URL)


def test_park_episode_reads_the_title_page_and_the_chapter_api(park):
    extractor, session = park()
    episode = extractor.episode(MP_CHAPTER_URL)

    assert episode.series_title == "アクトジジョウ"
    assert episode.episode_title == "#１①"
    assert [page.url for page in episode.pages] == [MP_IMAGE.format(0), MP_IMAGE.format(1)]
    assert episode.pages[0].extra == {"key": MP_KEY}
    assert episode.next_url == f"{MANGAPARK_URL}/title/33142/397006"
    assert episode.metadata["consume_type"] == "free"
    assert episode.metadata["page_start"] == "LEFT"
    assert episode.metadata["logged_in"] is False
    assert [(c["title"], c["free"]) for c in episode.metadata["chapters"]] == [
        ("#１①", True),
        ("#１②", True),
        ("#2②", False),
    ]
    json.dumps(episode.metadata)
    assert session.calls == [MP_TITLE_URL, f"{MANGAPARK_URL}/api/chapter/397003"]
    assert session.headers_seen[-1]["Referer"] == MP_CHAPTER_URL


def test_park_episode_stops_at_the_last_chapter(park):
    extractor, _ = park()
    last = extractor.episode(f"{MANGAPARK_URL}/title/33142/399418")
    assert (last.prev_url, last.next_url) == (f"{MANGAPARK_URL}/title/33142/397006", None)


def test_park_chapter_wanting_a_login_has_no_pages(park, fake_response):
    answer = fake_response(payload=MP_LOGIN_REQUIRED, status_code=HTTPStatus.UNAUTHORIZED)
    extractor, _ = park({"/api/chapter/": answer})
    episode = extractor.episode(f"{MANGAPARK_URL}/title/33142/397006")

    assert episode.pages == ()
    assert episode.series_title == "アクトジジョウ"
    assert episode.episode_title == "#１②"
    assert episode.next_url == f"{MANGAPARK_URL}/title/33142/399418"
    assert episode.metadata["consume_type"] is None
    json.dumps(episode.metadata)


def test_park_chapter_wanting_coins_has_no_pages(park, fake_response):
    answer = {"csrf_token": "tok", "is_logged_in": True, "data": {"consume_type": "point", "paid_point": 30}}
    extractor, _ = park({"/api/chapter/": fake_response(payload=answer)})
    episode = extractor.episode(MP_CHAPTER_URL)

    assert episode.pages == ()
    assert episode.metadata["consume_type"] == "point"
    assert episode.metadata["logged_in"] is True


def test_park_resumed_chapter_is_readable(park, fake_response):
    extractor, _ = park({"/api/chapter/": fake_response(payload=mp_chapter_payload(consume_type="resume"))})
    assert len(extractor.episode(MP_CHAPTER_URL).pages) == 2


def test_park_chapter_the_title_does_not_list_is_not_an_episode(park):
    extractor, _ = park()
    with pytest.raises(NotAnEpisodePageError, match="lists no chapter 1"):
        extractor.episode(f"{MANGAPARK_URL}/title/33142/1")


def test_park_missing_title_is_not_an_episode(park, fake_response):
    extractor, _ = park({"/title/": fake_response(text="", status_code=HTTPStatus.NOT_FOUND)})
    with pytest.raises(NotAnEpisodePageError, match="HTTP 404"):
        extractor.episode(MP_CHAPTER_URL)


def test_park_chapter_api_error_is_not_an_episode(park, fake_response):
    extractor, _ = park({"/api/chapter/": fake_response(text="", status_code=HTTPStatus.NOT_FOUND)})
    with pytest.raises(NotAnEpisodePageError, match="HTTP 404"):
        extractor.episode(MP_CHAPTER_URL)


def test_park_episode_refuses_a_title_url(park):
    extractor, _ = park()
    with pytest.raises(UnsupportedUrlError, match="not a chapter"):
        extractor.episode(MP_TITLE_URL)


def test_park_series_urls_lists_the_title_page_in_order(park, fake_response):
    doubled = [*MP_CHAPTERS, MP_CHAPTERS[0]]
    extractor, session = park({"/title/": fake_response(text=mp_title_html(doubled))})
    urls = extractor.series_urls(MP_TITLE_URL)

    assert urls == [f"{MANGAPARK_URL}/title/33142/{i}" for i in (397003, 397006, 399418)]
    assert session.calls == [MP_TITLE_URL]


def test_park_series_urls_rejects_an_empty_title(park, fake_response):
    extractor, _ = park({"/title/": fake_response(text=mp_title_html([]))})
    with pytest.raises(NotAnEpisodePageError, match="lists no chapter"):
        extractor.series_urls(MP_TITLE_URL)


def test_park_series_urls_rejects_a_chapter_url(park):
    extractor, _ = park()
    with pytest.raises(UnsupportedUrlError, match="not a title"):
        extractor.series_urls(MP_CHAPTER_URL)


def test_park_image_undoes_the_xor(park):
    extractor, session = park()
    episode = extractor.episode(MP_CHAPTER_URL)
    assert extractor.image(episode.pages[0], episode).getpixel((4, 4)) == (10, 20, 30)
    assert session.headers_seen[-1]["Referer"] == MP_CHAPTER_URL


def test_park_image_leaves_a_page_without_a_key_alone(park, fake_response):
    extractor, _ = park(
        {
            "/api/chapter/": fake_response(payload=mp_chapter_payload([mp_image(0, key="")])),
            "/static/": fake_response(png_bytes(colour=(1, 2, 3))),
        },
    )
    episode = extractor.episode(MP_CHAPTER_URL)
    assert episode.pages[0].extra == {"key": ""}
    assert extractor.image(episode.pages[0], episode).getpixel((0, 0)) == (1, 2, 3)


def test_park_login_posts_the_form_with_the_api_token(park, fake_response):
    statuses = [
        fake_response(payload={"csrf_token": "tok", "is_logged_in": False}),
        fake_response(payload={"csrf_token": "tok2", "is_logged_in": True}),
    ]
    extractor, session = park({"/api/csrf_token": statuses, "/login": fake_response(text="<html></html>")})
    extractor.login(MP_TITLE_URL, "me@example.com", "secret")

    assert session.posts == [
        (f"{MANGAPARK_URL}/login", {"csrf_token": "tok", "address": "me@example.com", "password": "secret"})
    ]
    assert session.calls.count(f"{MANGAPARK_URL}/api/csrf_token") == 2


def test_park_login_raises_when_the_session_stays_signed_out(park, fake_response):
    extractor, _ = park(
        {"/login": fake_response(text='<p class="error">※パスワードまたはメールアドレスに誤りがあります</p>')}
    )
    with pytest.raises(LoginError, match="refused the credentials"):
        extractor.login(MP_TITLE_URL, "me@example.com", "wrong")


# ==========================================================================================
# マンガラボ!
# ==========================================================================================

ML_CHAPTER_URL = f"{MANGALAB_URL}/title/viewer/809956"
ML_TITLE_URL = f"{MANGALAB_URL}/title/105830"
ML_IMAGE = "https://manga-lab.net:443/static/lab_chapter_review/163273/{}/1h$yNPppA0.jpg?G66yw0azN4a"

# (id, name, number); the API lists them newest first.
ML_CHAPTERS = [(819577, "", 4.0), (810463, "第3話", 3.0), (809956, "", 2.0), (805133, "", 1.0)]


def double_field(number, value):
    return bytes([(number << 3) | 1]) + struct.pack("<d", value)


def ml_title_row(chapter_id, name, number):
    return (
        encode_varint_field(1, chapter_id)
        + encode_bytes_field(2, name)
        + encode_varint_field(7, 5)
        + double_field(8, number)
    )


def ml_title_answer(chapters=ML_CHAPTERS, name="ノンデリ男と童貞くん。"):
    title = encode_varint_field(1, 105830) + encode_bytes_field(2, name) + encode_bytes_field(3, "西野ぺんぎん")
    return encode_bytes_field(1, title) + b"".join(encode_bytes_field(2, ml_title_row(*c)) for c in chapters)


def ml_chapter_answer(chapter_id=809956, name="", number=2.0, pages=None, *, blank=False, title_id=105830):
    if pages is None:
        pages = [ML_IMAGE.format(0), ML_IMAGE.format(1)]
    chapter = encode_varint_field(1, chapter_id) + encode_bytes_field(2, name) + double_field(3, number)
    chapter += b"".join(encode_bytes_field(4, page) for page in pages)
    chapter += encode_varint_field(5, int(blank))
    author = encode_varint_field(1, 39772811) + encode_bytes_field(2, "西野ぺんぎん")
    # The site's own `nextChapterId` (3) points the other way, at the older chapter; it is ignored.
    return (
        encode_bytes_field(1, "ノンデリ男と童貞くん。")
        + encode_bytes_field(2, chapter)
        + encode_varint_field(3, 805133)
        + encode_bytes_field(4, author)
        + encode_varint_field(7, title_id)
    )


@pytest.fixture
def lab(fake_session, fake_response):
    def build(routes=None):
        merged = dict(routes or {})
        merged.setdefault(
            "/api/title/chapter/", fake_response(ml_chapter_answer(), content_type="application/protobuf")
        )
        merged.setdefault("/api/title/", fake_response(ml_title_answer(), content_type="application/protobuf"))
        merged.setdefault("/static/", fake_response(png_bytes()))
        session = fake_session(merged)
        return MangaLab(session), session

    return build


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (ML_CHAPTER_URL, True),
        (ML_CHAPTER_URL + "/", True),
        (ML_TITLE_URL, True),
        ("http://manga-lab.net/title/viewer/809956", False),
        (f"{MANGALAB_URL}/title/ranking", False),
        (f"{MANGALAB_URL}/title/new_arrival", False),
        (f"{MANGALAB_URL}/mypage/title/105830", False),
        ("https://info.manga-lab.net/title/105830", False),
    ],
)
def test_lab_suitable(url, expected):
    assert MangaLab.suitable(url) is expected


def test_lab_is_series_means_a_title_page():
    assert MangaLab.is_series(ML_TITLE_URL)
    assert not MangaLab.is_series(ML_CHAPTER_URL)


def test_lab_episode_reads_the_chapter_and_walks_the_title_list_upwards(lab):
    extractor, session = lab()
    episode = extractor.episode(ML_CHAPTER_URL)

    assert episode.series_title == "ノンデリ男と童貞くん。"
    assert episode.episode_title == "2"
    assert [page.url for page in episode.pages] == [ML_IMAGE.format(0), ML_IMAGE.format(1)]
    assert episode.pages[0].extra == {}
    assert episode.next_url == f"{MANGALAB_URL}/title/viewer/810463"
    assert episode.metadata["title_id"] == 105830
    assert episode.metadata["number"] == 2.0
    assert episode.metadata["author"] == "西野ぺんぎん"
    assert episode.metadata["begin_with_blank_page"] is False
    assert [c["title"] for c in episode.metadata["chapters"]] == ["1", "2", "第3話", "4"]
    json.dumps(episode.metadata)
    assert session.calls == [f"{MANGALAB_URL}/api/title/chapter/809956/", f"{MANGALAB_URL}/api/title/105830/"]


def test_lab_episode_prefers_the_chapter_name(lab, fake_response):
    answer = ml_chapter_answer(chapter_id=810463, name="第3話", number=3.0, blank=True)
    extractor, _ = lab({"/api/title/chapter/": fake_response(answer)})
    episode = extractor.episode(f"{MANGALAB_URL}/title/viewer/810463")
    assert episode.episode_title == "第3話"
    assert episode.metadata["begin_with_blank_page"] is True
    assert (episode.prev_url, episode.next_url) == (ML_CHAPTER_URL, f"{MANGALAB_URL}/title/viewer/819577")


def test_lab_episode_stops_at_the_highest_number(lab, fake_response):
    extractor, _ = lab({"/api/title/chapter/": fake_response(ml_chapter_answer(chapter_id=819577, number=4.0))})
    assert extractor.episode(f"{MANGALAB_URL}/title/viewer/819577").next_url is None


def test_lab_episode_names_an_unnumbered_chapter_by_its_id(lab, fake_response):
    extractor, _ = lab({"/api/title/chapter/": fake_response(ml_chapter_answer(chapter_id=7, number=0.0, title_id=0))})
    episode = extractor.episode(f"{MANGALAB_URL}/title/viewer/7")
    assert episode.episode_title == "7"
    assert episode.next_url is None
    assert episode.metadata["chapters"] == []


def test_lab_chapter_without_pages_has_none(lab, fake_response):
    extractor, _ = lab({"/api/title/chapter/": fake_response(ml_chapter_answer(pages=[]))})
    assert extractor.episode(ML_CHAPTER_URL).pages == ()


def test_lab_missing_chapter_is_not_an_episode(lab, fake_response):
    extractor, _ = lab(
        {"/api/title/chapter/": fake_response(text="404 page not found", status_code=HTTPStatus.NOT_FOUND)}
    )
    with pytest.raises(NotAnEpisodePageError, match="HTTP 404"):
        extractor.episode(ML_CHAPTER_URL)


def test_lab_episode_refuses_a_title_url(lab):
    extractor, _ = lab()
    with pytest.raises(UnsupportedUrlError, match="not a chapter"):
        extractor.episode(ML_TITLE_URL)


def test_lab_series_urls_sorts_the_chapters_by_number(lab):
    extractor, _ = lab()
    urls = extractor.series_urls(ML_TITLE_URL)
    assert urls == [f"{MANGALAB_URL}/title/viewer/{i}" for i in (805133, 809956, 810463, 819577)]


def test_lab_series_urls_keeps_the_oldest_first_among_equal_numbers(lab, fake_response):
    same = [(30, "", 1.0), (20, "", 1.0), (10, "", 1.0)]
    extractor, _ = lab({"/api/title/": fake_response(ml_title_answer(same))})
    assert extractor.series_urls(ML_TITLE_URL) == [f"{MANGALAB_URL}/title/viewer/{i}" for i in (10, 20, 30)]


def test_lab_series_urls_rejects_an_empty_title(lab, fake_response):
    extractor, _ = lab({"/api/title/": fake_response(ml_title_answer([]))})
    with pytest.raises(NotAnEpisodePageError, match="lists no chapter"):
        extractor.series_urls(ML_TITLE_URL)


def test_lab_series_urls_rejects_a_missing_title(lab, fake_response):
    extractor, _ = lab({"/api/title/": fake_response(text="404 page not found", status_code=HTTPStatus.NOT_FOUND)})
    with pytest.raises(NotAnEpisodePageError, match="HTTP 404"):
        extractor.series_urls(ML_TITLE_URL)


def test_lab_series_urls_rejects_a_chapter_url(lab):
    extractor, _ = lab()
    with pytest.raises(UnsupportedUrlError, match="not a title"):
        extractor.series_urls(ML_CHAPTER_URL)


def test_lab_image_is_served_as_is(lab):
    extractor, session = lab()
    episode = extractor.episode(ML_CHAPTER_URL)

    image = extractor.image(episode.pages[0], episode)

    assert image.getpixel((4, 4)) == (10, 20, 30)
    assert session.headers_seen[-1]["Referer"] == ML_CHAPTER_URL


# ==========================================================================================
# The real sites
# ==========================================================================================

# One free chapter per host: the first chapter of a long-running series.
TEST_URLS: dict[str, str] = {
    "manga-one.com": "https://manga-one.com/manga/939/chapter/93060",
    "flowercomics.jp": "https://flowercomics.jp/chapter/10047",
    "www.ganganonline.com": "https://www.ganganonline.com/title/35/chapter/259",
    "manga-park.com": "https://manga-park.com/title/22/1570",
    "manga-lab.net": "https://manga-lab.net/title/viewer/805133",
}

EXTRACTORS = {
    "manga-one.com": MangaOne,
    "flowercomics.jp": FlowerComics,
    "www.ganganonline.com": GanganOnline,
    "manga-park.com": MangaPark,
    "manga-lab.net": MangaLab,
}


@pytest.mark.network
@pytest.mark.parametrize("host", TEST_URLS)
def test_site_download(tmp_path, host):
    result = Downloader(EXTRACTORS[host](), tmp_path, only_first=True).download(TEST_URLS[host])
    assert result.status == "saved"
    assert (result.save_dir / "0.jpg").stat().st_size > 0


@pytest.mark.network
@pytest.mark.parametrize(
    ("extractor", "url", "chapter"),
    [
        (MangaOne, "https://manga-one.com/manga/939", TEST_URLS["manga-one.com"]),
        (FlowerComics, "https://flowercomics.jp/title/354", TEST_URLS["flowercomics.jp"]),
        (GanganOnline, "https://www.ganganonline.com/title/35", TEST_URLS["www.ganganonline.com"]),
        (MangaPark, "https://manga-park.com/title/22", TEST_URLS["manga-park.com"]),
        (MangaLab, "https://manga-lab.net/title/105830", TEST_URLS["manga-lab.net"]),
    ],
)
def test_title_page_lists_chapters(extractor, url, chapter):
    urls = extractor().series_urls(url)
    assert chapter in urls
    assert all(extractor.suitable(u) for u in urls)


@pytest.mark.network
def test_park_chapter_wanting_a_login_is_skipped_not_raised():
    episode = MangaPark().episode("https://manga-park.com/title/22/11823")
    assert episode.pages == ()
    assert episode.episode_title == "第10話 雨やどり②"
    assert episode.next_url == "https://manga-park.com/title/22/11826"
