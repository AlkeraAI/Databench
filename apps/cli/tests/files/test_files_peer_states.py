"""The states a box's text peer keeps across restarts, per file, beside its
folder's journal."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from alkera_cli.files import peer_states
from alkera_cli.files.peer_states import STATES_KEPT, PeerStates


def _beside(tmp_path: Path) -> PeerStates:
    return PeerStates.beside(SimpleNamespace(path=tmp_path / "mount.journal"))


def test_each_file_s_latest_states_come_back_to_a_new_process(tmp_path: Path) -> None:
    written = _beside(tmp_path)
    written.save("node-a", [(f"t{n}", f"text {n}") for n in range(STATES_KEPT + 2)])
    written.save("node-b", [("tb", "b")])

    read = _beside(tmp_path)
    assert read.load("node-a") == [(f"t{n}", f"text {n}") for n in range(2, STATES_KEPT + 2)]
    assert read.load("node-b") == [("tb", "b")]
    assert read.load("node-c") == []
    # Beside the journal, so the lease going back takes them with it.
    assert (tmp_path / "mount.journal-peer").is_file()


def test_a_file_too_large_to_keep_drops_what_was_kept_for_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(peer_states, "MAX_KEPT_BYTES", 10)
    states = _beside(tmp_path)
    states.save("node-a", [("t1", "small")])
    states.save("node-a", [("t1", "small"), ("t2", "far too large")])
    assert states.load("node-a") == []


@pytest.mark.parametrize(
    "body",
    [
        pytest.param("{not json", id="not-json"),
        pytest.param('["a list"]', id="not-an-object"),
        pytest.param('{"node-a": "not a list"}', id="not-a-list"),
        pytest.param('{"node-a": [["t1"], [1, 2], "x"]}', id="malformed-states"),
    ],
)
def test_what_cannot_be_read_back_names_nothing(tmp_path: Path, body: str) -> None:
    (tmp_path / "mount.journal-peer").write_text(body, encoding="utf-8")
    assert _beside(tmp_path).load("node-a") == []


def test_a_sync_with_no_journal_keeps_nothing(tmp_path: Path) -> None:
    states = PeerStates.beside(None)
    states.save("node-a", [("t1", "text")])
    assert states.load("node-a") == []
    assert list(tmp_path.iterdir()) == []


def test_a_file_the_peer_left_is_forgotten_and_the_others_kept(tmp_path: Path) -> None:
    states = _beside(tmp_path)
    states.save("node-a", [("ta", "a")])
    states.save("node-b", [("tb", "b")])
    states.drop("node-a")
    states.drop("node-never-kept")
    read = _beside(tmp_path)
    assert read.load("node-a") == []
    assert read.load("node-b") == [("tb", "b")]


def test_past_the_most_files_kept_the_least_recently_written_goes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(peer_states, "MAX_NODES", 2)
    states = _beside(tmp_path)
    states.save("node-a", [("ta", "a")])
    states.save("node-b", [("tb", "b")])
    # Written again, node-a is the newest: node-b is the one to go.
    states.save("node-a", [("ta", "a"), ("ta2", "a2")])
    states.save("node-c", [("tc", "c")])
    assert states.load("node-b") == []
    assert states.load("node-a") == [("ta", "a"), ("ta2", "a2")]
    assert states.load("node-c") == [("tc", "c")]


def test_past_the_most_text_kept_the_least_recently_written_goes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(peer_states, "MAX_FILE_BYTES", 10)
    states = _beside(tmp_path)
    states.save("node-a", [("ta", "aaaaaa")])
    states.save("node-b", [("tb", "bbbbbb")])
    assert states.load("node-a") == []
    assert states.load("node-b") == [("tb", "bbbbbb")]
    # One file larger than the whole budget alone is still the newest kept.
    states.save("node-c", [("tc", "c" * 20)])
    assert states.load("node-b") == []
    assert states.load("node-c") == [("tc", "c" * 20)]


def test_the_new_file_is_on_disk_before_it_is_renamed_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash just after the rename must find the whole file under the name,
    never an empty one: what is renamed in was synced to disk first."""
    synced: dict[int, int] = {}
    replaced: list[tuple[int, int]] = []
    real_fsync, real_replace = os.fsync, os.replace

    def fsync(fd: int) -> None:
        stat = os.fstat(fd)
        synced[stat.st_ino] = stat.st_size
        real_fsync(fd)

    def replace(source: Any, target: Any) -> None:
        stat = os.stat(source)
        replaced.append((stat.st_ino, stat.st_size))
        real_replace(source, target)

    monkeypatch.setattr(peer_states.os, "fsync", fsync)
    monkeypatch.setattr(peer_states.os, "replace", replace)
    _beside(tmp_path).save("node-a", [("ta", "text")])
    assert len(replaced) == 1
    inode, size = replaced[0]
    assert size > 0 and synced.get(inode) == size
