"""The periodic walk: what the watcher never saw is found, and cheaply.

Driven against the fake drive, which answers digests from its own rows. Every
assertion is about what the drive was asked or ended up holding: which rows a
walk queued, how many digest requests left, whether a batch went at all.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from alkera_cli.files.digest import digest_of
from alkera_cli.files.live_sync import (
    RECONCILE_CAP,
    RECONCILE_EVERY,
    WALK_YIELD_EVERY,
    DriveDigests,
    LiveSync,
    RestLiveApi,
)
from alkera_cli.files.tree_watch import Change
from files._live_sync_fakes import FakeClock, FakeLiveApi
from files._live_sync_fakes import make_sync as _sync
from files._live_sync_fakes import write as _write


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    root = tmp_path / "scratch"
    root.mkdir()
    _write(root, "top.txt", b"top")
    _write(root, "a/x.txt", b"x")
    _write(root, "b/y.txt", b"y")
    return root


def _in_sync(tree: Path, api: FakeLiveApi, clock: FakeClock, **kwargs: Any) -> LiveSync:
    """A sync that has told the drive about everything on disk."""
    sync = _sync(tree, api, clock, **kwargs)
    sync._offer_entries(Change.added, tree, recursive=True)
    sync.flush()
    # The digests the uploads proved go in a batch of their own.
    sync.flush()
    assert sync.metadata == {} and sync.pending == {}
    return sync


def _sent_since(api: FakeLiveApi, mark: int) -> list[tuple[str, str]]:
    return sorted((entry.op, entry.path) for batch in api.trees[mark:] for entry in batch)


def test_a_file_the_watcher_never_saw_is_found_and_only_its_directory_is_sent(
    tree: Path,
) -> None:
    clock = FakeClock()
    api = FakeLiveApi(root=tree)
    sync = _in_sync(tree, api, clock)
    mark = len(api.trees)
    _write(tree, "b/new.txt", b"written while the watch was down")

    assert asyncio.run(sync.reconcile()) is True
    sync.flush()

    assert _sent_since(api, mark) == [("upsert", "b/new.txt"), ("upsert", "b/y.txt")]
    assert api.stored["b/new.txt"] == b"written while the watch was down"


def test_a_file_deleted_while_the_watcher_was_down_is_trashed_by_the_walk(tree: Path) -> None:
    clock = FakeClock()
    api = FakeLiveApi(root=tree)
    sync = _in_sync(tree, api, clock)
    node = api.nodes["a/x.txt"]
    (tree / "a" / "x.txt").unlink()

    assert asyncio.run(sync.reconcile()) is True
    sync.flush()

    assert "a/x.txt" not in api.nodes
    assert node in api.trashed


def test_a_file_the_drive_trashed_is_never_offered_back_by_the_walk(tree: Path) -> None:
    """Seen on an awake box: a person trashed a file right after the box
    pulled it, the box's fetch of the owed change was refused, and the walk
    then found the directory differing and filed the box's untouched copy as
    a new node: the trash was undone. A file still holding the bytes the two
    agreed that the drive no longer files was taken away by somebody else;
    the walk leaves it to the inbound pass and sends nothing for it."""
    clock = FakeClock()
    api = FakeLiveApi(root=tree)
    sync = _in_sync(tree, api, clock)
    node = api.nodes["a/x.txt"]
    api._delete("a/x.txt")
    mark = len(api.trees)
    uploads = len(api.uploads)

    assert asyncio.run(sync.reconcile()) is True
    sync.flush()
    sync.flush()

    assert "a/x.txt" not in api.nodes, "the trashed file was filed again"
    assert api.trashed == [node]
    assert ("upsert", "a/x.txt") not in _sent_since(api, mark)
    assert api.uploads[uploads:] == []


def test_a_file_the_box_changed_after_the_drive_trashed_it_is_still_sent(tree: Path) -> None:
    """The asymmetric case: bytes the box wrote after the agreement are its own
    work, and the walk still sends them even though the drive trashed the
    node, so nothing the agent wrote is lost."""
    clock = FakeClock()
    api = FakeLiveApi(root=tree)
    sync = _in_sync(tree, api, clock)
    api._delete("a/x.txt")
    _write(tree, "a/x.txt", b"the agent's rewrite")

    assert asyncio.run(sync.reconcile()) is True
    sync.flush()

    assert api.stored["a/x.txt"] == b"the agent's rewrite"


def test_a_file_deleted_while_the_box_was_down_is_trashed_by_the_next_start_s_walk(
    tree: Path,
) -> None:
    """The box restarts; a file it had listed is gone from the disk by then.
    The new process never sent that row, but the drive still lists it: the
    walk must trash it, or the two trees differ for good and every walk after
    re-sends the whole directory."""
    clock = FakeClock()
    api = FakeLiveApi(root=tree)
    _in_sync(tree, api, clock)
    (tree / "a" / "x.txt").unlink()
    restarted = _sync(tree, api, clock)

    asyncio.run(restarted.reconcile())
    restarted.flush()
    restarted.flush()

    assert "a/x.txt" not in api.nodes
    mark = len(api.trees)
    assert asyncio.run(restarted.reconcile()) is False, "the trees agree after one walk"
    assert len(api.trees) == mark


def test_the_next_start_s_walk_trashes_it_once_and_then_rests(tree: Path) -> None:
    """The row is trashed by the first walk after the restart, the directory's
    digest then matches the drive's, and the walk after sends nothing and
    doubles its interval instead of re-sending the directory for good."""
    clock = FakeClock()
    api = FakeLiveApi(root=tree)
    _in_sync(tree, api, clock)
    node = api.nodes["a/x.txt"]
    (tree / "a" / "x.txt").unlink()
    restarted = _sync(tree, api, clock)
    mark = len(api.trees)

    assert asyncio.run(restarted.reconcile()) is True
    restarted.flush()

    assert _sent_since(api, mark) == [("delete", "a/x.txt")]
    assert node in api.trashed
    assert api.name_requests == [["a"]]
    settled = len(api.trees)
    assert asyncio.run(restarted.reconcile()) is False
    assert len(api.trees) == settled
    assert api.name_requests == [["a"]], "a matching digest asks for no names"
    assert restarted.reconcile_interval == RECONCILE_EVERY * 2


def test_a_directory_deleted_while_the_box_was_down_is_one_delete(tree: Path) -> None:
    _write(tree, "b/deep/z.txt", b"z")
    clock = FakeClock()
    api = FakeLiveApi(root=tree)
    _in_sync(tree, api, clock)
    for inner in ("deep/z.txt", "y.txt"):
        (tree / "b" / inner).unlink()
    (tree / "b" / "deep").rmdir()
    (tree / "b").rmdir()
    restarted = _sync(tree, api, clock)
    mark = len(api.trees)

    assert asyncio.run(restarted.reconcile()) is True
    restarted.flush()

    sent = _sent_since(api, mark)
    assert [entry for entry in sent if entry[0] == "delete"] == [("delete", "b")]
    assert not [entry for entry in sent if entry[1].startswith("b/")]
    assert not [path for path in [*api.nodes, *api.dirs] if path.startswith("b")]


def test_a_directory_that_lost_one_file_and_gained_another_sends_one_of_each(
    tree: Path,
) -> None:
    clock = FakeClock()
    api = FakeLiveApi(root=tree)
    _in_sync(tree, api, clock)
    (tree / "a" / "x.txt").unlink()
    _write(tree, "a/fresh.txt", b"written while the box was down")
    restarted = _sync(tree, api, clock)
    mark = len(api.trees)

    assert asyncio.run(restarted.reconcile()) is True
    restarted.flush()

    assert _sent_since(api, mark) == [("delete", "a/x.txt"), ("upsert", "a/fresh.txt")]
    assert sorted(path for path in api.nodes if path.startswith("a/")) == ["a/fresh.txt"]


def test_a_name_the_disk_still_holds_as_something_the_plane_leaves_out_is_kept(
    tree: Path,
) -> None:
    """The walk skips a link, so the directory differs; but the name is still
    on the disk, and a delete would trash what was never deleted."""
    clock = FakeClock()
    api = FakeLiveApi(root=tree)
    _in_sync(tree, api, clock)
    (tree / "a" / "x.txt").unlink()
    (tree / "a" / "x.txt").symlink_to(tree / "top.txt")
    restarted = _sync(tree, api, clock)
    mark = len(api.trees)

    assert asyncio.run(restarted.reconcile()) is True
    restarted.flush()

    assert ("delete", "a/x.txt") not in _sent_since(api, mark)
    assert "a/x.txt" in api.nodes


@pytest.mark.skipif(
    sys.platform == "win32",
    reason=(
        "1 001 real directories, each with a file, took a 30 s median and a 60 s peak on the"
        " Windows runner (under 1 s on macOS) and crossed the 90 s per-test timeout in run"
        " 36711483024, killing the xdist worker. The batch size is the route's ceiling, so the"
        " volume cannot shrink; Linux and macOS keep the test."
    ),
)
def test_differing_directories_are_listed_in_batches_of_a_thousand(tmp_path: Path) -> None:
    root = tmp_path / "wide"
    for index in range(1001):
        _write(root, f"d{index:04d}/f.txt", b"")
    api = FakeLiveApi(root=root)
    sync = _sync(root, api, FakeClock())

    asyncio.run(sync.reconcile())

    assert [len(request) for request in api.name_requests] == [1000, 2]
    assert all(
        set(names) <= set(paths)
        for names, paths in zip(api.name_requests, api.digest_requests[1:], strict=True)
    )


def test_a_tree_where_nothing_moved_sends_one_digest_request_and_no_batch(tree: Path) -> None:
    clock = FakeClock()
    api = FakeLiveApi(root=tree)
    sync = _in_sync(tree, api, clock)
    mark = len(api.trees)

    assert asyncio.run(sync.reconcile()) is False
    sync.flush()

    assert api.digest_requests == [["", "a", "b"]]
    assert api.name_requests == []
    assert len(api.trees) == mark
    assert sync.metadata == {}


def test_directories_are_asked_about_in_batches_of_four_thousand(tmp_path: Path) -> None:
    root = tmp_path / "wide"
    for index in range(4001):
        (root / f"d{index:04d}").mkdir(parents=True)
    api = FakeLiveApi(root=root)
    sync = _sync(root, api, FakeClock())

    asyncio.run(sync.reconcile())

    # The walk's own two, then one listing the root: the drive lacks its 4 001 folders.
    assert [len(request) for request in api.digest_requests] == [4000, 2, 1]


def test_rows_under_a_directory_the_rules_now_exclude_are_deleted_by_the_walk(
    tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cache the drive was sent before the rules left it out must not linger:
    the walk deletes the excluded directory itself, and says nothing beneath."""
    _write(tree, ".uvcache/wheels/pkg.whl", b"wheel")
    clock = FakeClock()
    api = FakeLiveApi(root=tree)
    # The rows were sent by a sync that folded nothing, as before the caches preset existed.
    _in_sync(tree, api, clock, exclude_presets=())
    assert ".uvcache/wheels/pkg.whl" in api.nodes
    walk_module = sys.modules["alkera_cli.files.walk"]
    monkeypatch.setitem(walk_module.EXCLUDE_PRESETS, "caches", frozenset({b".uvcache"}))
    sync = _sync(tree, api, clock, exclude_presets=("caches",))
    mark = len(api.trees)

    assert asyncio.run(sync.reconcile()) is True
    deletes = [path for path, entry in sync.metadata.items() if entry.op == "delete"]
    assert deletes == [".uvcache"]
    assert not [path for path in sync.metadata if path.startswith(".uvcache/")]
    sync.flush()

    assert not [path for path in [*api.nodes, *api.dirs] if path.startswith(".uvcache")]
    assert [e for e in _sent_since(api, mark) if e[1].startswith(".uvcache")] == [
        ("delete", ".uvcache")
    ]


