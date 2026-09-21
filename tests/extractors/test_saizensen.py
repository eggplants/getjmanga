from __future__ import annotations

from http import HTTPStatus
from io import BytesIO

import pytest
from httpx import HTTPStatusError
from PIL import Image

from getjmanga.downloader import Downloader
from getjmanga.errors import NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractors.saizensen import (
    Saizensen,
    group_strips,
    parse_twi4_index,
    reader_credits,
    split_reader_title,
    stitch,
)

HOST = "https://sai-zen-sen.jp"
TWI4_WORK_URL = f"{HOST}/comics/twi4/tsuredure/"
TWI4_EPISODE_URL = f"{HOST}/comics/twi4/tsuredure/0009.html"
TWI4_CLOSED_URL = f"{HOST}/comics/twi4/tsuredure/0500.html"
READER_WORK_URL = f"{HOST}/comics/karanokyoukai/"
READER_EPISODE_URL = f"{HOST}/works/comics/karanokyoukai/03/01.html"
LEGACY_EPISODE_URL = f"{HOST}/works/comics/seishunrikon/04/01.html"
FOURPAGES_WORK_URL = f"{HOST}/special/4pages-comics/marine-yumi/"
FOURPAGES_EPISODE_URL = f"{HOST}/special/4pages-comics/marine-yumi/01.html"

# The parts of a ツイ4 strip page `episode()` reads: the title, the strip and
# the back numbers, newest first, as the site writes them.
TWI4_HTML = """
<html><head><title>告白（８） -『徒然チルドレン』若林稔弥 | ツイ４ | 最前線</title></head><body>
<header><h1><a href="/comics/twi4/tsuredure/"><strong>
<img alt="若林稔弥『徒然チルドレン』" src="/comics/twi4/tsuredure/res/images/title.png" /></strong></a>
<small><span class="number">#0009</span></small></h1></header>
<section id="comics"><div class="section-body">
<article class="comic" id="comic-0009">
<header><div class="hgroup"><h3><span class="number">9</span><span class="delimiter">/</span>
<span class="total">1030</span></h3></div></header>
<div class="section-body"><div class="pgroup">
<p><img alt="告白（８）" src="/comics/twi4/tsuredure/works/0009.fUvE7wwnjls9mY5u1rpLuv4sVHverpUP.jpg" /></p>
</div></div>
</article>
</div>
<nav id="backnumbers"><div class="section-body"><ul>
<li><a href="https://sai-zen-sen.jp/comics/twi4/tsuredure/0012.html">若林稔弥『不真面目な彼女（１）』 #0012</a></li>
<li><a href="https://sai-zen-sen.jp/comics/twi4/tsuredure/0011.html">若林稔弥『不真面目な彼女（扉）』 #0011</a></li>
<li><a href="https://sai-zen-sen.jp/comics/twi4/tsuredure/0010.html">若林稔弥『告白（９）』 #0010</a></li>
<li><a href="https://sai-zen-sen.jp/comics/twi4/tsuredure/0009.html">若林稔弥『告白（８）』 #0009</a></li>
<li><a href="https://sai-zen-sen.jp/comics/twi4/tsuredure/0008.html">若林稔弥『告白（７）』 #0008</a></li>
</ul></div></nav>
</section>
<aside id="more"><ul><li class="work"><a href="/comics/twi4/kanako/">
<strong class="work-title">幸せカナコの殺し屋生活</strong></a></li></ul></aside>
</body></html>
"""

# A strip the work has closed: the notice stands where the image was. The
# `<title>` of this one lacks the strip part, as some pages do.
TWI4_CLOSED_HTML = """
<html><head><title>『徒然チルドレン』若林稔弥 | ツイ４ | 最前線</title></head><body>
<section id="comics"><div class="section-body">
<article class="comic" id="comic-0500">
<header><div class="hgroup"><h3><span class="number">500</span><span class="delimiter">/</span>
<span class="total">1030</span></h3></div></header>
<div class="section-body"><div class="pgroup">
<p><em>『<strong><a href="/comics/twi4/tsuredure/">徒然チルドレン</a>
</strong>』は、<strong>第9回まで</strong>と<strong>最新2回</strong>を公開中！</em></p>
<p><small>（第500話は公開を終了しました）</small></p>
</div></div>
</article>
</div>
<nav id="backnumbers"><div class="section-body"><ul>
<li><a href="https://sai-zen-sen.jp/comics/twi4/tsuredure/0502.html">若林稔弥『おまけ（２）』 #0502</a></li>
<li><a href="https://sai-zen-sen.jp/comics/twi4/tsuredure/0501.html">若林稔弥『おまけ（１）』 #0501</a></li>
<li><a href="https://sai-zen-sen.jp/comics/twi4/tsuredure/0500.html">若林稔弥『おまけ』 #0500</a></li>
</ul></div></nav>
</section>
</body></html>
"""

