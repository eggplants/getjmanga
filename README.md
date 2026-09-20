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
```

| Option | Description |
| --- | --- |
| `-s`, `--search` | treat each url as a web page and download what it links to instead |
| `-b`, `--bulk`, `--no-bulk` | follow every next episode |
| `-d DIR`, `--savedir DIR` | directory to save into, as `<DIR>/<host>/<series>/<episode>/` (default: the config's `savedir`, else `.`) |
| `-f`, `--first` | download only the first page |
| `-o`, `--overwrite`, `--no-overwrite` | download again if it exists |
| `-m`, `--metadata` | save episode metadata as `metadata.json` |
| `-u ID`, `--username ID` | id or email address to log in with |
| `-p PW`, `--password PW` | password (prompted for if `-u` is given without it) |
| `-e NAME`, `--extractor NAME` | use this extractor instead of picking one by the url's host |
| `-c FILE`, `--config FILE` | config file holding site credentials (default: `~/.config/getjmanga/config.toml`) |
| `-q`, `--quiet` | disable console output |
| `--list-extractors` | list every extractor, its URL shapes and its hosts |

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