def test_the_interval_doubles_to_its_cap_while_nothing_differs_and_resets(tree: Path) -> None:
    clock = FakeClock()
    api = FakeLiveApi(root=tree)
    sync = _in_sync(tree, api, clock)

    seen = []
    for _ in range(6):
        asyncio.run(sync.reconcile())
        seen.append(sync.reconcile_interval)
    assert seen == [120.0, 240.0, 480.0, RECONCILE_CAP, RECONCILE_CAP, RECONCILE_CAP]

    # Any batch puts it back to the minute...
    _write(tree, "c.txt", b"c")
    sync.classify(Change.added, str(tree / "c.txt"))
    sync.flush()
    assert sync.reconcile_interval == RECONCILE_EVERY

    # ...and so does a walk that found something...
    for _ in range(3):
        asyncio.run(sync.reconcile())
    _write(tree, "a/late.txt", b"late")
    asyncio.run(sync.reconcile())
    assert sync.reconcile_interval == RECONCILE_EVERY

    # ...and a watcher that says it dropped events.
    sync.flush()
    asyncio.run(sync.reconcile())
    assert sync.reconcile_interval > RECONCILE_EVERY
    sync.note_overflow()
    assert sync.reconcile_interval == RECONCILE_EVERY


class TickingWatcher:
    """Empty ticks, each moving the clock on first — with the heartbeat
    landing on every one, so the fence stays open."""

    def __init__(self, sync: LiveSync, clock: FakeClock, step: float, ticks: int) -> None:
        self.sync, self.clock, self.step, self.ticks = sync, clock, step, ticks

    async def changes(self) -> AsyncIterator[set[tuple[Change, str]]]:
        async def stream() -> AsyncIterator[set[tuple[Change, str]]]:
            for _ in range(self.ticks):
                self.clock.advance(self.step)
                self.sync.fence.beat()
                yield set()

        return stream()


