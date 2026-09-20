"""Command line entry point for getjmanga."""

from __future__ import annotations

import getpass
import shutil
import sys
from argparse import (
    Action,
    ArgumentDefaultsHelpFormatter,
    ArgumentParser,
    BooleanOptionalAction,
    Namespace,
    RawDescriptionHelpFormatter,
)
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from httpx import HTTPError

from . import __version__
from .config import (
    Config,
    Credentials,
    default_config_path,
    init_config,
    load_config,
    set_option,
    set_site,
)
from .downloader import Downloader
from .errors import GetjmangaError, NotAnEpisodePageError
from .extractors import EXTRACTORS, find_extractor, get_extractor
from .session import make_session

if TYPE_CHECKING:
    from .extractor import Extractor


#: What `jm config` answers to on the command line.
CONFIG_COMMANDS = ("config", "c")


class HelpFormatter(ArgumentDefaultsHelpFormatter, RawDescriptionHelpFormatter):
    """Show argument defaults while keeping the description's own line breaks."""

    def _get_help_string(self, action: Action) -> str | None:
        # Neither a positional nor an option that defaults to nothing has a default worth printing.
        if not action.option_strings or action.default is None:
            return action.help
        return super()._get_help_string(action)


def make_parser(prog: str, description: str, epilog: str | None = None) -> ArgumentParser:
    """An `ArgumentParser` laid out the way every command here is."""
    return ArgumentParser(
        prog=prog,
        description=description,
        epilog=epilog,
        formatter_class=lambda prog: HelpFormatter(
            prog,
            width=shutil.get_terminal_size(fallback=(120, 50)).columns,
            max_help_position=40,
        ),
    )


def extractor_list() -> str:
    """Render every extractor with the URL shapes and the hosts it takes."""
    lines: list[str] = []
    for extractor in EXTRACTORS:
        lines.append(f"{extractor.NAME}:")
        if extractor.CONFIG_KEY:
            lines.append(f"  config: [site.{extractor.CONFIG_KEY}]")
        lines.append("  urls:")
        lines.extend(f"    - {form}" for form in extractor.URL_FORMS)
        lines.append("  hosts:")
        lines.extend(f"    - https://{host}" for host in extractor.HOSTS)
    return "\n".join(lines)


def parse_args(args: list[str] | None = None) -> Namespace:
    """Parse the command line.

    Args:
        args: Arguments to parse instead of `sys.argv[1:]`. Used by the tests.

    Returns:
        The parsed arguments.
    """
    parser = make_parser(
        "getjmanga",
        "Retrieve and save images from japanese web comic sites",
        epilog="extractors: "
        + ", ".join(extractor.NAME for extractor in EXTRACTORS)
        + "\n\nconfig: `%(prog)s config --help` sets up the config file",
    )
    parser.add_argument("urls", metavar="url", nargs="*", help="episode url, or a series url to take every episode of")
    # `-b`, `-d` and `-o` default to None so that the config file can fill them in.
    parser.add_argument("-b", "--bulk", action=BooleanOptionalAction, help="follow every next episode")
    parser.add_argument(
        "-d", "--savedir", metavar="DIR", help="directory to save into (default: the config's savedir, else .)"
    )
    parser.add_argument("-f", "--first", action="store_true", help="download only the first page")
    parser.add_argument("-o", "--overwrite", action=BooleanOptionalAction, help="download again if it exists")
    parser.add_argument("-m", "--metadata", action="store_true", help="save episode metadata as json")
    parser.add_argument("-u", "--username", metavar="ID", help="id or email address to log in with")
    parser.add_argument("-p", "--password", metavar="PW", help="password (prompted for if -u is given without it)")
    parser.add_argument(
        "-e",
        "--extractor",
        metavar="NAME",
        help="use this extractor instead of picking one by the url's host",
    )
    parser.add_argument(
        "-c",
        "--config",
        metavar="FILE",
        type=Path,
        help=f"config file holding site credentials (default: {default_config_path()})",
    )
    parser.add_argument("-q", "--quiet", action="store_true", help="disable console output")
    parser.add_argument("--list-extractors", action="store_true", help="list every extractor and exit")
    parser.add_argument("-V", "--version", action="version", version=f"%(prog)s {__version__}")
    if not args:
        parser.print_help()
        raise SystemExit(0)
    parsed = parser.parse_args(args)
    if not parsed.urls and not parsed.list_extractors:
        parser.error("the following arguments are required: url")
    return parsed