# The `-all` page of a 座談会 entry: every strip of the entry, and the editors'
# comments with their icons, which are not pages.
ZADANKAI_ALL_HTML = """
<html><head><title>あまつばら -『第135回 ツイ4新人賞座談会』 | ツイ４ | 最前線</title></head><body>
<section id="comics"><div class="section-body">
<article class="comic" id="comic-0001"><div class="section-body"><div class="pgroup">
<p><img alt="あまつばら" src="/comics/twi4/zadankai-202608/works/0001-0001.jpg" /></p></div>
<section class="comments"><div class="section-body"><div class="pgroup">
<p><em class="comment-person"><img alt="" src="/comics/twi4/res/images/icon-editor-maeda.jpg">前田</em>
<span class="comment-body">節分の時期を狙い撃ちのマンガですね。</span></p>
</div></div></section>
</div></article>
<article class="comic" id="comic-0001"><div class="section-body"><div class="pgroup">
<p><img alt="あまつばら" src="/comics/twi4/zadankai-202608/works/0001-0002.jpg" /></p></div></div></article>
</div>
<nav id="backnumbers"><div class="section-body"><ul>
<li><a href="https://sai-zen-sen.jp/comics/twi4/zadankai-202608/c002.html">『おわりに』 #c002</a></li>
<li><a href="https://sai-zen-sen.jp/comics/twi4/zadankai-202608/0002.html">『憑いてるね！』 #0002</a></li>
<li><a href="https://sai-zen-sen.jp/comics/twi4/zadankai-202608/0001.html">『あまつばら』 #0001</a></li>
<li><a href="https://sai-zen-sen.jp/comics/twi4/zadankai-202608/c001.html">『はじめに』 #c001</a></li>
</ul></div></nav>
</section>
</body></html>
"""

# A ツイ4 work's `index.js`: strips 1 to 3 and 12 open, the rest closed, one
# comment page in between that counts for nothing.
TWI4_INDEX_JS = """
t4.Meta = { Status: "休載", Title: "徒然チルドレン", Author: "若林稔弥", ID: "tsuredure", TotalEpisodes: 12,
PublishingRange: { StartSide: 3, EndSide: 1 }, Items: [{ Title: "告白（タイトル）", Format: "r", Suffix: ".3Ps" },
{ Title: "告白（１）", Format: "r", Suffix: ".V9z" }, { Title: "告白（２）", Format:"x", Suffix: ".qQ4" },
{ Format:"c", Title: "はじめに" },
{ Format: "r0" }, { Format: "r0" }, { Format: "x0" }, { Format: "r0" }, { Format: "r0" }, { Format: "r0" },
{ Format: "r0" }, { Format: "r0" },
{ Title: "テスト（１）", Format: "r", Suffix: ".BFt" }] };
"""

# A ツイ4 work page: the back numbers, newest first, plus links to other works.
TWI4_WORK_HTML = """
<html><head><title>『徒然チルドレン』若林稔弥 | ツイ４ | 最前線</title></head><body>
<section id="comics"><nav id="backnumbers"><div class="section-body"><ul>
<li><a href="https://sai-zen-sen.jp/comics/twi4/tsuredure/0003.html">若林稔弥『告白（２）』 #0003</a></li>
<li><a href="https://sai-zen-sen.jp/comics/twi4/tsuredure/0002.html">若林稔弥『告白（１）』 #0002</a></li>
<li><a href="https://sai-zen-sen.jp/comics/twi4/tsuredure/0002.html#again">若林稔弥『告白（１）』 #0002</a></li>
<li><a href="https://sai-zen-sen.jp/comics/twi4/tsuredure/0001.html">若林稔弥『告白（タイトル）』 #0001</a></li>
</ul></div></nav></section>
<aside id="more"><ul>
<li class="work"><a href="/comics/twi4/kanako/0001.html">
<strong class="work-title">幸せカナコの殺し屋生活</strong></a></li>
</ul></aside>
</body></html>
"""

# A reader volume: one `div.item > noscript > img` per page, spreads of two.
READER_TITLE = (
    "天空すふぃあ『空の境界 the Garden of sinners』１／俯瞰風景 第三回 "
    "原作／奈須きのこ キャラクターデザイン原案／武内 崇 | 最前線"
)
READER_HTML = f"""
<html><head><title>{READER_TITLE}</title>
<script src="/reader/res/bib/i/res/scripts/bibi.js"></script></head>
<body data-bibi-book="works/comics/karanokyoukai/03" data-szsr-mode="comics">
<main role="main"><article class="book">
<header><div class="hgroup"><h1>{READER_TITLE}</h1>
</div></header>
<div class="spread-box"><div class="spread">
<div class="item-box"><div class="item" id="item-001"><noscript>
<img id="p001" alt="" src="/works/comics/karanokyoukai/03/01.res/001.png" /></noscript></div></div>
</div></div>
<div class="spread-box"><div class="spread">
<div class="item-box"><div class="item" id="item-002"><noscript>
<img id="p002" alt="" src="/works/comics/karanokyoukai/03/01.res/002.png" /></noscript></div></div>
<div class="item-box"><div class="item" id="item-003"><noscript>
<img id="p003" alt="" src="/works/comics/karanokyoukai/03/01.res/003.png" /></noscript></div></div>
</div></div>
<div class="spread-box extra-spread-box"><div class="spread"><div class="item-box"><div class="item" id="item-004">
<div class="extra" id="p004"><nav><ul class="relatives">
<li><a href="/comics/karanokyoukai/">作品紹介に戻る</a></li></ul>
</nav></div>
</div></div></div></div>
</article></main></body></html>
"""

