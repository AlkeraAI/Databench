"""An export batch over a tree nobody wrote reads nothing from the drive.

A held folder is exported on a cadence, and every batch was a whole-tree push:
an item lookup per file, the held folder read for its etag, then the snapshot —
so an idle folder asked the drive about every file it held every few seconds
to learn that nothing had changed. The batch now remembers the tree it last
landed and, while the tree is the same, sends only the snapshot that keeps the
lease current, under the etag that snapshot was last accepted at. A file
written on the box still lands on the very next batch.

The wire is a real ``httpx`` client over a transport that serves the snapshot
route; the Files namespace and the push count every read they are asked for.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from alkera_cli.files.mount import MountRecord, export, tree_mark_path, watch
from alkera_cli.files.push import PushSummary

DRIVE = "11111111-1111-1111-1111-111111111111"
NODE = "22222222-2222-2222-2222-222222222222"


class Files:
    """The namespace a batch reads through; every read is counted."""

    def __init__(self) -> None:
        self.reads: list[str] = []
        self.etag = "7"

    def drive(self) -> dict[str, Any]:
        self.reads.append("drive")
        return {"id": DRIVE, "rootId": "root"}

    def item(self, drive_id: str, item_id: str, *, select: str | None = None) -> dict[str, Any]:
        self.reads.append(f"item {item_id}")
        return {"id": item_id, "etag": self.etag, "kind": "folder"}

    def item_by_path(self, drive_id: str, item_path: str) -> dict[str, Any]:
        self.reads.append(f"path {item_path}")
        return {"id": NODE, "etag": self.etag, "kind": "folder"}

    def children(self, drive_id: str, item_id: str, **_kwargs: Any) -> Any:
        self.reads.append(f"children {item_id}")
        return iter(())


class Drive:
    """The snapshot route: the If-Match each snapshot carried, refused when it
    is not the held folder's etag right now."""

    def __init__(self, files: Files) -> None:
        self.files = files
        self.snapshots: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/snapshots"):
            said = request.headers.get("If-Match", "").strip('"')
            self.snapshots.append(said)
            if said != self.files.etag:
                return httpx.Response(
                    412, json={"error": {"code": "precondition_failed", "message": "moved"}}
                )
            json.loads(request.content)
            return httpx.Response(204)
        return httpx.Response(404, json={})


class Pushes:
    """The push a batch runs: one per batch that walked the drive file by file."""

    def __init__(self) -> None:
        self.count = 0

    def __call__(self, **_kwargs: Any) -> PushSummary:
        self.count += 1
        return PushSummary(uploaded=1)


@pytest.fixture
def held(tmp_path: Path) -> tuple[MountRecord, Path, Path]:
    root = tmp_path / "folder"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "a.txt").write_bytes(b"a\n")
    (root / "b.txt").write_bytes(b"b\n")
    record = MountRecord(
        instance_id="box:1",
        drive_id=DRIVE,
        node_id=NODE,
        org_path="home/ana/Chats/c",
        local_root=str(root),
        epoch=3,
    )
    return record, root, tmp_path / "home"


def _batch(record: MountRecord, files: Files, drive: Drive, pushes: Pushes, home: Path) -> Any:
    http = httpx.Client(transport=httpx.MockTransport(drive), base_url="http://files.test")
    return export(files=files, http=http, record=record, push_tree=pushes, home=home)


def test_a_batch_over_an_unchanged_tree_reads_nothing(
    held: tuple[MountRecord, Path, Path],
) -> None:
    record, _root, home = held
    files, pushes = Files(), Pushes()
    drive = Drive(files)
    _batch(record, files, drive, pushes, home)
    assert pushes.count == 1
    files.reads.clear()

    for _ in range(5):
        summary = _batch(record, files, drive, pushes, home)
        assert summary.uploaded == 0

    assert pushes.count == 1, "an unchanged tree was pushed again"
    assert files.reads == [], "an unchanged tree read the drive"
    assert drive.snapshots[1:] == ["7"] * 5, "the lease is still kept current, read-free"


@pytest.mark.parametrize(
    "change",
    [
        pytest.param(lambda root: (root / "docs" / "a.txt").write_bytes(b"edited\n"), id="edit"),
        pytest.param(lambda root: (root / "docs" / "new.txt").write_bytes(b"n\n"), id="add"),
        pytest.param(lambda root: (root / "b.txt").unlink(), id="delete"),
        pytest.param(lambda root: (root / "b.txt").rename(root / "c.txt"), id="rename"),
        pytest.param(lambda root: (root / "sub").mkdir(), id="new-folder"),
        pytest.param(
            lambda root: os.chmod(root / "b.txt", 0o600),
            id="chmod",
            marks=pytest.mark.skipif(
                sys.platform == "win32",
                reason="Windows has no mode bits: chmod 0o600 changes nothing to fingerprint",
            ),
        ),
        pytest.param(
            lambda root: os.utime(root / "b.txt", ns=(1_000_000_000, 1_000_000_000)), id="touch"
        ),
    ],
)
def test_a_change_on_the_box_is_sent_by_the_next_batch(
    held: tuple[MountRecord, Path, Path], change: Any
) -> None:
    record, root, home = held
    files, pushes = Files(), Pushes()
    drive = Drive(files)
    _batch(record, files, drive, pushes, home)
    _batch(record, files, drive, pushes, home)
    assert pushes.count == 1

    change(root)
    _batch(record, files, drive, pushes, home)

    assert pushes.count == 2
    _batch(record, files, drive, pushes, home)
    assert pushes.count == 2, "the change is sent once, not on every batch after it"