def apply_config(parsed: Namespace, config: Config) -> None:
    """Fill in the flags the command line left out from the config file.

    Args:
        parsed: The parsed command line, updated in place.
        config: The config file.
    """
    if parsed.savedir is None:
        parsed.savedir = config.savedir if config.savedir is not None else "."
    if parsed.overwrite is None:
        parsed.overwrite = config.overwrite
    if parsed.bulk is None:
        parsed.bulk = config.bulk


def parse_config_args(args: list[str]) -> Namespace:
    """Parse `jm config ...`.

    Args:
        args: What came after `config`.

    Returns:
        The parsed arguments; `command` names the subcommand.
    """
    parser = make_parser("getjmanga config", "Set up the config file")
    parser.add_argument(
        "-c",
        "--config",
        metavar="FILE",
        type=Path,
        help=f"config file to edit (default: {default_config_path()})",
    )
    commands = parser.add_subparsers(dest="command", metavar="command", required=True)
    commands.add_parser("init", help="create the file from a commented template")
    site = commands.add_parser("site", help="set an account, asking for the username and password")
    site.add_argument("key", help="the site's host, or the key `getjmanga --list-extractors` prints")
    savedir = commands.add_parser("savedir", help="set what -d defaults to")
    savedir.add_argument("dir", type=Path, help="directory to save into")
    for name in ("overwrite", "bulk"):
        flag = commands.add_parser(name, help=f"set whether -{name[0]} is on by default")
        flag.add_argument("value", choices=("true", "false"))
    if not args:
        parser.print_help()
        raise SystemExit(0)
    return parser.parse_args(args)


def known_site_keys() -> set[str]:
    """Every `[site.<key>]` key some extractor reads: its hosts and its `CONFIG_KEY`."""
    keys: set[str] = set()
    for extractor in EXTRACTORS:
        keys.update(extractor.HOSTS)
        if extractor.CONFIG_KEY:
            keys.add(extractor.CONFIG_KEY)
    return keys


def ask_credentials(key: str) -> Credentials:
    """Prompt for the account to write under `[site.<key>]`.

    Raises:
        SystemExit: No extractor reads the key, no username was typed, or the input ended.
    """
    if key not in known_site_keys():
        print(f"error: no extractor reads [site.{key}]; see `getjmanga --list-extractors`.", file=sys.stderr)
        raise SystemExit(1)
    try:
        username = input(f"username for {key}: ").strip()
        if not username:
            print("error: a username is needed.", file=sys.stderr)
            raise SystemExit(1)
        password = getpass.getpass("password (leave empty to be prompted for it each run): ")
    except EOFError:
        print("error: aborted.", file=sys.stderr)
        raise SystemExit(1) from None
    # An empty password means "ask at run time", the way a section without one does.
    return Credentials(username, password or None)


def config_main(args: list[str]) -> None:
    """Run `jm config ...`.

    Raises:
        SystemExit: The file could not be written, or no username was typed.
    """
    parsed = parse_config_args(args)
    try:
        if parsed.command == "init":
            path = init_config(parsed.config)
            print("created:", path)
        elif parsed.command == "site":
            path = set_site(parsed.key, ask_credentials(parsed.key), parsed.config)
            print(f"saved: [site.{parsed.key}] in {path}")
        elif parsed.command == "savedir":
            path = set_option("savedir", str(parsed.dir.expanduser().absolute()), parsed.config)
            print(f"saved: savedir in {path}")
        else:
            path = set_option(parsed.command, parsed.value == "true", parsed.config)
            print(f"saved: {parsed.command} in {path}")
    except GetjmangaError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


def episode_urls(extractor: Extractor, url: str, *, quiet: bool) -> list[str]:
    """The episodes to download: everything a series lists, or the URL itself.

    Args:
        extractor: The extractor to read the series with.
        url: The URL given on the command line.
        quiet: Print nothing.

    Returns:
        One episode URL per download.
    """
    if not extractor.is_series(url):
        return [url]
    urls = extractor.series_urls(url)
    if not quiet:
        print(f"series: {len(urls)} episodes listed.")
    return urls