# A legacy reader volume: each page cut into strips named `<page>.<strip>.jpg`,
# a `continue.png` and a banner in the same `p.image` elements.
LEGACY_HTML = """
<html><head><title>HERO（OOZ Inc.）『青春離婚』第4回 原作／紅玉いづき | 最前線</title></head>
<body data-bibi-book="works/comics/seishunrikon/04" data-szsr-mode="text legacy">
<div class="book novel"><article id="book-body">
<header class="book-page-spread book-introduction">
<figure><img alt="" src="/works/comics/seishunrikon/04/00.res/ci.jpg" /></figure>
<div class="hgroup"><h1 class="book-title">青春離婚</h1><h2 class="book-volume-title">第4回</h2></div>
</header>
<section class="book-page-spread"><div class="pgroup">
<p class="image"><img alt="" src="/works/comics/seishunrikon/04/01.res/01.01.jpg"></p>
<p class="image"><img alt="" src="/works/comics/seishunrikon/04/01.res/01.02.jpg"></p>
<p class="image"><img alt="" src="/works/comics/seishunrikon/04/01.res/02.01.jpg"></p>
<p class="image"><img alt="" src="/works/comics/seishunrikon/04/01.res/02.02.jpg"></p>
<p class="image"><img alt="" src="/works/comics/seishunrikon/00/00.res/continue.png"></p>
</div></section>
<p class="image"><a href="https://www.amazon.co.jp/">
<img alt="" src="/works/comics/seishunrikon/00/00.res/banner.png"></a></p>
</article></div></body></html>
"""

# A 4ページマンガ最前線 volume: the work's cover, then the four pages.
FOURPAGES_HTML = """
<html><head><title>『まりんこゆみ / Marine Corps Yumi』第1回著者：野上武志 原案：モレノ | 最前線</title></head>
<body data-bibi-book="special/4pages-comics/marine-yumi/01">
<main role="main"><article class="book">
<header><div class="hgroup">
<h1>『まりんこゆみ / Marine Corps Yumi』第1回 著者：野上武志 原案：アナステーシア・モレノ | 最前線</h1>
</div></header>
<div class="spread-box"><div class="spread"><div class="item-box">
<div class="item" id="item-000"><noscript>
<img id="p000" class="book-page-image" alt="" src="/special/4pages-comics/marine-yumi/cover.png" /></noscript></div>
</div></div></div>
<div class="spread-box"><div class="spread">
<div class="item-box"><div class="item" id="item-001"><noscript>
<img id="p001" class="book-page-image" alt="" src="/special/4pages-comics/works/content/marine-yumi/high/01_01.png" />
</noscript></div></div>
<div class="item-box"><div class="item" id="item-002"><noscript>
<img id="p002" class="book-page-image" alt="" src="/special/4pages-comics/works/content/marine-yumi/high/01_02.png" />
</noscript></div></div>
</div></div>
</article></main></body></html>
"""

# A reader work page: the latest volume linked from the header, then the back
# numbers newest first, the expired ones without a link.
READER_WORK_HTML = """
<html><head><title>空の境界 the Garden of sinners | 最前線 - フィクション・コミック・Webエンターテイメント</title>
</head><body>
<nav class="work_action-nav"><ul>
<li class="odd"><a href="/works/comics/karanokyoukai/73/01.html">
<img alt="最新作を読む" src="/res/img/common/buttons/read.latest.png"></a></li>
<li class="even"><a href="/works/comics/karanokyoukai/01/01.html">
<img alt="第一回を読む" src="/res/img/common/buttons/read.first.png"></a></li>
</ul></nav>
<section class="work_new-episode">
<h3><a href="/works/comics/karanokyoukai/73/01.html">６／「忘却録音」第十七回</a></h3></section>
<section class="work_introduction work_back-numbers" id="back-numbers"><nav><ul>
<li><strong class="attention attention_now">掲載中</strong>
<a href="/works/comics/karanokyoukai/73/01.html">６／「忘却録音」第十七回</a></li>
<li><strong class="attention attention_now">掲載中</strong>
<a href="/works/comics/karanokyoukai/72/01.html">６／「忘却録音」第十六回</a></li>
<li><strong class="attention attention_expired">掲載終了</strong>
<span class="disable">６／「忘却録音」第十五回</span></li>
<li><strong class="attention attention_now">掲載中</strong>
<a href="/works/comics/karanokyoukai/02/01.html">１／俯瞰風景 第二回</a></li>
<li><strong class="attention attention_now">掲載中</strong>
<a href="/works/comics/karanokyoukai/01/01.html">１／俯瞰風景 第一回</a></li>
</ul></nav></section>
<p><a href="/works/comics/tsukinosango/01/01.html">another work's volume</a></p>
</body></html>
"""

