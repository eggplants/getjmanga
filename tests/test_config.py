from __future__ import annotations

from pathlib import Path

import pytest

from getjmanga.config import (
    Config,
    ConfigError,
    Credentials,
    Work,
    default_config_path,
    init_config,
    load_config,
    set_option,
    set_site,
    store_work,
)
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


def test_load_config_reads_the_defaults(tmp_path):
    path = write(tmp_path / "c.toml", 'savedir = "~/manga"\noverwrite = true\nbulk = true\n')
    config = load_config(path)
    assert (config.savedir, config.overwrite, config.bulk, config.both) == (Path.home() / "manga", True, True, False)


def test_load_config_refuses_bulk_and_both_together(tmp_path):
    path = write(tmp_path / "c.toml", "bulk = true\nboth = true\n")
    with pytest.raises(ConfigError, match="cannot both be true"):
        load_config(path)


def test_load_config_leaves_the_defaults_alone_when_unset(tmp_path):
    config = load_config(write(tmp_path / "c.toml", ""))
    assert (config.savedir, config.overwrite, config.bulk) == (None, False, False)


def test_load_config_reads_the_patrol_entries(tmp_path):
    path = write(
        tmp_path / "c.toml",
        '[[patrol]]\nurl = "https://a/1"\ntitle = "A"\n\n[[patrol]]\nurl = "https://b/"\nsearch = true\n',
    )
    assert load_config(path).patrol == (Work("https://a/1", "A"), Work("https://b/", search=True))


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
        ("savedir = 1\n", "savedir must be a string"),
        ('overwrite = "yes"\n', "overwrite must be a boolean"),
        ("bulk = 1\n", "bulk must be a boolean"),
        ("both = 1\n", "both must be a boolean"),
        ("patrol = 1\n", "must be an array"),
        ("[[patrol]]\ntitle = 'x'\n", "needs a url"),
        ("[[patrol]]\nurl = 'https://a/'\nsearch = 1\n", "search a boolean"),
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


# --- writing it ------------------------------------------------------------------------


def test_init_config_writes_a_readable_template_owner_only(isolated_config):
    assert init_config() == isolated_config
    assert isolated_config.stat().st_mode & 0o777 == 0o600
    config = load_config()
    assert (config.savedir, config.overwrite, config.bulk, dict(config.sites)) == (Path(), False, False, {})


def test_init_config_refuses_an_existing_file(tmp_path):
    path = write(tmp_path / "c.toml")
    with pytest.raises(ConfigError, match="already exists"):
        init_config(path)
    assert path.read_text(encoding="utf-8") == CONFIG


def test_set_site_creates_the_file(tmp_path):
    path = tmp_path / "new" / "c.toml"
    assert set_site("piccoma", Credentials("me", "pw"), path) == path
    assert load_config(path).sites == {"piccoma": Credentials("me", "pw")}


def test_set_site_keeps_the_other_sections_and_comments(tmp_path):
    path = write(tmp_path / "c.toml", "# keep me\n" + CONFIG)
    set_site("piccoma", Credentials("new", None), path)
    text = path.read_text(encoding="utf-8")
    assert text.startswith("# keep me\n")
    sites = load_config(path).sites
    assert sites["piccoma"] == Credentials("new", None)
    assert sites["shonenjumpplus.com"] == Credentials("jump@example.com", "jump-pw")


def test_set_site_quotes_a_host_key(tmp_path):
    path = set_site("shonenjumpplus.com", Credentials("me", "pw"), tmp_path / "c.toml")
    assert '[site."shonenjumpplus.com"]' in path.read_text(encoding="utf-8")


def test_set_option_replaces_the_template_line_in_place(isolated_config):
    init_config()
    set_option("savedir", "/manga")
    set_option("bulk", True)
    text = isolated_config.read_text(encoding="utf-8")
    assert text.index('savedir = "/manga"') < text.index("bulk = true") < text.index("# [site.")
    config = load_config()
    assert (config.savedir, config.bulk) == (Path("/manga"), True)


def test_set_option_turns_the_other_chain_flag_off(tmp_path):
    path = write(tmp_path / "c.toml", "bulk = true  # mine\n")
    set_option("both", True, path)
    assert path.read_text(encoding="utf-8") == "bulk = false  # mine\nboth = true\n"
    set_option("both", False, path)
    assert load_config(path).bulk is False  # turning one off leaves the other alone


def test_set_option_goes_before_the_site_sections(tmp_path):
    path = write(tmp_path / "c.toml")
    set_option("overwrite", True, path)
    text = path.read_text(encoding="utf-8")
    assert text.index("overwrite = true") < text.index("[site.")
    assert load_config(path).overwrite is True


def test_writes_refuse_a_broken_file(tmp_path):
    path = write(tmp_path / "c.toml", "[site\n")
    with pytest.raises(ConfigError, match="not valid TOML"):
        set_option("bulk", True, path)


def test_writes_refuse_a_directory(tmp_path):
    with pytest.raises(ConfigError, match="cannot read"):
        set_option("bulk", True, tmp_path)


def test_store_work_appends_and_then_updates_in_place(tmp_path):
    path = write(tmp_path / "c.toml")
    store_work(Work("https://a/1", "A"), path)
    store_work(Work("https://b/", search=True), path)
    store_work(Work("https://a/2", "A"), path, replacing="https://a/1")
    store_work(Work("https://b/", search=True), path)
    text = path.read_text(encoding="utf-8")
    assert text.startswith(CONFIG)
    assert text.endswith(
        '\n[[patrol]]\nurl = "https://a/2"\ntitle = "A"\n\n[[patrol]]\nurl = "https://b/"\nsearch = true\n'
    )
    assert load_config(path).patrol == (Work("https://a/2", "A"), Work("https://b/", search=True))


def test_store_work_keeps_a_hand_written_inline_array(tmp_path):
    path = write(tmp_path / "c.toml", 'patrol = [\n  { url = "https://a/1", title = "A" },\n]\n')
    store_work(Work("https://a/2", "A"), path, replacing="https://a/1")
    store_work(Work("https://b/", search=True), path)
    text = path.read_text(encoding="utf-8")
    assert "[[patrol]]" not in text
    assert load_config(path).patrol == (Work("https://a/2", "A"), Work("https://b/", search=True))


def test_store_work_starts_a_missing_file(tmp_path):
    path = tmp_path / "new" / "c.toml"
    store_work(Work("https://a/1"), path)
    assert path.read_text(encoding="utf-8") == '[[patrol]]\nurl = "https://a/1"\n'
