from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import pytest

from getjmanga import __version__
from getjmanga.cli import download, extractor_list, main, parse_args
from getjmanga.downloader import Downloader
from getjmanga.extractors.common import Episode, Extractor, LoginError, NotAnEpisodePageError, Page


def test_parse_args_defaults():
    parsed = parse_args(["https://mangabu.jp/episodes/1"])
    assert parsed.urls == ["https://mangabu.jp/episodes/1"]
    assert parsed.savedir == "."
    assert parsed.extractor is None
    assert (parsed.bulk, parsed.first, parsed.overwrite, parsed.metadata, parsed.quiet) == (
        False,
        False,
        False,
        False,
        False,
    )


def test_parse_args_takes_several_urls():
    parsed = parse_args(["https://a/1", "https://b/2", "-e", "comici"])
    assert parsed.urls == ["https://a/1", "https://b/2"]
    assert parsed.extractor == "comici"


def test_parse_args_wants_a_url_unless_listing(capsys):
    with pytest.raises(SystemExit) as excinfo:
        parse_args([])
    assert excinfo.value.code == 2
    assert "required: url" in capsys.readouterr().err


def test_help_prints_defaults_for_options_only(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["--help"])
    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert "(default: None)" not in out
    assert "--savedir DIR" in out
    assert "(default: .)" in out


