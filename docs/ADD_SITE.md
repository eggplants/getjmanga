# Adding a site

How to make `jm` read one more site. There are two cases, and the first is
much cheaper than the second:

- **The site runs a viewer that already has an extractor** (GigaViewer,
  Comici+). Add its host to that extractor
  ([Case A](#2-case-a-a-new-host-for-an-existing-extractor)).
- **The site has a viewer of its own.** Write a new extractor
  ([Case B](#3-case-b-a-new-extractor)).

Either way, start with the [investigation](#1-find-out-how-the-site-works) and
end with the [checklist](#checklist).

## 1. Find out how the site works

Open an episode page in the browser with the developer tools' network tab on.
Then answer these questions. Every one of them maps onto a method of
`Extractor`.

| Question | Where it ends up |
| --- | --- |
| Which viewer is it? Look for `gigaviewer` and `script#episode-json` (GigaViewer), `comici.jp` and `#comici-viewer` (Comici+), or `_pdata_` (Piccoma) in the HTML. | Case A if any of these match. |
| What does an episode URL look like? A series URL? | `HOSTS`, `URL_FORMS`, `suitable()`, `is_series()` |
| Where does the page list come from -- JSON embedded in the HTML, or an API call the viewer makes? What request headers does it need (`Referer`, `Authorization`, `X-Requested-With`, ...)? | `episode()` |
| How are "the next episode" and "the previous episode" named? A `prev`/`next` field or link, or the episode's place in the work's listing -- `neighbours()` in `getjmanga/extractor.py` looks either side of a listing, and `Extractor._listed_neighbours()` does so off `series_urls()` when the page names only the next one. | `Episode.next_url`, `Episode.prev_url` |
| How does a series page or feed list its episodes, and in which order? | `series_urls()` |
| Are the page images scrambled? Save one from the network tab and look at it. If so, where does the viewer get the permutation or the seed? Read the viewer's JavaScript. | `image()`, `Page.extra` |
| Do the images need a `Referer` or a cookie to be served? Try `curl` without one. | `image()` |
| What does a paywalled, wait-ticketed or login-only episode look like -- an empty page list, a redirect to a sign-in page, a purchase page without the viewer? | `episode()` returning empty `pages` |
| How does sign-in work (form + CSRF token, JSON API, OAuth)? | `login()` |
| Does one account work on every host the extractor takes (Comici+), or does each site have its own (GigaViewer)? | `CONFIG_KEY` |

The existing extractors are a good reference for what a finished
investigation looks like: `getjmanga/extractors/comici.py` reads an element off
the HTML *and* falls back to an API, `piccoma.py` derives a shuffle seed from
the image URL, `gigaviewer.py` retries a page the site sometimes serves
without its JSON. And `fuz.py` talks a protobuf API (field numbers read out
of the site's bundled JS) and decrypts AES-CBC page files with a key the API
hands over per page. It only gets those files served because the API response
set a cookie on the shared session.

What is the reader's rather than the site's lives in `getjmanga/viewers/`:
`speedbinb.py` (Voyager's SpeedBinb, its API dance and its static export),
`yondemill.py`, `publus.py`, `kmanga.py` and `seedrandom.py`. A site on one of
those readers writes only the page around it -- how the content id and the API
endpoint are found, the listing, the titles -- and calls in there for the rest
(`gaugau.py`, `bloom.py` and `porta.py` show the three SpeedBinb shapes).
`getjmanga/cipher.py` undoes the AES-CBC and XOR masking several sites put
their page files under.

When the viewer is a JavaScript app, download its bundles (the `<script src>`
of an episode page) and grep them: the API path constants, the request builder
and any `decode`/`decrypt` function are all in there, and the network tab tells
you which of them a page actually calls.

## 2. Case A: a new host for an existing extractor

Before doing any of this, try the site with the extractor forced:

```bash
jm -e comici -f -d /tmp/out https://new-site.example/episodes/abc123
```

If that already works, the site only needs steps 1 to 4.

1. Add the hostname to `HOSTS` of the extractor in
   `getjmanga/extractors/<name>.py`, keeping the tuple sorted. `suitable()`
   matches the exact hostname, so `www.example.com` and `example.com` are two
   entries if both serve the viewer.
2. Add one **free** episode of the site to `TEST_URLS` in
   `tests/extractors/test_<name>.py`. `test_registry.py::test_every_known_host_has_a_site_test`
   fails until every host has an entry there, and `test_site_download` then
   downloads its first page in CI.
3. If the site's URLs differ in shape from the others (an imprint prefix, a
   different id format), adjust the path regexes. Add an offline test for
   the new shape; `test_series_urls_keeps_an_imprint_prefix` in
   `tests/extractors/test_comici.py` shows the pattern.
4. Update the README (see [Documentation](#4-documentation)).
5. Run the [checks](#5-checks).

## 3. Case B: a new extractor

### The module

Create `getjmanga/extractors/<name>.py`. One module per site, holding the
extractor class and any descrambling code as plain functions next to it, so
you can test the functions on their own. When a second site turns up on the
same viewer, the viewer's part moves to `getjmanga/viewers/<viewer>.py` and
both extractors import it from there: an extractor never imports another
extractor.

```python
"""<Site name>, and whatever is special about its viewer."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, ClassVar
from urllib.parse import urlparse

from getjmanga.errors import NotAnEpisodePageError, UnsupportedUrlError
from getjmanga.extractor import Episode, Extractor, Page

if TYPE_CHECKING:
    from PIL import Image

_EPISODE_PATH = re.compile(r"^/episode/(?P<id>\d+)/?$")
_SERIES_PATH = re.compile(r"^/series/(?P<id>\d+)/?$")


class Example(Extractor):
    """Fetch episodes from Example."""

    NAME = "example"                       # what `--extractor` and `--list-extractors` call it
    HOSTS = ("comic.example.com",)         # exact hostnames, sorted
    URL_FORMS = (                          # shown by `--list-extractors`
        "https://comic.example.com/episode/<id>",
        "https://comic.example.com/series/<id>",
    )
    CONFIG_KEY = "example"                 # `[site.example]` in config.toml signs in on every host;
                                           # leave it out when each host has accounts of its own
    # Only when the site wants more than the browser-like defaults.
    HEADERS: ClassVar[dict[str, str]] = {**Extractor.HEADERS, "X-Requested-With": "XMLHttpRequest"}

    @classmethod
    def suitable(cls, url: str) -> bool:
        # The default checks https + HOSTS. Narrow it when the site has pages
        # the extractor cannot read, so `find_extractor()` does not claim them.
        if not super().suitable(url):
            return False
        path = urlparse(url).path
        return bool(_EPISODE_PATH.match(path) or _SERIES_PATH.match(path))

    @classmethod
    def is_series(cls, url: str) -> bool:
        return _SERIES_PATH.match(urlparse(url).path) is not None

    def series_urls(self, url: str) -> list[str]:
        match = _SERIES_PATH.match(urlparse(url).path)
        if match is None:
            msg = f"{url} is not a series page."
            raise UnsupportedUrlError(msg)
        res = self._get(url)
        ...  # collect the episode links, in the order to download them
        if not urls:
            msg = f"the series at {url} lists no episode."
            raise NotAnEpisodePageError(msg)
        return urls

    def episode(self, url: str) -> Episode:
        res = self._get(url)
        ...  # read the titles, the page image URLs and the next episode
        if no_viewer_on_the_page:
            msg = f"no viewer on {url}."
            raise NotAnEpisodePageError(msg)
        return Episode(
            url=url,
            series_title=series,
            episode_title=title,
            pages=tuple(Page(url=src, extra={"seed": seed}) for src in image_urls),  # () when locked
            prev_url=prev_url,          # None at the start of the series
            next_url=next_url,          # None at the end of the series
            metadata=raw_json,          # whatever the site said; written by --metadata
        )

    def image(self, page: Page, episode: Episode) -> Image.Image:
        # The default GETs `page.url` with the episode URL as Referer and hands
        # the image over as-is. Override to descramble, or to send other headers.
        image = self._fetch_image(page.url, headers={**self.HEADERS, "Referer": episode.url})
        return descramble(image, str(page.extra["seed"]))

    def login(self, url: str, username: str, password: str) -> None:
        # The default raises LoginError("... does not support logging in").
        ...
```

### The contract

These are what `Downloader` and the CLI rely on. Keep them, or they break in
ways the offline tests will not catch.

- **A locked episode returns, it does not raise.** Paywalled, wait-ticketed
  and login-only episodes come back as an `Episode` with empty `pages` (and
  still with `next_url`, if the site names it), so a `-b` run reports it as
  `skip:` and carries on down the chain. Reserve `NotAnEpisodePageError` for
  a page that is not an episode at all -- no viewer, wrong URL, a 404 page.
  On sites where a locked episode serves a purchase page *without* the
  viewer, the CLI treats `NotAnEpisodePageError` as the end of the chain, so
  that case is fine too.
- **`pages` is in reading order** and holds one `Page` per image. The
  downloader numbers them `00.jpg`, `01.jpg`, ... from the tuple order.
- **`Page.extra` is JSON-serialisable.** `--metadata` writes it out, so a
  tile permutation goes in as a list or a string, not as a NumPy array
  or a callable. Anything `image()` needs to put the page back together lives
  here, not on the instance.
- **`Episode.metadata` is JSON-serialisable** for the same reason.
- **Titles are raw.** Do not sanitise `series_title` or `episode_title`; the
  downloader does that, and keeps a `/` readable as `／`.
- **`suitable()` is cheap and offline.** It runs against every URL on the
  command line for every extractor; a regex on the URL, never a request.
- **`series_urls()` returns episode URLs `episode()` accepts**, deduplicated,
  in download order. When the listing is empty, raise
  `NotAnEpisodePageError`; when the URL is not a series URL, raise
  `UnsupportedUrlError`.
- **Requests go through `self._get()`** (raises on a failing status, applies
  `HEADERS` and `TIMEOUT`) or `self._session` directly when a non-2xx answer
  is meaningful (an API that says 404 for "no more pages"). Never build a
  `httpx.Client` of your own: the CLI hands every extractor one shared
  session so cookies from `login()` reach the requests that follow.
- **The CLI calls `login()` once per site**, with any URL on that site,
  using `-u`/`-p` or the config file's `[site."<host>"]` /
  `[site.<CONFIG_KEY>]` section. When the site refuses the credentials, raise
  `LoginError`. Store whatever a later request needs (a token, a flag) on the
  instance. Set `CONFIG_KEY` only when one account really works on every
  host in `HOSTS`.
- **Descrambling is a pure function** `descramble(image, ...) -> Image` next
  to the class (or in `getjmanga/viewers/` when the viewer is shared),
  returning a new image, tested on a synthetic tiled image without touching
  the network.

### Registering

1. Import the class in `getjmanga/extractors/__init__.py` and add it to
   `EXTRACTORS`. The tuple is ordered: `find_extractor()` hands a URL to the
   first class whose `suitable()` accepts it, so put a class with a broad
   `suitable()` *after* the picky ones. Add it to `__all__` there too.
2. Re-export it from `getjmanga/__init__.py` (import and `__all__`), so
   `from getjmanga import Example` works.
3. `--list-extractors` and the `extractors:` line of `--help` pick it up from
   `EXTRACTORS`; nothing else in the CLI needs to change.

### Tests

Create `tests/extractors/test_<name>.py`. The `fake_session` and
`fake_response` fixtures from `tests/conftest.py` script a site without the
network: the fixture matches routes by substring on the requested URL, first
match wins, and a route holding a list hands its responses out in order.

```python
from getjmanga.downloader import Downloader
from getjmanga.extractors.example import Example, descramble

EPISODE_HTML = """<html>...the parts episode() reads...</html>"""


def test_episode_reads_the_titles_and_the_pages(fake_session, fake_response):
    session = fake_session({"/episode/": fake_response(text=EPISODE_HTML)})
    episode = Example(session).episode("https://comic.example.com/episode/1")

    assert episode.series_title == "..."
    assert [page.url for page in episode.pages] == ["...", "..."]
    assert (episode.prev_url, episode.next_url) == (None, "https://comic.example.com/episode/2")
    # What was sent is recorded too:
    assert session.headers_seen[-1]["Referer"] == "https://comic.example.com/episode/1"
    assert session.params_seen[-1] is None
```

Cover at least:

- `suitable()` accepting each URL form and rejecting `http://`, other hosts
  and pages it cannot read (parametrised).
- `episode()` on a readable episode, on a locked one (empty `pages`, the
  `next_url` still there), and on a page without a viewer
  (`NotAnEpisodePageError`).
- `series_urls()` order and deduplication, and an empty listing.
- `descramble()` restoring a synthetic tiled image, if the site has one of
  its own; `image()` with a fake image route, checking the pixels come back
  where they belong and the `Referer` sent.
- `login()` posting what the site expects and raising on a refusal, if the
  site has one.
- Whatever oddity the investigation turned up: a `Referer` the API insists
  on, a retry the site needs, a redirect that means "locked".

And leave out what adds nothing: the default `login()` refusing (tested once
in `tests/test_extractor.py`), `Downloader` reporting an empty episode as `locked`
(tested in `test_downloader.py`), a shared viewer's own behaviour (tested in
`tests/viewers/`), a parse helper the `episode()` test already drives, a URL
builder the `episode().url` assertion already covers, or a listing being
fetched once.

Then the live part, at the bottom of the file:

```python
# One episode per known host, free to read without an account.
TEST_URLS: dict[str, str] = {
    "comic.example.com": "https://comic.example.com/episode/1",
}


@pytest.mark.network
@pytest.mark.parametrize("host", TEST_URLS)
def test_site_download(tmp_path, host):
    result = Downloader(Example(), tmp_path, only_first=True).download(TEST_URLS[host])
    assert result.status == "saved"
    assert (result.save_dir / "0.jpg").exists()
```

Pick an episode that is free and unlikely to disappear (the first episode of
a long-running series). If the site scrambles only some episodes, pick one
scrambled and one not. Some sites refuse GitHub's runners outright: a 403, a
412, or a stand-in page with no viewer in it, while the same test passes from
home. Such a site gets `@pytest.mark.geoblocked` next to
`@pytest.mark.network` on every test it breaks. `tests/conftest.py` skips
those when `GITHUB_ACTIONS` is set and runs them everywhere else.
`test_site_download` in `test_comici.py` shows the other shape, for a site
that only refuses some networks with a 403.

## 4. Documentation

- **`docs/SUPPORTED_SITES.md` `Sites` table.** One row per site, naming
  the publisher, the viewer platform and the extractor; a site that is not in
  the list yet gets a new row in the same shape. When something needs saying
  (a site that moved domains, a sibling site that is not covered), add a note
  under the table. Bump the site count in the README's `Supported sites`
  paragraph.
- **`docs/SUPPORTED_SITES.md` `URL formats` table.** One row per URL shape a new
  extractor reads, and what a series URL downloads.
- **README `Configuration`.** A `[site.<CONFIG_KEY>]` example when the
  extractor has a shared key.
- **`CLAUDE.md` Layout.** A new module under `getjmanga/viewers/` gets a
  mention there; extractors are listed in `docs/SUPPORTED_SITES.md` only.
- **`--list-extractors`** is generated from `NAME`, `URL_FORMS` and `HOSTS`,
  so keep `URL_FORMS` honest.

## 5. Checks

```bash
uv run pytest -m "not network" tests/extractors/test_<name>.py   # the fakes
uv run pytest -m network -k <name>                                 # the real site
jm -f -m -d /tmp/out https://comic.example.com/episode/1         # one page, plus metadata.json
jm -e <name> -f -d /tmp/out https://unlisted.example/episode/1   # forced onto an unlisted host
mise run ci                                                        # what CI runs
```

Open the downloaded `0.jpg`. A descrambler that is subtly wrong (tiles off
by one, the edge strip shuffled, rows and columns swapped) passes every
size and histogram assertion and still produces an unreadable page, so look
at it once.

## Checklist

- [ ] `HOSTS`, `URL_FORMS` (and `NAME`, `CONFIG_KEY` if one account spans the hosts) filled in
- [ ] `episode()` returns empty `pages` for a locked episode, raises
  `NotAnEpisodePageError` for a non-episode page
- [ ] `Page.extra` / `Episode.metadata` are JSON-serialisable
- [ ] New extractor registered in `EXTRACTORS` (in the right position) and
  re-exported from `getjmanga/__init__.py`
- [ ] Offline tests for URLs, episode, series, descrambling, download, login
- [ ] `TEST_URLS` has one free episode per host; `test_every_known_host_has_a_site_test` passes
- [ ] `docs/SUPPORTED_SITES.md` site row and URL formats added, README site count bumped; `CLAUDE.md` bullet added
- [ ] `mise run ci` passes, including the `network` tests (`geoblocked` ones marked if the site refuses GitHub's runners)
- [ ] A downloaded page looked at with human eyes
- [ ] Committed as `feat: <site or extractor name>` (see `.claude/skills/commit/SKILL.md`)
