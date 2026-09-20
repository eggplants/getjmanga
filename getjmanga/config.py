"""The config file: `$XDG_CONFIG_HOME/getjmanga/config.toml`, holding site credentials and defaults.

```toml
savedir = "~/manga"           # what `-d` defaults to
overwrite = false             # whether `-o` is on unless `--no-overwrite` is given
bulk = false                  # whether `-b` is on unless `--no-bulk` is given

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

`jm config` writes the file: `init` lays down a commented template, `site`
asks for an account, and `savedir` / `overwrite` / `bulk` set the defaults.
The writes go through tomlkit so the comments in a hand-edited file survive.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TypeVar
from urllib.parse import urlparse

import tomlkit
from tomlkit.exceptions import ParseError

from .errors import GetjmangaError

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from .extractor import Extractor

#: Where the config lives under `XDG_CONFIG_HOME` (`~/.config` when unset).
CONFIG_RELPATH = Path("getjmanga") / "config.toml"

#: The top-level keys that stand in for a command line flag.
Option = Literal["savedir", "overwrite", "bulk"]

_T = TypeVar("_T", str, bool)

#: What `init` writes. The defaults are real lines, not comments, so that
#: `set_option` replaces them in place instead of appending below the
#: commented-out section example, where uncommenting it would swallow them.
TEMPLATE = """\
# getjmanga config -- `jm config --help` edits it.

# Defaults for the command line flags of the same name.
savedir = "."
overwrite = false
bulk = false

# One [site.<key>] section per account. The key is the site's host, or the
# key `jm --list-extractors` prints for a login shared across hosts.
# Leave the password out to be prompted for it once per run.
# [site."shonenjumpplus.com"]
# username = "you@example.com"
# password = "..."
"""


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
    #: What `-d` defaults to, `~` expanded; None leaves it to the command line.
    savedir: Path | None = None
    #: Whether `-o` is on by default.
    overwrite: bool = False
    #: Whether `-b` is on by default.
    bulk: bool = False

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
    savedir = _option(data, "savedir", str, where)
    return Config(
        sites=_sites(data.get("site", {}), where),
        path=where,
        savedir=Path(savedir).expanduser() if savedir else None,
        overwrite=_option(data, "overwrite", bool, where) or False,
        bulk=_option(data, "bulk", bool, where) or False,
    )


def _option(data: dict[str, object], name: str, kind: type[_T], where: Path) -> _T | None:
    value = data.get(name)
    if value is not None and not isinstance(value, kind):
        msg = f"{where}: {name} must be a {'string' if kind is str else 'boolean'}."
        raise ConfigError(msg)
    return value


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


# --- writing it ----------------------------------------------------------------------


def init_config(path: Path | None = None) -> Path:
    """Lay down the commented template.

    Args:
        path: The file to create instead of `default_config_path()`.

    Returns:
        The file created.

    Raises:
        ConfigError: The file is already there, or cannot be written.
    """
    where = path if path is not None else default_config_path()
    if where.exists():
        msg = f"{where} already exists."
        raise ConfigError(msg)
    _write(where, TEMPLATE)
    return where


def set_site(key: str, credentials: Credentials, path: Path | None = None) -> Path:
    """Write a `[site.<key>]` section, replacing the one already there.

    Args:
        key: The host, or the extractor's `CONFIG_KEY`.
        credentials: The account; a None password leaves the line out.
        path: The file to edit instead of `default_config_path()`.

    Returns:
        The file written.

    Raises:
        ConfigError: The file cannot be read, parsed or written.
    """
    section = tomlkit.table()
    section["username"] = credentials.username
    if credentials.password is not None:
        section["password"] = credentials.password

    def edit(document: tomlkit.TOMLDocument) -> None:
        sites = document.get("site")
        if not isinstance(sites, dict):
            sites = document["site"] = tomlkit.table(is_super_table=True)
        sites[key] = section

    return _edit(path, edit)


def set_option(name: Option, value: str | bool, path: Path | None = None) -> Path:
    """Write a top-level key, replacing the one already there.

    Args:
        name: `savedir`, `overwrite` or `bulk`.
        value: A path for `savedir`, kept as given; True or False for the others.
        path: The file to edit instead of `default_config_path()`.

    Returns:
        The file written.

    Raises:
        ConfigError: The file cannot be read, parsed or written.
    """

    def edit(document: tomlkit.TOMLDocument) -> None:
        document[name] = value

    return _edit(path, edit)


def _edit(path: Path | None, edit: Callable[[tomlkit.TOMLDocument], None]) -> Path:
    """Apply `edit` to the parsed file, an empty document standing in for a missing one."""
    where = path if path is not None else default_config_path()
    try:
        document = tomlkit.parse(where.read_text(encoding="utf-8"))
    except FileNotFoundError:
        document = tomlkit.document()
    except OSError as exc:
        msg = f"cannot read {where}: {exc.strerror or exc}"
        raise ConfigError(msg) from exc
    except (UnicodeDecodeError, ParseError) as exc:
        msg = f"{where} is not valid TOML: {exc}"
        raise ConfigError(msg) from exc
    edit(document)
    _write(where, tomlkit.dumps(document))
    return where


def _write(where: Path, text: str) -> None:
    """Write the file, readable by its owner only since it holds passwords."""
    try:
        where.parent.mkdir(parents=True, exist_ok=True)
        where.touch(mode=0o600)
        where.write_text(text, encoding="utf-8")
    except OSError as exc:
        msg = f"cannot write {where}: {exc.strerror or exc}"
        raise ConfigError(msg) from exc
