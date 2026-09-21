"""What the command line shows while it runs.

Three ways, picked by `setup()`: a compact live display of what is going on
right now, one printed line per work once it is over (the default); one
timestamped log line per step (`-v`); nothing but the warnings and the
errors (`-q`). Every message goes through `logger`; what is going on --
the episode being read, the pages written so far -- goes through a
`Display`, which the live display draws and the log writes down.
"""

from __future__ import annotations

import logging
from collections import Counter
from typing import TYPE_CHECKING

from rich.console import Console, Group
from rich.live import Live
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)

if TYPE_CHECKING:
    from .downloader import Result, Status
    from .extractor import Episode

#: Where every message of the command line goes.
logger = logging.getLogger("getjmanga")

#: What `-v` writes each line as.
LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

#: How the summary line names what became of an episode.
_LEAD: dict[Status, str] = {"saved": "saved", "exists": "skipped", "locked": "locked"}
_COUNTED: dict[Status, str] = {"saved": "saved", "exists": "skipped", "locked": "locked"}


def summary(results: list[Result]) -> tuple[int, str]:
    """One line for what became of a work, and the level to say it at.

    Args:
        results: Every episode of the work, in reading order.

    Returns:
        A warning when nothing was readable, else an info line: the
        episode's directory, or the series' directory with the counts.
    """
    if len(results) == 1:
        result = results[0]
        if result.status == "locked":
            return logging.WARNING, f"skip: '{result.episode.episode_title}' needs a purchase, a wait or a login."
        return logging.INFO, f"{_LEAD[result.status]}: {result.save_dir}"
    counts = Counter(result.status for result in results)
    lead = next(status for status in _LEAD if counts[status])
    rest = [f"{counts[status]} {_COUNTED[status]}" for status in _LEAD if status != lead and counts[status]]
    level = logging.WARNING if lead == "locked" else logging.INFO
    what = ", ".join([f"{counts[lead]} episodes", *rest])
    return level, f"{_LEAD[lead]}: {results[-1].save_dir.parent} ({what})"


class Display:
    """What the command line shows of what it is doing: nothing, until a subclass says otherwise.

    One work is a series, a chain or a single episode: `work()` opens it,
    `done()` closes it, and what happens in between is one `fetching()`,
    at most one run of `pages()`, and one `finished()` per episode.
    """

    def __init__(self) -> None:
        """Start with no work under way."""
        #: What became of every episode of the work under way.
        self.results: list[Result] = []

    def work(self, url: str) -> None:
        """A work starts: the episodes at `url` come next."""
        self.results = []

    def series(self, total: int) -> None:
        """The work is a series listing `total` episodes."""

    def fetching(self, url: str) -> None:
        """The episode page at `url` is being read."""

    def pages(self, episode: Episode, done: int, total: int) -> None:
        """`done` of the episode's `total` pages are written; called before the first and after each."""

    def finished(self, result: Result) -> None:
        """An episode is done with, whatever became of it."""
        self.results.append(result)

    def done(self) -> None:
        """The work is over, whether or not every episode was reached."""


class Log(Display):
    """`-v`: one log line per step, through `logger`."""

    def series(self, total: int) -> None:
        """Say how many episodes the series lists."""
        logger.info("series: %d episodes listed.", total)

    def fetching(self, url: str) -> None:
        """Say which episode is being read."""
        logger.info("get: %s", url)

    def pages(self, episode: Episode, done: int, total: int) -> None:
        """Say how many pages there are, then count them off."""
        if done == 0:
            logger.info("'%s': %d pages", episode.episode_title, total)
        else:
            logger.debug("page %d/%d", done, total)

    def finished(self, result: Result) -> None:
        """Say what became of the episode."""
        super().finished(result)
        if result.status == "locked":
            logger.warning("skip: '%s' needs a purchase, a wait or a login.", result.episode.episode_title)
            return
        logger.info("%s: %s", _LEAD[result.status], result.save_dir)
        if result.saved and result.archive is not None:
            logger.info("packed: %s", result.archive)

    def done(self) -> None:
        """Sum a work of several episodes up."""
        if len(self.results) > 1:
            logger.log(*summary(self.results))


