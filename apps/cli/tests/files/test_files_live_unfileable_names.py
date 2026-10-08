"""A name the drive cannot file stays on the machine and stops nothing else.

The watch spells a name that is not UTF-8 as a surrogate-escaped ``str``. On
a real box the storm's ``bad\\xffutf8`` reached the classifier, was queued, and
the journal's write of it raised ``UnicodeEncodeError`` — the live sync of the
whole workspace stopped and nothing written after it reached the drive.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

import pytest
from alkera_cli.files.journal import LiveJournal
from alkera_cli.files.tree_watch import Change
from files._live_sync_fakes import FakeClock, FakeLiveApi, FakeWatcher
from files._live_sync_fakes import make_sync as _sync

BAD = os.fsdecode(b"bad\xffutf8")


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    root = tmp_path / "scratch"
    root.mkdir()
    return root


def _make_bad(tree: Path) -> str:
    """The bad name on disk where the filesystem allows it; its spelling
    either way (APFS refuses the name, and a classifier refuses it unread)."""
    path = os.fsencode(tree) + b"/" + os.fsencode(BAD)
    if sys.platform != "darwin":
        os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o644))
    return os.fsdecode(path)


@pytest.mark.parametrize(
    "name",
    [
        pytest.param(BAD, id="not-utf8"),
        pytest.param("new\nline", id="control-character"),
    ],
)
def test_an_unfileable_name_is_never_queued_and_the_rest_syncs_through_the_journal(
    tree: Path, tmp_path: Path, name: str, caplog: pytest.LogCaptureFixture
) -> None:
    clock = FakeClock()
    api = FakeLiveApi(root=tree, clock=clock)
    path = _make_bad(tree) if name == BAD else str(tree / name)
    if name != BAD:
        (tree / name).write_text("x")
    (tree / "beside.txt").write_text("synced")
    batches = [
        {(Change.added, path), (Change.added, str(tree / "beside.txt"))},
        {(Change.modified, path)},
    ]
    sync = _sync(
        tree,
        api,
        clock,
        watcher=FakeWatcher(batches),
        journal=LiveJournal(tmp_path / "home" / "mount.journal"),
    )
    with caplog.at_level(logging.WARNING, logger="alkera_cli.files.live_sync"):
        asyncio.run(sync.run())

    assert api.stored.get("beside.txt") == b"synced"
    assert not [p for p in api.stored if "bad" in p or "line" in p]
    said = [r for r in caplog.records if "stays on this machine" in r.message]
    assert len(said) == 1, "said once, however often the watch reports it"