def test_version_flag_prints_the_version(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_list_extractors_names_every_extractor_and_its_hosts(capsys):
    main(["--list-extractors"])
    out = capsys.readouterr().out
    assert out == extractor_list() + "\n"
    assert "gigaviewer:" in out
    assert "comici:" in out
    assert "piccoma:" in out
    assert "https://shonenjumpplus.com" in out
    assert "https://<host>/episodes/<id>" in out
    assert "  config: [site.comici-plus]" in out
    assert "  config: [site.piccoma]" in out


class Recording(Extractor):
    """Walks a canned chain of three episodes, the second of which may be locked."""

    NAME = "recording"
    HOSTS = ("mangabu.jp",)
    instances: ClassVar[list[Recording]] = []
    locked: ClassVar[set[str]] = set()
    missing: ClassVar[set[str]] = set()

    def __init__(self, session=None):
        super().__init__(session)
        self.episodes: list[str] = []
        self.logins: list[tuple[str, str, str]] = []
        self.images = 0
        Recording.instances.append(self)

    @classmethod
    def is_series(cls, url):
        return "/series/" in url

    def series_urls(self, url):
        return [f"https://mangabu.jp/episodes/feed{index}" for index in range(3)]

    def login(self, url, username, password):
        if password == "wrong":
            msg = "refused"
            raise LoginError(msg)
        self.logins.append((url, username, password))

    def episode(self, url):
        self.episodes.append(url)
        if url in Recording.missing:
            msg = f"no viewer on {url}"
            raise NotAnEpisodePageError(msg)
        index = len(self.episodes)
        next_url = f"https://mangabu.jp/episodes/{index}" if index < 3 else None
        pages = () if url in Recording.locked else (Page(url=f"{url}/0.jpg"),)
        return Episode(url=url, series_title="S", episode_title=f"ep{index}", pages=pages, next_url=next_url)

    def image(self, page, episode):
        from PIL import Image  # noqa: PLC0415

        self.images += 1
        return Image.new("RGB", (2, 2))


@pytest.fixture
def recording(monkeypatch, tmp_path):
    Recording.instances.clear()
    Recording.locked = set()
    Recording.missing = set()
    Recording.CONFIG_KEY = ""
    monkeypatch.setattr("getjmanga.cli.find_extractor", lambda url: Recording)
    monkeypatch.setattr("getjmanga.cli.get_extractor", lambda name: Recording)
    monkeypatch.chdir(tmp_path)
    return Recording


def test_main_downloads_a_single_episode(recording, capsys, tmp_path):
    main(["https://mangabu.jp/episodes/0"])

    extractor = recording.instances[0]
    assert extractor.episodes == ["https://mangabu.jp/episodes/0"]
    assert extractor.images == 1
    assert (tmp_path / "S" / "ep1" / "0.jpg").exists()
    out = capsys.readouterr().out
    assert "get: https://mangabu.jp/episodes/0" in out
    assert "saved:" in out
    assert "done." in out


def test_bulk_follows_the_next_episode_chain(recording):
    main(["-b", "https://mangabu.jp/episodes/0"])
    assert recording.instances[0].episodes == [
        "https://mangabu.jp/episodes/0",
        "https://mangabu.jp/episodes/1",
        "https://mangabu.jp/episodes/2",
    ]


def test_bulk_steps_over_a_locked_episode(recording, capsys):
    recording.locked = {"https://mangabu.jp/episodes/1"}
    main(["-b", "https://mangabu.jp/episodes/0"])

    assert len(recording.instances[0].episodes) == 3
    assert "skip: 'ep2' needs a purchase" in capsys.readouterr().err


def test_bulk_stops_where_the_chain_stops_being_readable(recording, capsys):
    recording.missing = {"https://mangabu.jp/episodes/1"}
    main(["-b", "https://mangabu.jp/episodes/0"])

    assert recording.instances[0].episodes == ["https://mangabu.jp/episodes/0", "https://mangabu.jp/episodes/1"]
    assert "stop: the next episode is not readable." in capsys.readouterr().err


def test_a_first_url_without_a_viewer_fails(recording, capsys):
    recording.missing = {"https://mangabu.jp/episodes/0"}
    with pytest.raises(SystemExit) as excinfo:
        main(["https://mangabu.jp/episodes/0"])
    assert excinfo.value.code == 1
    assert "error: no viewer on" in capsys.readouterr().err


def test_a_series_downloads_every_episode_it_lists(recording, capsys):
    main(["https://mangabu.jp/series/x"])

    assert recording.instances[0].episodes == [
        "https://mangabu.jp/episodes/feed0",
        "https://mangabu.jp/episodes/feed1",
        "https://mangabu.jp/episodes/feed2",
    ]
    assert "series: 3 episodes listed." in capsys.readouterr().out


def test_a_series_skips_what_it_cannot_read_and_warns_about_bulk(recording, capsys):
    recording.missing = {"https://mangabu.jp/episodes/feed1"}
    main(["-b", "https://mangabu.jp/series/x"])

    err = capsys.readouterr().err
    assert "-b does nothing for a series" in err
    assert "skip: https://mangabu.jp/episodes/feed1 is not readable." in err
    assert len(recording.instances[0].episodes) == 3


def test_a_series_with_nothing_readable_fails(recording, capsys):
    recording.locked = {f"https://mangabu.jp/episodes/feed{index}" for index in range(3)}
    with pytest.raises(SystemExit) as excinfo:
        main(["https://mangabu.jp/series/x"])
    assert excinfo.value.code == 1
    assert "no episode in the series" in capsys.readouterr().err


def test_several_urls_share_one_extractor(recording):
    main(["https://mangabu.jp/episodes/0", "https://mangabu.jp/episodes/9"])
    assert len(recording.instances) == 1
    assert recording.instances[0].episodes == ["https://mangabu.jp/episodes/0", "https://mangabu.jp/episodes/9"]


def test_an_existing_episode_is_skipped(recording, capsys, tmp_path):
    (tmp_path / "S" / "ep1").mkdir(parents=True)
    main(["https://mangabu.jp/episodes/0"])
    assert recording.instances[0].images == 0
    assert "skipped (already there):" in capsys.readouterr().out


def test_login_happens_once_per_site(recording, capsys):
    main(["-u", "me", "-p", "pw", "https://mangabu.jp/episodes/0", "https://mangabu.jp/episodes/9"])

    assert recording.instances[0].logins == [("https://mangabu.jp/episodes/0", "me", "pw")]
    assert capsys.readouterr().out.count("logged in as: me") == 1


def test_login_prompts_for_a_missing_password(recording, monkeypatch):
    monkeypatch.setattr("getjmanga.cli.getpass.getpass", lambda _prompt: "typed")
    main(["-u", "me", "https://mangabu.jp/episodes/0"])
    assert recording.instances[0].logins == [("https://mangabu.jp/episodes/0", "me", "typed")]


def test_a_refused_login_fails(recording, capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["-u", "me", "-p", "wrong", "https://mangabu.jp/episodes/0"])
    assert excinfo.value.code == 1
    assert "error: refused" in capsys.readouterr().err


def test_password_without_username_warns(recording, capsys):
    main(["-p", "pw", "https://mangabu.jp/episodes/0"])
    assert "-p without -u does nothing" in capsys.readouterr().err
    assert recording.instances[0].logins == []


def test_quiet_prints_nothing(recording, capsys):
    main(["-q", "https://mangabu.jp/episodes/0"])
    assert capsys.readouterr().out == ""


def test_an_unsupported_url_fails(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["https://example.com/episodes/1"])
    assert excinfo.value.code == 1
    assert "no extractor takes" in capsys.readouterr().err


def test_an_unknown_extractor_name_fails(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["-e", "nope", "https://example.com/episodes/1"])
    assert excinfo.value.code == 1
    assert "no extractor is named 'nope'" in capsys.readouterr().err


def test_download_never_visits_a_url_twice(tmp_path):
    extractor = Recording()
    parsed = parse_args(["-b", "-q", "https://mangabu.jp/episodes/2"])
    downloader = Downloader(extractor, tmp_path)

    # Started at episode 2, the fake names episode 1 next, which names episode 2 again -- a loop.
    done = download(downloader, ["https://mangabu.jp/episodes/2"], parsed, series=False)

    assert done == 2
    assert extractor.episodes == ["https://mangabu.jp/episodes/2", "https://mangabu.jp/episodes/1"]
    assert isinstance(downloader.save_path, Path)


# --- the config file --------------------------------------------------------------------


def write_config(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_the_config_file_signs_in_by_host(recording, isolated_config, capsys):
    write_config(isolated_config, '[site."mangabu.jp"]\nusername = "cfg"\npassword = "cfg-pw"\n')
    main(["https://mangabu.jp/episodes/0"])
    assert recording.instances[0].logins == [("https://mangabu.jp/episodes/0", "cfg", "cfg-pw")]
    assert "logged in as: cfg" in capsys.readouterr().out


def test_the_config_file_signs_in_by_the_shared_key(recording, isolated_config):
    recording.CONFIG_KEY = "recording-shared"
    write_config(isolated_config, '[site.recording-shared]\nusername = "shared"\npassword = "pw"\n')
    main(["https://mangabu.jp/episodes/0"])
    assert recording.instances[0].logins == [("https://mangabu.jp/episodes/0", "shared", "pw")]


def test_the_command_line_beats_the_config_file(recording, isolated_config):
    write_config(isolated_config, '[site."mangabu.jp"]\nusername = "cfg"\npassword = "cfg-pw"\n')
    main(["-u", "me", "-p", "pw", "https://mangabu.jp/episodes/0"])
    assert recording.instances[0].logins == [("https://mangabu.jp/episodes/0", "me", "pw")]


def test_a_config_section_without_a_password_prompts_once(recording, isolated_config, monkeypatch):
    write_config(isolated_config, '[site."mangabu.jp"]\nusername = "cfg"\n')
    prompts = []
    monkeypatch.setattr("getjmanga.cli.getpass.getpass", lambda prompt: prompts.append(prompt) or "typed")
    main(["https://mangabu.jp/episodes/0", "https://mangabu.jp/episodes/9"])
    assert recording.instances[0].logins == [("https://mangabu.jp/episodes/0", "cfg", "typed")]
    assert prompts == ["password for cfg: "]


def test_a_host_without_a_section_is_not_signed_in(recording, isolated_config):
    write_config(isolated_config, '[site."other.example"]\nusername = "cfg"\npassword = "pw"\n')
    main(["https://mangabu.jp/episodes/0"])
    assert recording.instances[0].logins == []


def test_dash_c_names_another_config_file(recording, tmp_path):
    path = write_config(tmp_path / "alt.toml", '[site."mangabu.jp"]\nusername = "alt"\npassword = "pw"\n')
    main(["-c", str(path), "https://mangabu.jp/episodes/0"])
    assert recording.instances[0].logins == [("https://mangabu.jp/episodes/0", "alt", "pw")]


def test_a_broken_config_file_fails(recording, isolated_config, capsys):
    write_config(isolated_config, "[site\n")
    with pytest.raises(SystemExit) as excinfo:
        main(["https://mangabu.jp/episodes/0"])
    assert excinfo.value.code == 1
    assert "not valid TOML" in capsys.readouterr().err
    assert recording.instances == []