def download(downloader: Downloader, queue: list[str], parsed: Namespace, *, series: bool) -> int:
    """Download every queued episode.

    Args:
        downloader: The downloader to run.
        queue: The episodes to download, extended with the next episode of each
            when `-b` walks a chain.
        parsed: The parsed command line.
        series: The queue came from a series listing, whose episodes stand on
            their own, so the next episode is never followed.

    Returns:
        How many episodes were downloaded or found already there.
    """
    done = 0
    seen: set[str] = set()
    while queue:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        if not parsed.quiet:
            print("get:", url)
        try:
            result = downloader.download(url)
        except NotAnEpisodePageError:
            # Locked episodes on some sites serve a purchase page with no viewer
            # on it, which is where a bulk run is meant to end rather than fail.
            if not series and not done:
                raise
            print(
                f"skip: {url} is not readable." if series else "stop: the next episode is not readable.",
                file=sys.stderr,
            )
            if series:
                continue
            break

        if result.status == "locked":
            print(f"skip: '{result.episode.episode_title}' needs a purchase, a wait or a login.", file=sys.stderr)
        else:
            done += 1
            if not parsed.quiet:
                print("saved:" if result.saved else "skipped (already there):", result.save_dir)
        if parsed.bulk and not series and result.episode.next_url:
            queue.append(result.episode.next_url)
    return done


class Runner:
    """Drive one command line: pick extractors, sign in once per site, download."""

    def __init__(self, parsed: Namespace, config: Config, password: str | None) -> None:
        """Build a runner.

        Args:
            parsed: The parsed command line.
            config: The config file, for the credentials `-u` did not give.
            password: The password that goes with `-u`, once `-p` and the prompt are settled.
        """
        self.parsed = parsed
        self.config = config
        self.password = password
        self.session = make_session()
        # One instance per extractor class, so a chain of episodes shares its
        # cookies and a series on the same site is signed in to once.
        self._extractors: dict[type[Extractor], Extractor] = {}
        self._logged_in: set[tuple[str, str]] = set()
        self._prompted: dict[str, str] = {}

    def extractor(self, url: str) -> Extractor:
        """The extractor for `url`: the one `-e` named, or the first that takes it."""
        cls = get_extractor(self.parsed.extractor) if self.parsed.extractor else find_extractor(url)
        if cls not in self._extractors:
            self._extractors[cls] = cls(self.session)
        return self._extractors[cls]

    def credentials(self, extractor: Extractor, url: str) -> Credentials | None:
        """What to sign in to `url` with: `-u`/`-p` when given, else the config file's section."""
        if self.parsed.username:
            return Credentials(self.parsed.username, self.password)
        return self.config.credentials(type(extractor), url)

    def login(self, extractor: Extractor, url: str) -> None:
        """Sign in to the site `url` is on, unless that already happened or nothing says how."""
        credentials = self.credentials(extractor, url)
        if credentials is None:
            return
        key = (extractor.NAME, urlparse(url).netloc)
        if key in self._logged_in:
            return
        password = credentials.password
        if password is None:
            # A section without a password is asked for it once per account.
            if credentials.username not in self._prompted:
                self._prompted[credentials.username] = getpass.getpass(f"password for {credentials.username}: ")
            password = self._prompted[credentials.username]
        extractor.login(url, credentials.username, password)
        self._logged_in.add(key)
        if not self.parsed.quiet:
            print("logged in as:", credentials.username)

    def run(self, url: str) -> None:
        """Download `url`: the episode, or every episode of the series.

        Raises:
            SystemExit: Nothing in the series was readable.
        """
        parsed = self.parsed
        extractor = self.extractor(url)
        self.login(extractor, url)

        # A series listing already names every episode, so there is no next episode to follow.
        series = extractor.is_series(url)
        if series and parsed.bulk:
            print("warning: -b does nothing for a series, every listed episode is downloaded.", file=sys.stderr)
        downloader = Downloader(
            extractor,
            parsed.savedir,
            overwrite=parsed.overwrite,
            only_first=parsed.first,
            save_metadata=parsed.metadata,
            progress=not parsed.quiet,
        )
        queue = episode_urls(extractor, url, quiet=parsed.quiet)
        done = download(downloader, queue, parsed, series=series)
        if series and not done:
            print(f"error: no episode in the series at {url} was readable.", file=sys.stderr)
            raise SystemExit(1)


def main(args: list[str] | None = None) -> None:
    """Run the command."""
    argv = sys.argv[1:] if args is None else args
    if argv and argv[0] in CONFIG_COMMANDS:
        config_main(argv[1:])
        return
    parsed = parse_args(argv)
    if parsed.list_extractors:
        print(extractor_list())
        return

    password = parsed.password
    if parsed.username and password is None:
        password = getpass.getpass("password: ")
    elif password and not parsed.username:
        print("warning: -p without -u does nothing.", file=sys.stderr)

    try:
        config = load_config(parsed.config)
        apply_config(parsed, config)
        runner = Runner(parsed, config, password)
        for url in parsed.urls:
            runner.run(url)
    except (GetjmangaError, HTTPError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    if not parsed.quiet:
        print("done.")


if __name__ == "__main__":
    main()