def test_the_loop_never_walks_within_the_interval_of_the_last_walk(tree: Path) -> None:
    """Ticks every 30 s for 150 s: one walk at 60 s, and after it found
    nothing, none before 180 s."""
    clock = FakeClock()
    api = FakeLiveApi(root=tree)
    sync = _in_sync(tree, api, clock)
    sync.watcher = TickingWatcher(sync, clock, 30.0, 5)

    asyncio.run(sync.run())

    assert len(api.digest_requests) == 1


def test_the_walk_yields_to_the_loop_while_it_walks(tmp_path: Path) -> None:
    """Another coroutine runs while the walk is still visiting entries —
    before it has asked the drive a thing."""
    root = tmp_path / "big"
    for index in range(WALK_YIELD_EVERY * 2 + 10):
        _write(root, f"d{index % 7}/f{index:05d}.txt", b"")
    api = FakeLiveApi(root=root)
    sync = _sync(root, api, FakeClock())
    mid_walk: list[bool] = []

    async def neighbour() -> None:
        while True:
            if sync.reconciling:
                mid_walk.append(not api.digest_requests)
            await asyncio.sleep(0)
            if api.digest_requests and not sync.reconciling:
                return

    async def both() -> None:
        await asyncio.gather(neighbour(), sync.reconcile())

    asyncio.run(both())

    assert any(mid_walk), "nothing else ran while the walk was visiting entries"


