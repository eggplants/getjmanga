from __future__ import annotations

from pathlib import Path

import pytest

from getjmanga.config import Config, ConfigError, Credentials, default_config_path, load_config
from getjmanga.extractors import Comici, Fuz, GigaViewer, Piccoma

CONFIG = """
[site."shonenjumpplus.com"]
username = "jump@example.com"
password = "jump-pw"

[site.comici-plus]
username = "comici-id"

[site."takecomic.jp"]
username = "take@example.com"
password = "take-pw"

[site.piccoma]
username = "pic@example.com"
password = "pic-pw"
"""


def write(path: Path, text: str = CONFIG) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# --- where the file lives ---------------------------------------------------------


def test_default_path_follows_xdg_config_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert default_config_path() == tmp_path / "getjmanga" / "config.toml"


@pytest.mark.parametrize("value", ["", "relative/dir", None])
def test_default_path_falls_back_to_dot_config(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    else:
        monkeypatch.setenv("XDG_CONFIG_HOME", value)
    assert default_config_path() == Path.home() / ".config" / "getjmanga" / "config.toml"


# --- reading it --------------------------------------------------------------------


def test_load_config_reads_the_default_file(isolated_config):
    write(isolated_config)
    config = load_config()
    assert config.path == isolated_config
    assert config.sites["shonenjumpplus.com"] == Credentials("jump@example.com", "jump-pw")
    assert config.sites["comici-plus"] == Credentials("comici-id", None)


def test_load_config_reads_a_given_file(tmp_path):
    path = write(tmp_path / "elsewhere.toml")
    assert load_config(path).sites["piccoma"] == Credentials("pic@example.com", "pic-pw")


def test_load_config_is_empty_without_a_file(tmp_path):
    config = load_config(tmp_path / "missing.toml")
    assert config.sites == {}
    assert config.path is None


def test_load_config_ignores_other_tables(tmp_path):
    path = write(tmp_path / "c.toml", '[other]\nx = 1\n[site.piccoma]\nusername = "u"\n')
    assert list(load_config(path).sites) == ["piccoma"]


def test_load_config_rejects_bad_toml(tmp_path):
    path = write(tmp_path / "c.toml", "[site\n")
    with pytest.raises(ConfigError, match="not valid TOML"):
        load_config(path)


def test_load_config_rejects_a_directory(tmp_path):
    with pytest.raises(ConfigError, match="cannot read"):
        load_config(tmp_path)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("site = 1\n", r"\[site\] must be a table"),
        ("[site]\npiccoma = 1\n", "needs a username"),
        ('[site.piccoma]\npassword = "x"\n', "needs a username"),
        ('[site.piccoma]\nusername = ""\n', "needs a username"),
        ('[site.piccoma]\nusername = "u"\npassword = 1\n', "password must be a string"),
    ],
)
def test_load_config_rejects_a_misshapen_section(tmp_path, text, message):
    path = write(tmp_path / "c.toml", text)
    with pytest.raises(ConfigError, match=message):
        load_config(path)


# --- picking a section for a url -------------------------------------------------------


@pytest.fixture
def config(tmp_path):
    return load_config(write(tmp_path / "c.toml"))


def test_a_host_section_applies_to_its_host(config):
    assert config.credentials(GigaViewer, "https://shonenjumpplus.com/episode/1") == Credentials(
        "jump@example.com",
        "jump-pw",
    )


def test_a_host_without_a_section_gets_nothing_on_a_per_site_extractor(config):
    assert config.credentials(GigaViewer, "https://comic-days.com/episode/1") is None


def test_the_shared_section_applies_to_every_host_of_the_extractor(config):
    assert config.credentials(Comici, "https://mangabu.jp/episodes/1") == Credentials("comici-id", None)
    assert config.credentials(Comici, "https://younganimal.com/episodes/1") == Credentials("comici-id", None)


def test_a_host_section_beats_the_shared_one(config):
    assert config.credentials(Comici, "https://takecomic.jp/episodes/1") == Credentials("take@example.com", "take-pw")


def test_single_site_extractors_have_a_shared_key(config):
    assert config.credentials(Piccoma, "https://piccoma.com/web/viewer/1/2") == Credentials("pic@example.com", "pic-pw")
    assert config.credentials(Fuz, "https://comic-fuz.com/manga/viewer/1") is None


def test_an_empty_config_yields_nothing():
    assert Config().credentials(Comici, "https://mangabu.jp/episodes/1") is None