class LiveDisplay(Display):
    """The default: a live line or two for what is going on, and one printed line per work once it is over.

    The live part only draws on a terminal; anywhere else, the printed
    lines are all there is.
    """

    def __init__(self) -> None:
        """Set up the console the live display and the lines share."""
        super().__init__()
        self.console = Console()
        self._live: Live | None = None
        self._title = ""
        self._total: int | None = None
        self._work = Progress()
        self._pages = Progress()
        self._work_task = TaskID(0)
        self._pages_task = TaskID(0)

    def work(self, url: str) -> None:
        """Open the live display, on `url` until an episode names the series."""
        super().work(url)
        self._title = ""
        self._total = None
        self._work = Progress(SpinnerColumn(), TextColumn("{task.description}", markup=False), TimeElapsedColumn())
        self._pages = Progress(
            TextColumn("  {task.description}", markup=False),
            BarColumn(),
            MofNCompleteColumn(),
            TextColumn("pages"),
            TimeRemainingColumn(),
        )
        self._work_task = self._work.add_task(url)
        self._pages_task = self._pages.add_task("", total=None, visible=False)
        self._live = Live(Group(self._work, self._pages), console=self.console, transient=True, refresh_per_second=10)
        self._live.start()

    def series(self, total: int) -> None:
        """Count the episodes off against `total` from here on."""
        self._total = total

    def fetching(self, url: str) -> None:
        """Show which episode is being read."""
        self._pages.update(self._pages_task, visible=False)
        self._show(f"get {url}")

    def pages(self, episode: Episode, done: int, total: int) -> None:
        """Show the episode and a bar of its pages."""
        self._title = episode.series_title
        if done == 0:
            self._show()
            self._pages.update(
                self._pages_task, description=episode.episode_title, total=total, completed=0, visible=True
            )
        else:
            self._pages.update(self._pages_task, completed=done)

    def finished(self, result: Result) -> None:
        """Count the episode and take its bar down."""
        super().finished(result)
        self._title = result.episode.series_title
        self._pages.update(self._pages_task, visible=False)

    def done(self) -> None:
        """Take the live display down and print the one line the work leaves behind."""
        if self._live is not None:
            self._live.stop()
            self._live = None
        if self.results:
            logger.log(*summary(self.results))

    def _show(self, *tail: str) -> None:
        """Redraw the work line: the series, which episode this is, and `tail`."""
        parts = [self._title] if self._title else []
        index = len(self.results) + 1
        if self._total is not None:
            parts.append(f"episode {index}/{self._total}")
        elif self.results:
            parts.append(f"episode {index}")
        self._work.update(self._work_task, description=" · ".join([*parts, *tail]))


class ConsoleHandler(logging.Handler):
    """Print each record as a line: the warnings and the errors on stderr, the rest on stdout.

    While a live display is up, its console routes both above it.
    """

    def __init__(self, console: Console) -> None:
        """Print through `console`, and its stderr counterpart."""
        super().__init__()
        self._out = console
        self._err = Console(stderr=True)

    def emit(self, record: logging.LogRecord) -> None:
        """Print the record, with `error:` in front of an error."""
        message = record.getMessage()
        if record.levelno >= logging.ERROR:
            message = f"error: {message}"
        console = self._err if record.levelno >= logging.WARNING else self._out
        console.print(message, markup=False, highlight=False, soft_wrap=True)


def setup(*, quiet: bool = False, verbose: bool = False) -> Display:
    """Route `logger` to the terminal the way the flags say, and hand back the display to report to.

    Args:
        quiet: Nothing but the warnings and the errors.
        verbose: One timestamped log line per step, on stderr, and
            httpx2's line per request; overrides `quiet`.

    Returns:
        The display for the command line to report what it is doing to.
    """
    requests = logging.getLogger("httpx2")
    for log in (logger, requests):
        log.handlers.clear()
    if verbose:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(LOG_FORMAT, LOG_DATE_FORMAT))
        logger.setLevel(logging.DEBUG)
        requests.setLevel(logging.INFO)
        for log in (logger, requests):
            log.addHandler(handler)
        return Log()
    requests.setLevel(logging.NOTSET)
    display = Display() if quiet else LiveDisplay()
    logger.setLevel(logging.WARNING if quiet else logging.INFO)
    logger.addHandler(ConsoleHandler(display.console if isinstance(display, LiveDisplay) else Console()))
    return display
