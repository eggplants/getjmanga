"""Command line entry point for getjmanga."""

from __future__ import annotations

import getpass
import shutil
import sys
from argparse import Action, ArgumentDefaultsHelpFormatter, ArgumentParser, Namespace, RawDescriptionHelpFormatter
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from requests import RequestException

from . import __version__
from .config import Config, Credentials, default_config_path, load_config
from .downloader import Downloader
from .extractors import EXTRACTORS, GetjmangaError, NotAnEpisodePageError, find_extractor, get_extractor
from .session import make_session

if TYPE_CHECKING:
    from .extractors.common import Extractor


class HelpFormatter(ArgumentDefaultsHelpFormatter, RawDescriptionHelpFormatter):
    """Show argument defaults while keeping the description's own line breaks."""

    def _get_help_string(self, action: Action) -> str | None:
        # Neither a positional nor an option that defaults to nothing has a default worth printing.
        if not action.option_strings or action.default is None:
            return action.help
        return super()._get_help_string(action)


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
    parser = ArgumentParser(
        prog="getjmanga",
        description="Retrieve and save images from japanese web comic sites",
        epilog="extractors: " + ", ".join(extractor.NAME for extractor in EXTRACTORS),
        formatter_class=lambda prog: HelpFormatter(
            prog,
            width=shutil.get_terminal_size(fallback=(120, 50)).columns,
            max_help_position=40,
        ),
    )
    parser.add_argument("urls", metavar="url", nargs="*", help="episode url, or a series url to take every episode of")
    parser.add_argument("-b", "--bulk", action="store_true", help="follow every next episode")
    parser.add_argument("-d", "--savedir", metavar="DIR", default=".", help="directory to save into")
    parser.add_argument("-f", "--first", action="store_true", help="download only the first page")
    parser.add_argument("-o", "--overwrite", action="store_true", help="download again if it exists")
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
    parsed = parser.parse_args(args)
    if not parsed.urls and not parsed.list_extractors:
        parser.error("the following arguments are required: url")
    return parsed


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
    parsed = parse_args(args)
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
        runner = Runner(parsed, config, password)
        for url in parsed.urls:
            runner.run(url)
    except (GetjmangaError, RequestException) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    if not parsed.quiet:
        print("done.")


if __name__ == "__main__":
    main()
