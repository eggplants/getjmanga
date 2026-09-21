from __future__ import annotations

import logging
from pathlib import Path

import pytest

from getjmanga.console import logger, setup, summary
from getjmanga.downloader import Result
from getjmanga.extractor import Episode


def result(status, title="ep1"):
    episode = Episode(url="https://example.com/ep", series_title="S", episode_title=title, pages=())
    return Result(episode, Path("out/example.com/S") / title, status)


def test_summary_names_the_directory_of_a_single_episode():
    assert summary([result("saved")]) == (logging.INFO, "saved: out/example.com/S/ep1")
    assert summary([result("exists")]) == (logging.INFO, "skipped (already there): out/example.com/S/ep1")


def test_summary_warns_about_a_single_locked_episode():
    assert summary([result("locked", "ep2")]) == (
        logging.WARNING,
        "skip: 'ep2' needs a purchase, a wait or a login.",
    )


def test_summary_counts_the_episodes_of_a_work_under_its_series_directory():
    results = [result("saved", "ep1"), result("exists", "ep2"), result("locked", "ep3"), result("saved", "ep4")]
    assert summary(results) == (logging.INFO, "saved: out/example.com/S (2 episodes, 1 already there, 1 locked)")
    assert summary(results[1:3]) == (logging.INFO, "skipped (already there): out/example.com/S (1 episodes, 1 locked)")


def test_summary_warns_when_nothing_in_a_work_was_readable():
    assert summary([result("locked", "ep1"), result("locked", "ep2")]) == (
        logging.WARNING,
        "locked: out/example.com/S (2 episodes)",
    )


@pytest.mark.parametrize(
    ("flags", "out", "err"),
    [
        ({}, "hello\n", "careful\nerror: broken\n"),
        ({"quiet": True}, "", "careful\nerror: broken\n"),
    ],
)
def test_setup_splits_the_warnings_from_the_rest(capsys, flags, out, err):
    setup(**flags)
    logger.info("hello")
    logger.warning("careful")
    logger.error("broken")
    assert capsys.readouterr() == (out, err)


def test_setup_replaces_what_it_set_up_before(capsys):
    setup()
    setup()
    logger.info("once")
    assert capsys.readouterr().out == "once\n"
