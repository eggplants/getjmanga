"""The config file: `$XDG_CONFIG_HOME/getjmanga/config.toml`, holding site credentials.

```toml
[site."shonenjumpplus.com"]   # one GigaViewer site; each has an account of its own
username = "you@example.com"
password = "..."

[site.comici-plus]            # a Comici ID works on every Comici+ site
username = "comici-id"
password = "..."              # leave it out to be prompted

[site.piccoma]
username = "you@example.com"
password = "..."
```

A `[site.<key>]` section is looked up by the URL's hostname first, then by the
extractor's `CONFIG_KEY`, so a per-host section beats the shared one.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from .extractors.common import GetjmangaError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from .extractors.common import Extractor

#: Where the config lives under `XDG_CONFIG_HOME` (`~/.config` when unset).
CONFIG_RELPATH = Path("getjmanga") / "config.toml"


class ConfigError(GetjmangaError):
    """The config file cannot be read or does not have the expected shape."""


def default_config_path() -> Path:
    """Where the config file is looked for when `--config` is not given.

    Returns:
        `$XDG_CONFIG_HOME/getjmanga/config.toml`, `~/.config` standing in for an
        unset or relative `XDG_CONFIG_HOME` as the XDG spec says.
    """
    xdg = os.environ.get("XDG_CONFIG_HOME", "")
    base = Path(xdg) if xdg and Path(xdg).is_absolute() else Path.home() / ".config"
    return base / CONFIG_RELPATH


@dataclass(frozen=True)
class Credentials:
    """What `[site.<key>]` holds."""

    username: str
    #: None means "ask for it", the way `-u` without `-p` does.
    password: str | None = None


@dataclass(frozen=True)
class Config:
    """The parsed config file."""

    #: `[site.<key>]` sections, by key as written.
    sites: Mapping[str, Credentials] = field(default_factory=dict)
    #: The file the sections came from, or None when there was none.
    path: Path | None = None

    def credentials(self, extractor: type[Extractor], url: str) -> Credentials | None:
        """The credentials to sign in to `url` with.

        Args:
            extractor: The extractor `url` is handled by.
            url: The URL being downloaded.

        Returns:
            The `[site."<host>"]` section for the URL's host, else the
            `[site.<CONFIG_KEY>]` section the extractor shares across its
            hosts, else None.
        """
        host = urlparse(url).hostname or ""
        for key in (host, extractor.CONFIG_KEY):
            if key and key in self.sites:
                return self.sites[key]
        return None


def load_config(path: Path | None = None) -> Config:
    """Read the config file.

    Args:
        path: The file to read instead of `default_config_path()`.

    Returns:
        The config; an empty one when the file does not exist.

    Raises:
        ConfigError: The file is not valid TOML, or a section is not shaped as expected.
    """
    where = path if path is not None else default_config_path()
    try:
        raw = where.read_bytes()
    except FileNotFoundError:
        return Config()
    except OSError as exc:
        msg = f"cannot read {where}: {exc.strerror or exc}"
        raise ConfigError(msg) from exc
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        msg = f"{where} is not valid TOML: {exc}"
        raise ConfigError(msg) from exc
    return Config(sites=_sites(data.get("site", {}), where), path=where)


def _sites(table: object, where: Path) -> dict[str, Credentials]:
    if not isinstance(table, dict):
        msg = f"{where}: [site] must be a table of [site.<key>] sections."
        raise ConfigError(msg)
    sites: dict[str, Credentials] = {}
    for key, section in table.items():
        if not isinstance(section, dict) or not isinstance(section.get("username"), str) or not section["username"]:
            msg = f'{where}: [site."{key}"] needs a username = "..." line.'
            raise ConfigError(msg)
        password = section.get("password")
        if password is not None and not isinstance(password, str):
            msg = f'{where}: [site."{key}"] password must be a string.'
            raise ConfigError(msg)
        sites[str(key)] = Credentials(username=section["username"], password=password)
    return sites
