# getjmanga

[![PyPI](
  <https://img.shields.io/pypi/v/getjmanga?color=blue>
  )](
  <https://pypi.org/project/getjmanga/>
) [![CI](
  <https://github.com/eggplants/getjmanga/actions/workflows/ci.yml/badge.svg>
  )](
  <https://github.com/eggplants/getjmanga/actions/workflows/ci.yml>
)

[![ghcr size](
  <https://ghcr-badge.egpl.dev/eggplants/getjmanga/size>
)](
  <https://github.com/eggplants/getjmanga/pkgs/container/getjmanga>
)

Retrieve and save images from japanese web comic sites.

_Note: Redistribution of downloaded image data is prohibited. Please keep it to private use._

## Supported sites

See [docs/SUPPORTED_SITES.md](docs/SUPPORTED_SITES.md).

## Installation

```bash
# mise via github release
mise use -g github:eggplants/getjmanga

# mise via pipx
mise use -g pipx:getjmanga

# pipx
pipx install getjmanga

# pip
pip install getjmanga
```

### Docker

```bash
docker pull ghcr.io/eggplants/getjmanga

docker run --rm -v "$PWD:/work" -w /work \
  ghcr.io/eggplants/getjmanga https://takecomic.jp/episodes/74f33031e13cd
```

## CLI

```shellsession
# one episode
jm https://takecomic.jp/episodes/74f33031e13cd https://piccoma.com/web/viewer/8195/1185884

# episodes in bulk
jm -b https://shonenjumpplus.com/episode/13932016480028799982

# login
jm -u you@example.com https://piccoma.com/web/viewer/8195/1185884

# every link on a page that some extractor takes
jm -s https://shonenjumpplus.com/

# remember the work, then download what is new in every remembered work
jm -S -b https://shonenjumpplus.com/episode/13932016480028799982
jm patrol
```

### Configuration

Use `jm config` / `jm c`.

```shellsession
jm c init

# asks for the username and password
jm c site shonenjumpplus.com
jm c site piccoma

jm c savedir ~/manga
jm c overwrite true
jm c bulk false
```

### Patrol

`jm -S` remembers what it downloaded as a `[[patrol]]` entry in the config file,
and `jm patrol` / `jm p` goes through them: an episode is followed to the newest
one and the entry moves along to the first episode still locked (so a wait-to-read
episode is tried again next time), a series page is listed again, a `-s` page is
scanned again. Episodes already there are skipped, so only what is new gets
downloaded. `jm patrol` takes the download options (`-d`, `-o`, `-q`, ...) but no url.

```toml
patrol = [
  { url = "https://shonenjumpplus.com/episode/13932016480028799982", title = "SPY×FAMILY" },
  { url = "https://shonenjumpplus.com/", search = true },
  ...
]
```

## Library

```python
from getjmanga import Downloader, find_extractor

url = "https://takecomic.jp/episodes/74f33031e13cd"
extractor = find_extractor(url)()      # -> Comici
result = Downloader(extractor, "out").download(url)
print(result.status, result.save_dir, result.episode.next_url)
```

An extractor on its own reads without writing anything:

```python
from getjmanga import Comici

comici = Comici()
for url in comici.series_urls("https://takecomic.jp/series/b167ea507d35f"):
    episode = comici.episode(url)
    print(episode.episode_title, len(episode.pages), episode.readable)
```

### Writing an extractor

See [docs/ADD_SITE.md](docs/ADD_SITE.md).

## License

[MIT License](
  <https://github.com/eggplants/getjmanga/blob/master/LICENSE.txt>
)