def test_the_machines_own_hold_on_the_tree_is_not_a_change(
    held: tuple[MountRecord, Path, Path],
) -> None:
    """The lock and journal files a box keeps in the folder change whenever it
    is opened; they are never pushed, so they never send a batch either."""
    record, root, home = held
    files, pushes = Files(), Pushes()
    drive = Drive(files)
    _batch(record, files, drive, pushes, home)
    (root / ".lock").write_text('{"pid": 1}')
    (root / ".cache").mkdir()
    (root / ".cache" / "pip.json").write_text("{}")

    _batch(record, files, drive, pushes, home)

    assert pushes.count == 1


def test_a_folder_changed_on_the_drive_since_is_batched_in_full(
    held: tuple[MountRecord, Path, Path],
) -> None:
    """The held folder moved on the drive: the snapshot's precondition refuses,
    and the batch runs whole, as it did before it could be skipped."""
    record, _root, home = held
    files, pushes = Files(), Pushes()
    drive = Drive(files)
    _batch(record, files, drive, pushes, home)
    files.etag = "8"

    _batch(record, files, drive, pushes, home)

    assert pushes.count == 2
    assert drive.snapshots[-2:] == ["7", "8"]
    files.reads.clear()
    _batch(record, files, drive, pushes, home)
    assert files.reads == [] and pushes.count == 2


def test_a_new_grant_sends_its_first_batch_whatever_an_earlier_one_sent(
    held: tuple[MountRecord, Path, Path],
) -> None:
    """The mark belongs to the batches under one grant: a mount taken again
    forgets it, so the first batch after the take is always sent."""
    from alkera_cli.files.mount import forget_tree_mark

    record, root, home = held
    files, pushes = Files(), Pushes()
    drive = Drive(files)
    _batch(record, files, drive, pushes, home)
    assert tree_mark_path(root, home=home).exists()

    forget_tree_mark(root, home=home)
    _batch(record, files, drive, pushes, home)

    assert pushes.count == 2


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="flaky: counts loop passes in a wall-clock window and a slow Windows runner "
    "gets 5 where it expects 10 (Sep 30 2026); re-enable once the clock seam drives the count",
)
def test_the_mount_loop_walks_the_drive_once_for_an_idle_folder(
    held: tuple[MountRecord, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exporter the mount command runs, over twenty cadences of a folder
    nobody writes: one batch pushes it, the rest only keep the lease."""
    import alkera_cli.files.mount as mount_module

    record, _root, home = held
    files, pushes = Files(), Pushes()
    drive = Drive(files)
    monkeypatch.setattr(mount_module, "heartbeat", lambda **kwargs: kwargs["record"])
    now = [0.0]
    ticks = [0]

    def sleep(seconds: float) -> None:
        # The batch runs on a thread of its own; the loop only moves on once it
        # is done, so every cadence below has its batch.
        while mount_module.threading.active_count() > threads:
            time.sleep(0.001)
        now[0] += seconds

    def stop() -> bool:
        ticks[0] += 1
        return ticks[0] > 40

    threads = threading.active_count()
    http = httpx.Client(transport=httpx.MockTransport(drive), base_url="http://files.test")
    watch(
        files=files,  # type: ignore[arg-type]
        http=http,
        record=record,
        stop=stop,
        home=home,
        sleep=sleep,
        monotonic=lambda: now[0],
        push_tree=pushes,
    )

    assert pushes.count == 1
    assert len(drive.snapshots) >= 10
    assert [r for r in files.reads if not r.startswith("item")] == []
    assert files.reads.count(f"item {NODE}") == 1, "only the batch that pushed read the folder"


def test_a_directory_listing_that_lags_the_files_does_not_move_the_mark(
    held: tuple[MountRecord, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows answers a directory listing from the folder's own index, which
    catches up with a file's size and write time only once every handle on the
    file is closed — a virus scanner reading a file just written holds one. A
    mark taken from that listing moved between two batches over a tree nobody
    wrote, and pushed it again. The mark reads each entry's own metadata, so a
    listing that lags the files and one that has caught up agree."""
    import dataclasses

    import alkera_cli.files.mount as mount_module
    from alkera_cli.files.mount import tree_mark
    from alkera_cli.files.walk import EntryKind

    _record, root, _home = held
    caught_up = tree_mark(root)
    real_walk = mount_module.walk

    def lagging_walk(*args: Any, **kwargs: Any) -> Any:
        for entry in real_walk(*args, **kwargs):
            if entry.kind is EntryKind.FILE:
                entry = dataclasses.replace(entry, size=0, mtime_ns=entry.mtime_ns - 10**9)
            yield entry

    monkeypatch.setattr(mount_module, "walk", lagging_walk)

    assert tree_mark(root) == caught_up
    (root / "b.txt").write_bytes(b"bb\n")
    assert tree_mark(root) != caught_up, "a real write still moves the mark"