# A 4ページマンガ最前線 work page: the latest volume in the header, the back numbers below.
FOURPAGES_WORK_HTML = """
<html><body>
<nav class="work_action-nav"><ul>
<li><a href="https://sai-zen-sen.jp/special/4pages-comics/marine-yumi/03.html" target="_blank">最新作を読む</a></li>
<li><a href="/special/4pages-comics/marine-yumi/01.html" target="_blank">第一回を読む</a></li>
</ul><ul class="work-english-nav"><li>
<a href="/special/4pages-comics/marine-yumi/01.html#en">English version (vol.1)</a></li></ul></nav>
<div id="work-back-numbers">
<div class="work-back-number"><a href="https://sai-zen-sen.jp/special/4pages-comics/marine-yumi/02.html">第2回</a></div>
<div class="work-back-number"><a href="https://sai-zen-sen.jp/special/4pages-comics/marine-yumi/01.html">第1回</a></div>
</div>
</body></html>
"""

# `/comics/<work>/meta.json`: volumes 1, 2, 3, 72 and 73 served.
READER_META = {
    "sai-zen-sen": {"uri": "/comics/karanokyoukai/", "index": "111" + "0" * 68 + "11"},
    "amazon": {"uri": "https://www.amazon.co.jp/o/ASIN/4063695018/seikaisha-22"},
}

# What the site serves for a URL of the right shape that is no page at all.
NOT_FOUND_HTML = "<html><head><title>404 Not Found</title></head><body><h1>Not Found</h1></body></html>"


def _png(color, size=(6, 8)):
    raw = BytesIO()
    Image.new("RGB", size, color).save(raw, "PNG")
    return raw.getvalue()


@pytest.fixture
def client(fake_session):
    def make(routes):
        session = fake_session(routes)
        return Saizensen(session), session

    return make


# --- URLs ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        TWI4_EPISODE_URL,
        f"{HOST}/comics/twi4/zadankai-202608/0001-all.html",
        TWI4_WORK_URL,
        f"{HOST}/comics/twi4/tsuredure",
        READER_EPISODE_URL,
        READER_WORK_URL,
        f"{HOST}/comics/karanokyoukai",
        FOURPAGES_EPISODE_URL,
        f"{HOST}/special/4pages-comics/marine-yumi/193.html",
        FOURPAGES_WORK_URL,
    ],
)
def test_suitable_accepts_episode_and_work_urls(url):
    assert Saizensen.suitable(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://sai-zen-sen.jp/comics/twi4/tsuredure/0009.html",
        f"{HOST}/",
        f"{HOST}/comics/",
        f"{HOST}/comics/twi4/",
        f"{HOST}/comics/twi4/special/",
        f"{HOST}/comics/twi4/special/fgoantholostar/",
        f"{HOST}/comics/twi4/zadankai.html",
        f"{HOST}/comics/twi4/zadankai-202608/c001.html",
        f"{HOST}/comics/twi4/tsuredure/works/0001.3Ps1zVJl8qaWM23PD99hrsHn0PItnV4A.jpg",
        f"{HOST}/works/comics/karanokyoukai/73/",
        f"{HOST}/works/comics/karanokyoukai/73/02.html",
        f"{HOST}/special/4pages-comics/",
        f"{HOST}/fictions/",
        "https://www.splush.jp/series/14716/",
    ],
)
def test_suitable_rejects_other_urls(url):
    assert not Saizensen.suitable(url)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (TWI4_WORK_URL, True),
        (READER_WORK_URL, True),
        (FOURPAGES_WORK_URL, True),
        (TWI4_EPISODE_URL, False),
        (READER_EPISODE_URL, False),
        (FOURPAGES_EPISODE_URL, False),
        (f"{HOST}/comics/twi4/", False),
    ],
)
def test_is_series_tells_a_work_page_by_its_url(url, expected):
    assert Saizensen.is_series(url) is expected


# --- parsing --------------------------------------------------------------------------


def test_parse_twi4_index_flags_the_open_strips():
    assert parse_twi4_index(TWI4_INDEX_JS) == [True, True, True] + [False] * 8 + [True]
    assert parse_twi4_index("t4.Index = [{ Title: 'x' }]") is None