def test_a_second_walk_while_one_runs_does_nothing(tree: Path) -> None:
    api = FakeLiveApi(root=tree)
    sync = _in_sync(tree, api, FakeClock())
    sync.reconciling = True

    assert asyncio.run(sync.reconcile()) is False
    assert api.digest_requests == []


def test_the_rest_api_asks_from_the_leased_folder_and_answers_as_asked(tmp_path: Path) -> None:
    """The watched directory sits a step below the lease, so every path takes
    the step on the way out and gives it back on the way in."""
    seen: list[dict[str, Any]] = []
    digest = digest_of([("file", b"a.txt", 1, 2)])

    def route(request: httpx.Request) -> httpx.Response:
        body = request.content
        if request.headers.get("Content-Encoding") == "gzip":
            body = gzip.decompress(body)
        seen.append({"path": request.url.path, "body": json.loads(body)})
        # Rendered as the route renders it: all ASCII, a surrogate escaped.
        answer = json.dumps(
            {
                "digests": {
                    "scratch": {"count": digest.count, "xor": digest.hex},
                    "scratch/sub": {"count": 0, "xor": "0" * 16},
                    "elsewhere": {"count": 9, "xor": "f" * 16},
                },
                "children": {
                    "scratch/sub": ["a.txt", "r\udce9.txt"],
                    "elsewhere": ["not-ours.txt"],
                },
            }
        )
        assert answer.isascii()
        return httpx.Response(
            200, content=answer.encode(), headers={"Content-Type": "application/json"}
        )

    api = RestLiveApi(
        files=None,  # type: ignore[arg-type]
        http=httpx.Client(transport=httpx.MockTransport(route), base_url="http://files.test"),
        root=tmp_path,
        drive_id="d1",
        lease_node_id="n1",
        dest="chat/scratch",
        inside="scratch",
    )

    plain = api.digests(["", "sub"])
    listing = api.digests(["", "sub"], names=["sub"])

    assert seen == [
        {
            "path": "/api/v1/files/drives/d1/items/n1/lease/tree/digests",
            "body": {"paths": ["scratch", "scratch/sub"]},
        },
        {
            "path": "/api/v1/files/drives/d1/items/n1/lease/tree/digests",
            "body": {"paths": ["scratch", "scratch/sub"], "names": ["scratch/sub"]},
        },
    ]
    assert plain == DriveDigests(digests={"": digest, "sub": digest_of([])}, children={})
    # Only the folders asked to list, keyed as asked, the undecodable byte as
    # the surrogate the walk spells it with.
    assert listing == DriveDigests(
        digests={"": digest, "sub": digest_of([])},
        children={"sub": ["a.txt", "r\udce9.txt"]},
    )


def test_the_walk_doubles_its_interval_while_nothing_differs_and_a_difference_resets_it(
    tree: Path,
) -> None:
    clock = FakeClock()
    api = FakeLiveApi(root=tree)
    sync = _in_sync(tree, api, clock)
    sync.reconcile_every, sync.reconcile_cap = 5.0, 60.0
    sync.note_overflow()
    intervals = []
    for _ in range(6):
        assert asyncio.run(sync.reconcile()) is False
        intervals.append(sync.reconcile_interval)
    assert intervals == [10.0, 20.0, 40.0, 60.0, 60.0, 60.0]
    _write(tree, "b/late.txt", b"found by the walk")
    assert asyncio.run(sync.reconcile()) is True
    assert sync.reconcile_interval == 5.0
