"""Save the pages an extractor hands over, one directory per episode."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pathvalidate import sanitize_filename
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)

if TYPE_CHECKING:
    from .extractor import Episode, Extractor

#: What became of an episode: written, left alone because it was already
#: there, or locked behind a purchase, a wait or a login.
Status = Literal["saved", "exists", "locked"]


@dataclass(frozen=True)
class Result:
    """What `Downloader.download()` did with one episode."""

    episode: Episode
    save_dir: Path
    status: Status

    @property
    def saved(self) -> bool:
        """Whether pages were written this time."""
        return self.status == "saved"


class Downloader:
    """Write episodes to `<save_path>/<series>/<episode>/<page>.jpg`."""

    def __init__(
        self,
        extractor: Extractor,
        save_path: str | Path = ".",
        *,
        overwrite: bool = False,
        only_first: bool = False,
        save_metadata: bool = False,
        progress: bool = False,
    ) -> None:
        """Build a downloader.

        Args:
            extractor: The extractor to read episodes with.
            save_path: Directory to build `<series>/<episode>/` under.
            overwrite: Download again even if the directory already exists.
            only_first: Stop after the first page.
            save_metadata: Also write `metadata.json` next to the pages.
            progress: Draw a progress bar.
        """
        self.extractor = extractor
        self.save_path = Path(save_path)
        self.overwrite = overwrite
        self.only_first = only_first
        self.save_metadata = save_metadata
        self.progress = progress

    def download(self, url: str) -> Result:
        """Download one episode.

        Args:
            url: The episode URL.

        Returns:
            The episode, the directory it belongs in, and what was done.
        """
        episode = self.extractor.episode(url)
        save_dir = self.save_path / _dirname(episode.series_title) / _dirname(episode.episode_title)
        if save_dir.exists() and not self.overwrite:
            return Result(episode, save_dir, "exists")
        if not episode.readable:
            return Result(episode, save_dir, "locked")

        save_dir.mkdir(parents=True, exist_ok=True)
        if self.save_metadata:
            (save_dir / "metadata.json").write_text(
                json.dumps(asdict(episode), indent=4, ensure_ascii=False),
                encoding="utf-8",
            )
        self._save_pages(episode, save_dir)
        return Result(episode, save_dir, "saved")

    def _save_pages(self, episode: Episode, save_dir: Path) -> None:
        wanted = episode.pages[:1] if self.only_first else episode.pages
        width = len(str(len(wanted)))
        progress = Progress(
            SpinnerColumn(),
            TextColumn("{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            TextColumn("("),
            MofNCompleteColumn(),
            TextColumn("pages )"),
            TextColumn("remain:"),
            TimeRemainingColumn(),
            TextColumn("spent:"),
            TimeElapsedColumn(),
            disable=not self.progress,
        )
        with progress:
            task = progress.add_task("[red]Downloading...", total=len(wanted))
            for index, page in enumerate(wanted):
                image = self.extractor.image(page, episode)
                if image.mode not in ("RGB", "L"):
                    image = image.convert("RGB")
                image.save(save_dir / f"{index:0{width}d}.jpg", quality=95)
                progress.update(task, advance=1)


def _dirname(title: str) -> str:
    """Turn a title into a directory name, keeping a `/` in it readable."""
    return str(sanitize_filename(title.replace("/", "／"))) or "_"