@pytest.mark.parametrize(
    ("heading", "expected"),
    [
        (
            (
                "天空すふぃあ『空の境界 the Garden of sinners』６／忘却録音 第十七回 原作／奈須きのこ "
                "キャラクターデザイン原案／武内 崇 | 最前線"
            ),
            ("空の境界 the Garden of sinners", "６／忘却録音 第十七回"),
        ),
        (
            "シオミヤイルカ『非実在推理少女あ〜や』第一話「コンダラ殺人事件」第三回 原作／錦メガネ | 最前線",
            ("非実在推理少女あ〜や", "第一話「コンダラ殺人事件」第三回"),
        ),
        ("大岩賢次『エレGY』第16回 原作／泉 和良 | 最前線", ("エレGY", "第16回")),
        ("佐々木少年『月の珊瑚』 原作／奈須きのこ | 最前線", ("月の珊瑚", "")),
        ("『まりんこゆみ』第193回 著者：野上武志 原案：アナステーシア・モレノ | 最前線", ("まりんこゆみ", "第193回")),
        ("no brackets at all", ("", "")),
    ],
)
def test_split_reader_title_drops_the_author_and_the_credits(heading, expected):
    assert split_reader_title(heading) == expected


@pytest.mark.parametrize(
    ("heading", "expected"),
    [
        (READER_TITLE, "天空すふぃあ, 奈須きのこ (原作), 武内 崇 (キャラクターデザイン原案)"),
        (
            "『まりんこゆみ』第193回 著者：野上武志 原案：アナステーシア・モレノ | 最前線",
            "野上武志 (著者), アナステーシア・モレノ (原案)",
        ),
        ("佐々木少年『月の珊瑚』 原作／奈須きのこ | 最前線", "佐々木少年, 奈須きのこ (原作)"),
        ("no brackets at all", ""),
    ],
)
def test_reader_credits_names_the_author_and_everyone_credited(heading, expected):
    assert reader_credits(heading) == expected


def test_group_strips_keeps_consecutive_strips_of_one_page_together():
    assert group_strips(["/a/01.01.jpg", "/a/01.02.jpg", "/a/02.01.jpg", "/a/x.png", "/a/02.02.jpg"]) == [
        ("/a/01.01.jpg", "/a/01.02.jpg"),
        ("/a/02.01.jpg",),
        ("/a/x.png",),
        ("/a/02.02.jpg",),
    ]
    assert group_strips([]) == []


def test_stitch_glues_the_strips_top_to_bottom():
    page = stitch(
        [Image.new("RGB", (4, 2), (255, 0, 0)), Image.new("L", (3, 3), 0), Image.new("RGB", (4, 1), (0, 0, 255))]
    )

    assert page.size == (4, 6)
    assert page.getpixel((0, 0)) == (255, 0, 0)
    assert page.getpixel((0, 2)) == (0, 0, 0)
    # A narrower strip leaves the margin white.
    assert page.getpixel((3, 2)) == (255, 255, 255)
    assert page.getpixel((3, 5)) == (0, 0, 255)


# --- episode: ツイ4 ---------------------------------------------------------------------


def test_twi4_episode_reads_the_strip_and_skips_closed_ones_for_the_next(client, fake_response):
    saizensen, session = client(
        {
            "/tsuredure/index.js": fake_response(text=TWI4_INDEX_JS),
            "/tsuredure/0009.html": fake_response(text=TWI4_HTML),
        },
    )
    episode = saizensen.episode(TWI4_EPISODE_URL)

    assert episode.url == TWI4_EPISODE_URL
    assert episode.series_title == "徒然チルドレン"
    assert episode.episode_title == "告白（８）"
    assert (episode.writer, episode.publisher) == ("若林稔弥", "星海社")
    assert [page.url for page in episode.pages] == [
        f"{HOST}/comics/twi4/tsuredure/works/0009.fUvE7wwnjls9mY5u1rpLuv4sVHverpUP.jpg",
    ]
    assert episode.pages[0].extra == {}
    # `index.js` says 4 to 8 and 10 to 11 are closed, so the open strips either side are 3 and 12.
    assert (episode.prev_url, episode.next_url) == (
        f"{HOST}/comics/twi4/tsuredure/0003.html",
        f"{HOST}/comics/twi4/tsuredure/0012.html",
    )
    assert episode.metadata["kind"] == "twi4"
    assert episode.metadata["closed"] is False
    assert session.calls == [TWI4_EPISODE_URL, f"{HOST}/comics/twi4/tsuredure/index.js"]
    assert "User-Agent" in session.headers_seen[0]


def test_twi4_next_comes_from_the_back_numbers_without_an_index(client, fake_response):
    saizensen, _ = client(
        {
            "/tsuredure/index.js": fake_response(text=NOT_FOUND_HTML, status_code=HTTPStatus.NOT_FOUND),
            "/tsuredure/0009.html": fake_response(text=TWI4_HTML),
        },
    )
    episode = saizensen.episode(TWI4_EPISODE_URL)
    # The back numbers list 10 down to 8 around the current strip.
    assert (episode.prev_url, episode.next_url) == (
        f"{HOST}/comics/twi4/tsuredure/0008.html",
        f"{HOST}/comics/twi4/tsuredure/0010.html",
    )


