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

Retrieve and save images from Japanese web comic sites.

_Do not redistribute the downloaded images. Keep them for private use._

## Supported sites

See [docs/SUPPORTED_SITES.md](
  <https://github.com/eggplants/getjmanga/blob/master/docs/SUPPORTED_SITES.md>
).

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

# episodes in bulk: this one and every next one
jm -b https://shonenjumpplus.com/episode/13932016480028799982

# every previous one too: the whole work from one episode
jm -B https://shonenjumpplus.com/episode/13932016480028799982

# login
jm -u you@example.com https://piccoma.com/web/viewer/8195/1185884

# every link on a page that some extractor takes
jm -s https://shonenjumpplus.com/

# numbered pages: 1 to 3, or from 1 on until a page has nothing new
jm -s "https://comic-ryu.jp/series/list/up/[1-3]"
jm -s "https://comic-ryu.jp/series/list/up/[1-]"

# remember the work, then download what is new in every remembered work
jm -S -b https://shonenjumpplus.com/episode/13932016480028799982
jm patrol
```

### Configuration

Use `jm c`.

Default: `~/.config/getjmanga/config.toml`

Example: [config.example.toml](
  <https://github.com/eggplants/getjmanga/blob/master/config.example.toml>
)

```shellsession
jm c init

# asks for the username and password
jm c site shonenjumpplus.com
jm c site piccoma

jm c savedir ~/manga
jm c overwrite true
jm c bulk false
jm c both true

jm c patrol https://shonenjumpplus.com/episode/13932016480028799982
jm c patrol -s https://shonenjumpplus.com/
```

### Patrol

`jm -S` adds what it downloads to a list of works to watch for new episodes in the config file.

`jm c patrol <url>` adds a url to the list but does not download it.

`jm p` then goes through the list. It skips the episodes that are already saved, so it downloads only what is new.

What `jm p` does with an entry depends on what the entry is:

- For an episode, it follows the next links to the newest episode. The entry
  then moves to the first episode that is still locked. As a result, a
  wait-to-read episode gets one more try next time.
- For a series page, it reads the episode list again.
- For a page stored with `-s`, it scans the links again.

```toml
patrol = [
  { url = "https://shonenjumpplus.com/episode/13932016480028799982", title = "阿波連さんははかれない" },
  { url = "https://takecomic.jp/series/3f846451aff2d/1", title = "メイドインアビス" },
  { url = "https://shonenjumpplus.com/", search = true },
  ...
]
```

## Library

```python
from getjmanga import Downloader, find_extractor

url = "https://takecomic.jp/episodes/74f33031e13cd"
extractor = find_extractor(url)() # returns `Comici`
result = Downloader(extractor, "out").download(url)
print(result.status, result.save_dir, result.episode.next_url)
```

An extractor on its own reads the site and writes nothing:

```python
from getjmanga import Comici

comici = Comici()
for url in comici.series_urls("https://takecomic.jp/series/b167ea507d35f"):
    episode = comici.episode(url)
    print(episode.episode_title, len(episode.pages), episode.readable)
```

### Writing an extractor

See [docs/ADD_SITE.md](
  <https://github.com/eggplants/getjmanga/blob/master/docs/ADD_SITE.md>
).

## License

[MIT License](
  <https://github.com/eggplants/getjmanga/blob/master/LICENSE.txt>
)
