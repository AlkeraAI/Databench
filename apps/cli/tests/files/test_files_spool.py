"""The inbound spool: partial downloads a killed daemon left behind are swept
on the spool's first use, and stop counting as bytes still arriving; a
download still being written is never touched."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from alkera_cli.files import spool as spool_module
from alkera_cli.files.spool import SPOOL_SUFFIX, InboundSpool
from freezegun import freeze_time

NOW = "2026-10-05 23:30:00"
#: Long past any sweep window: a download that died days ago.
DAYS_DEAD = 3 * 24 * 3600.0
STALE_AFTER_S = getattr(spool_module, "STALE_AFTER_S", 6 * 3600.0)


def _leftover(directory: Path, name: str, size: int, *, age_s: float) -> Path:
    path = directory / f"{name}{SPOOL_SUFFIX}"
    path.write_bytes(b"x" * size)
    stamp = time.time() - age_s
    os.utime(path, (stamp, stamp))
    return path


@pytest.mark.parametrize(
    ("age_s", "swept"),
    [
        pytest.param(STALE_AFTER_S + 60, True, id="past-the-window"),
        pytest.param(STALE_AFTER_S - 60, False, id="inside-the-window"),
        pytest.param(5, False, id="being-written-now"),
    ],
)
def test_a_partial_download_is_swept_only_once_nobody_has_written_it_for_the_window(
    tmp_path: Path, age_s: float, swept: bool
) -> None:
    directory = tmp_path / "journal.inbound"
    directory.mkdir()
    spool = InboundSpool(directory)
    key = spool.new_file("papers/report.pdf").name.split(".", 1)[0]
    for child in list(directory.iterdir()):
        child.unlink()
    left = _leftover(directory, f"{key}.deadbeef", 4096, age_s=age_s)

    fresh = InboundSpool(directory)
    fresh.new_file("other.txt")

    assert left.exists() is not swept


def test_a_dead_download_no_longer_counts_as_bytes_arriving(tmp_path: Path) -> None:
    """A partial file of a download that died would read as bytes still
    arriving for that file, keeping a turn waiting on a hand-over that will
    never come."""
    directory = tmp_path / "journal.inbound"
    directory.mkdir()
    probe = InboundSpool(directory)
    key = probe.new_file("papers/report.pdf").name.split(".", 1)[0]
    for child in list(directory.iterdir()):
        child.unlink()
    _leftover(directory, f"{key}.deadbeef", 4096, age_s=DAYS_DEAD)
    _leftover(directory, f"{key}.live0001", 100, age_s=1)

    assert InboundSpool(directory).landing_bytes("papers/report.pdf") == 100


def test_the_sweep_window_is_read_off_the_clock_it_is_given(tmp_path: Path) -> None:
    directory = tmp_path / "journal.inbound"
    directory.mkdir()
    with freeze_time(NOW) as frozen:
        left = _leftover(directory, "abc.12345678", 10, age_s=0)
        frozen.tick(STALE_AFTER_S - 1)
        assert InboundSpool(directory).sweep() == 0 and left.exists()
        frozen.tick(2)
        assert InboundSpool(directory).sweep() == 1 and not left.exists()


def test_only_spool_files_are_swept(tmp_path: Path) -> None:
    directory = tmp_path / "journal.inbound"
    directory.mkdir()
    other = directory / "notes.txt"
    other.write_text("keep", encoding="utf-8")
    stamp = time.time() - STALE_AFTER_S * 2
    os.utime(other, (stamp, stamp))

    assert InboundSpool(directory).sweep() == 0
    assert other.exists()