def test_twi4_closed_strip_has_no_pages_but_a_next(client, fake_response):
    saizensen, _ = client(
        {
            "/tsuredure/index.js": fake_response(text=NOT_FOUND_HTML, status_code=HTTPStatus.NOT_FOUND),
            "/tsuredure/0500.html": fake_response(text=TWI4_CLOSED_HTML),
        },
    )
    episode = saizensen.episode(TWI4_CLOSED_URL)

    assert episode.pages == ()
    assert not episode.readable
    assert episode.episode_title == "#0500"
    assert episode.next_url == f"{HOST}/comics/twi4/tsuredure/0501.html"
    assert episode.metadata["closed"] is True


def test_twi4_last_open_strip_has_no_next(client, fake_response):
    saizensen, _ = client(
        {
            "/tsuredure/index.js": fake_response(text=TWI4_INDEX_JS),
            "/tsuredure/0012.html": fake_response(text=TWI4_HTML.replace("#0009", "#0012")),
        },
    )
    episode = saizensen.episode(f"{HOST}/comics/twi4/tsuredure/0012.html")
    assert (episode.prev_url, episode.next_url) == (f"{HOST}/comics/twi4/tsuredure/0003.html", None)


def test_twi4_all_page_lists_every_strip_of_the_entry(client, fake_response):
    url = f"{HOST}/comics/twi4/zadankai-202608/0001-all.html"
    saizensen, _ = client(
        {
            "/zadankai-202608/index.js": fake_response(text=NOT_FOUND_HTML, status_code=HTTPStatus.NOT_FOUND),
            "/zadankai-202608/0001-all.html": fake_response(text=ZADANKAI_ALL_HTML),
        },
    )
    episode = saizensen.episode(url)

    assert episode.series_title == "第135回 ツイ4新人賞座談会"
    assert episode.episode_title == "あまつばら"
    assert len(episode.pages) == 2
    assert episode.next_url == f"{HOST}/comics/twi4/zadankai-202608/0002.html"


# --- episode: the reader ----------------------------------------------------------------


def test_reader_episode_reads_the_pages_and_the_next_served_volume(client, fake_response):
    saizensen, session = client(
        {
            "/karanokyoukai/03/01.html": fake_response(text=READER_HTML),
            "/comics/karanokyoukai/meta.json": fake_response(payload=READER_META, content_type="application/json"),
        },
    )
    episode = saizensen.episode(READER_EPISODE_URL)

    assert episode.series_title == "空の境界 the Garden of sinners"
    assert episode.episode_title == "１／俯瞰風景 第三回"
    assert episode.writer == "天空すふぃあ, 奈須きのこ (原作), 武内 崇 (キャラクターデザイン原案)"
    assert [page.url for page in episode.pages] == [
        f"{HOST}/works/comics/karanokyoukai/03/01.res/001.png",
        f"{HOST}/works/comics/karanokyoukai/03/01.res/002.png",
        f"{HOST}/works/comics/karanokyoukai/03/01.res/003.png",
    ]
    # Volumes 4 to 71 are gone; 72 is the next one `meta.json` still lists, 2 the one before.
    assert (episode.prev_url, episode.next_url) == (
        f"{HOST}/works/comics/karanokyoukai/02/01.html",
        f"{HOST}/works/comics/karanokyoukai/72/01.html",
    )
    assert episode.metadata["kind"] == "reader"
    assert session.calls == [READER_EPISODE_URL, f"{HOST}/comics/karanokyoukai/meta.json"]


def test_reader_meta_is_fetched_once_per_work_and_the_last_volume_has_no_next(client, fake_response):
    saizensen, session = client(
        {
            "/karanokyoukai/72/01.html": fake_response(text=READER_HTML),
            "/karanokyoukai/73/01.html": fake_response(text=READER_HTML),
            "/comics/karanokyoukai/meta.json": fake_response(payload=READER_META, content_type="application/json"),
        },
    )
    assert saizensen.episode(f"{HOST}/works/comics/karanokyoukai/72/01.html").next_url.endswith("/73/01.html")
    last = saizensen.episode(f"{HOST}/works/comics/karanokyoukai/73/01.html")
    assert (last.prev_url, last.next_url) == (f"{HOST}/works/comics/karanokyoukai/72/01.html", None)
    assert session.calls.count(f"{HOST}/comics/karanokyoukai/meta.json") == 1


def test_reader_episode_has_no_next_without_a_meta(client, fake_response):
    saizensen, _ = client(
        {
            "/karanokyoukai/03/01.html": fake_response(text=READER_HTML),
            "/comics/karanokyoukai/meta.json": fake_response(text=NOT_FOUND_HTML, status_code=HTTPStatus.NOT_FOUND),
        },
    )
    assert saizensen.episode(READER_EPISODE_URL).next_url is None


def test_reader_episode_tolerates_a_meta_without_an_index(client, fake_response):
    saizensen, _ = client(
        {
            "/karanokyoukai/03/01.html": fake_response(text=READER_HTML),
            "/comics/karanokyoukai/meta.json": fake_response(payload={"amazon": {}}, content_type="application/json"),
        },
    )
    assert saizensen.episode(READER_EPISODE_URL).next_url is None


def test_legacy_episode_keeps_the_strips_of_each_page(client, fake_response):
    saizensen, _ = client(
        {
            "/seishunrikon/04/01.html": fake_response(text=LEGACY_HTML),
            "/comics/seishunrikon/meta.json": fake_response(
                payload={"sai-zen-sen": {"index": "11110000"}},
                content_type="application/json",
            ),
        },
    )
    episode = saizensen.episode(LEGACY_EPISODE_URL)

    assert episode.episode_title == "第4回"
    assert [page.url for page in episode.pages] == [
        f"{HOST}/works/comics/seishunrikon/04/01.res/01.01.jpg",
        f"{HOST}/works/comics/seishunrikon/04/01.res/02.01.jpg",
    ]
    assert episode.pages[0].extra == {
        "strips": [
            f"{HOST}/works/comics/seishunrikon/04/01.res/01.01.jpg",
            f"{HOST}/works/comics/seishunrikon/04/01.res/01.02.jpg",
        ],
    }
    assert episode.next_url is None
    assert episode.metadata["kind"] == "legacy"


def test_4pages_episode_reads_the_next_from_its_own_meta(client, fake_response):
    saizensen, session = client(
        {
            "/marine-yumi/01.html": fake_response(text=FOURPAGES_HTML),
            "/4pages-comics/marine-yumi/meta.json": fake_response(
                payload={"sai-zen-sen": {"index": "101"}},
                content_type="application/json",
            ),
        },
    )
    episode = saizensen.episode(FOURPAGES_EPISODE_URL)

    assert episode.series_title == "まりんこゆみ / Marine Corps Yumi"
    assert episode.episode_title == "第1回"
    assert len(episode.pages) == 3
    assert episode.next_url == f"{HOST}/special/4pages-comics/marine-yumi/03.html"
    assert session.calls[-1] == f"{HOST}/special/4pages-comics/marine-yumi/meta.json"


# --- episode: what is not one ------------------------------------------------------------


def test_episode_refuses_a_url_of_the_wrong_shape_without_a_request(client):
    saizensen, session = client({})
    with pytest.raises(NotAnEpisodePageError, match="not an episode page"):
        saizensen.episode(TWI4_WORK_URL)
    assert session.calls == []


def test_missing_page_is_not_an_episode(client, fake_response):
    saizensen, _ = client(
        {"/karanokyoukai/10/01.html": fake_response(text=NOT_FOUND_HTML, status_code=HTTPStatus.NOT_FOUND)}
    )
    with pytest.raises(NotAnEpisodePageError, match="404"):
        saizensen.episode(f"{HOST}/works/comics/karanokyoukai/10/01.html")


def test_other_http_errors_propagate(client, fake_response):
    saizensen, _ = client(
        {"/karanokyoukai/03/01.html": fake_response(text="", status_code=HTTPStatus.SERVICE_UNAVAILABLE)}
    )
    with pytest.raises(HTTPStatusError):
        saizensen.episode(READER_EPISODE_URL)


# --- series ---------------------------------------------------------------------------


def test_twi4_series_urls_come_from_the_index_open_strips_only(client, fake_response):
    saizensen, session = client({"/tsuredure/index.js": fake_response(text=TWI4_INDEX_JS)})
    urls = saizensen.series_urls(f"{HOST}/comics/twi4/tsuredure")

    assert urls == [
        f"{HOST}/comics/twi4/tsuredure/0001.html",
        f"{HOST}/comics/twi4/tsuredure/0002.html",
        f"{HOST}/comics/twi4/tsuredure/0003.html",
        f"{HOST}/comics/twi4/tsuredure/0012.html",
    ]
    assert all(Saizensen.suitable(url) for url in urls)
    # The work page is not needed when the index is there.
    assert session.calls == [f"{HOST}/comics/twi4/tsuredure/index.js"]


def test_twi4_series_urls_fall_back_to_the_work_page(client, fake_response):
    saizensen, _ = client(
        {
            "/tsuredure/index.js": fake_response(text=NOT_FOUND_HTML, status_code=HTTPStatus.NOT_FOUND),
            "/comics/twi4/tsuredure/": fake_response(text=TWI4_WORK_HTML),
        },
    )
    urls = saizensen.series_urls(TWI4_WORK_URL)
    # Oldest first, deduplicated, other works' strips left out.
    assert urls == [
        f"{HOST}/comics/twi4/tsuredure/0001.html",
        f"{HOST}/comics/twi4/tsuredure/0002.html",
        f"{HOST}/comics/twi4/tsuredure/0003.html",
    ]


def test_reader_series_urls_list_the_served_volumes_in_number_order(client, fake_response):
    saizensen, session = client({"/comics/karanokyoukai/": fake_response(text=READER_WORK_HTML)})
    urls = saizensen.series_urls(f"{HOST}/comics/karanokyoukai")

    assert urls == [
        f"{HOST}/works/comics/karanokyoukai/01/01.html",
        f"{HOST}/works/comics/karanokyoukai/02/01.html",
        f"{HOST}/works/comics/karanokyoukai/72/01.html",
        f"{HOST}/works/comics/karanokyoukai/73/01.html",
    ]
    assert all(Saizensen.suitable(url) for url in urls)
    assert session.calls == [READER_WORK_URL]


def test_4pages_series_urls_list_the_volumes_in_number_order(client, fake_response):
    saizensen, _ = client({"/4pages-comics/marine-yumi/": fake_response(text=FOURPAGES_WORK_HTML)})
    assert saizensen.series_urls(FOURPAGES_WORK_URL) == [
        f"{HOST}/special/4pages-comics/marine-yumi/01.html",
        f"{HOST}/special/4pages-comics/marine-yumi/02.html",
        f"{HOST}/special/4pages-comics/marine-yumi/03.html",
    ]


def test_series_urls_raises_on_an_empty_listing(client, fake_response):
    saizensen, _ = client({"/comics/nothing/": fake_response(text="<html><body><p>coming soon</p></body></html>")})
    with pytest.raises(NotAnEpisodePageError, match="no open episode"):
        saizensen.series_urls(f"{HOST}/comics/nothing/")


def test_series_urls_raises_on_a_missing_work(client, fake_response):
    saizensen, _ = client(
        {
            "/nonexistent/index.js": fake_response(text=NOT_FOUND_HTML, status_code=HTTPStatus.NOT_FOUND),
            "/comics/twi4/nonexistent/": fake_response(text=NOT_FOUND_HTML, status_code=HTTPStatus.NOT_FOUND),
        },
    )
    with pytest.raises(NotAnEpisodePageError, match="404"):
        saizensen.series_urls(f"{HOST}/comics/twi4/nonexistent/")


def test_series_urls_refuses_a_url_of_the_wrong_shape(client):
    saizensen, session = client({})
    with pytest.raises(UnsupportedUrlError):
        saizensen.series_urls(TWI4_EPISODE_URL)
    assert session.calls == []


# --- downloading ----------------------------------------------------------------------


def test_download_writes_a_strip_as_served(client, fake_response, tmp_path):
    saizensen, session = client(
        {
            "/tsuredure/index.js": fake_response(text=TWI4_INDEX_JS),
            "/tsuredure/0009.html": fake_response(text=TWI4_HTML),
            "/works/0009.": fake_response(_png((128, 128, 128)), content_type="image/jpeg"),
        },
    )
    result = Downloader(saizensen, tmp_path).download(TWI4_EPISODE_URL)

    assert result.status == "saved"
    assert result.save_dir == tmp_path / "sai-zen-sen.jp" / "徒然チルドレン" / "告白（８）"
    written = Image.open(result.save_dir / "0.jpg")
    assert written.size == (6, 8)
    assert written.getpixel((3, 4)) == (128, 128, 128)
    assert not (result.save_dir / "1.jpg").exists()
    # The image is asked for with the episode as Referer, which the site does not need but tolerates.
    assert session.calls[-1].endswith("0009.fUvE7wwnjls9mY5u1rpLuv4sVHverpUP.jpg")
    assert session.headers_seen[-1]["Referer"] == TWI4_EPISODE_URL


# --- logging in -----------------------------------------------------------------------


# --- the real site --------------------------------------------------------------------

# One episode per known host, free to read without an account: the title
# strip of 徒然チルドレン, one of the few a closed work keeps open.
TEST_URLS: dict[str, str] = {
    "sai-zen-sen.jp": "https://sai-zen-sen.jp/comics/twi4/tsuredure/0001.html",
}


@pytest.mark.network
@pytest.mark.parametrize("host", TEST_URLS)
def test_site_download(tmp_path, host):
    result = Downloader(Saizensen(), tmp_path, only_first=True).download(TEST_URLS[host])
    assert result.status == "saved"
    assert result.episode.series_title == "徒然チルドレン"
    assert (result.save_dir / "0.jpg").stat().st_size > 0


@pytest.mark.network
def test_reader_volume_downloads_a_page(tmp_path):
    result = Downloader(Saizensen(), tmp_path, only_first=True).download(READER_EPISODE_URL)
    assert result.status == "saved"
    assert result.episode.series_title == "空の境界 the Garden of sinners"
    assert len(result.episode.pages) > 1


@pytest.mark.network
def test_twi4_work_lists_its_open_strips_only():
    saizensen = Saizensen()
    urls = saizensen.series_urls(TWI4_WORK_URL)
    assert urls[0] == TEST_URLS["sai-zen-sen.jp"]
    assert len(urls) < 100
    assert all(Saizensen.suitable(url) for url in urls)


@pytest.mark.network
def test_closed_strip_is_locked_on_the_site():
    episode = Saizensen().episode(TWI4_CLOSED_URL)
    assert episode.pages == ()
    assert episode.metadata["closed"] is True
    assert episode.next_url is not None


@pytest.mark.network
def test_reader_work_lists_served_volumes():
    urls = Saizensen().series_urls(READER_WORK_URL)
    assert READER_EPISODE_URL in urls
    assert all(Saizensen.suitable(url) for url in urls)
