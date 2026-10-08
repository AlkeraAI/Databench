"""The metadata queue: rows on the drive before their bytes.

A folder held by a box lists every file within about a second of its creation,
with its size and modified time, whether or not a byte of it has moved. These
tests drive :class:`LiveSync` against the fake drive in
``files._live_sync_fakes`` (a small server that applies a ``tree`` batch the
way the route does) and :class:`RestLiveApi` against a transport that plays the
route, and assert what the drive ended up listing, in what order, and what
stayed queued — never how often a method was called.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import os
import stat
import sys
import time
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
from alkera_cli.files.live_sync import (
    LiveCadence,
    LiveSync,
    RestLiveApi,
    TreeAnswer,
    TreeEntry,
)
from alkera_cli.files.mount import LeaseSupersededError, SelfFence
from alkera_cli.files.tree_watch import Change
from files._live_sync_fakes import FakeClock, FakeLiveApi, FakeWatcher, make_sync, record, write


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    root = tmp_path / "scratch"
    root.mkdir()
    return root


def _refusal(
    status: int,
    *,
    code: str | None = None,
    headers: dict[str, str] | None = None,
    detail: dict[str, Any] | None = None,
) -> httpx.HTTPStatusError:
    """A refused Files call as the client raises it: status, headers, body."""
    body: dict[str, Any] = {}
    if code is not None:
        body["code"] = code
    if detail is not None:
        body["detail"] = detail
    request = httpx.Request("POST", "http://drive/api/v1/files/drives/d/items/l/lease/tree")
    response = httpx.Response(status, json=body, headers=headers or {}, request=request)
    return httpx.HTTPStatusError(f"refused with {status}", request=request, response=response)


@dataclass
class JudgingApi(FakeLiveApi):
    """A drive whose tree route refuses what ``judge`` objects to, whole."""

    judge: Callable[[Sequence[TreeEntry]], Exception | None] = lambda _entries: None
    refused: list[list[str]] = field(default_factory=list)

    def tree(self, batch_id: str, entries: Sequence[TreeEntry], *, gzip_above: int) -> TreeAnswer:
        failure = self.judge(entries)
        if failure is not None:
            self.refused.append([entry.path for entry in entries])
            raise failure
        return super().tree(batch_id, entries, gzip_above=gzip_above)


class TickingWatcher(FakeWatcher):
    """A scripted stream whose every batch arrives ``step`` seconds after the
    last, on the test's clock — an empty batch is the watcher's own timeout."""

    def __init__(
        self, clock: FakeClock, batches: Sequence[set[tuple[Change, str]]], *, step: float
    ) -> None:
        super().__init__(batches)
        self.clock = clock
        self.step = step

    async def changes(self) -> AsyncIterator[set[tuple[Change, str]]]:
        batches, clock, step = self.batches, self.clock, self.step

        async def stream() -> AsyncIterator[set[tuple[Change, str]]]:
            for batch in batches:
                clock.advance(step)
                yield batch

        return stream()


def _stat_row(path: Path) -> tuple[int, int, int]:
    info = path.stat()
    return info.st_size, info.st_mtime_ns, stat.S_IMODE(info.st_mode)


# -- a burst, over the real wire ----------------------------------------------


@dataclass
class _Route:
    """The lease's routes as a transport: ``tree`` lists rows, ``live`` takes
    states, and every request is written down in the order it arrived."""

    clock: FakeClock
    listed: dict[str, str] = field(default_factory=dict)
    events: list[tuple[str, Any, float]] = field(default_factory=list)
    trees: list[tuple[dict[str, str], dict[str, Any], float]] = field(default_factory=list)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/lease/tree"):
            raw = request.content
            if request.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            body = json.loads(raw)
            self.trees.append((dict(request.headers), body, self.clock.monotonic()))
            self.events.append(("tree", len(body["entries"]), self.clock.monotonic()))
            for entry in body["entries"]:
                if entry["op"] != "delete":
                    self.listed.setdefault(entry["path"], f"node-{len(self.listed) + 1}")
            return httpx.Response(
                200,
                json={
                    "live_seq": len(self.trees),
                    "applied": len(body["entries"]),
                    "landing_count": len(self.listed),
                },
            )
        return httpx.Response(200, json={"liveSeq": 1, "pending": 0})


class _ListedFiles:
    """``item_by_path`` over what the route has listed so far."""

    def __init__(self, route: _Route, dest: str) -> None:
        self.route = route
        self.dest = dest

    def item_by_path(self, drive_id: str, item_path: str) -> dict[str, Any]:
        relative = item_path.removeprefix(f"{self.dest}/")
        node_id = self.route.listed.get(relative)
        if node_id is None:
            raise RuntimeError(f"GET {item_path} returned 404 — {{}}")
        return {"id": node_id, "etag": f"etag-{node_id}"}

    def item_under(self, drive_id: str, item_id: str, item_path: str) -> dict[str, Any]:
        assert item_id == "lease-9", item_id
        return self.item_by_path(drive_id, f"{self.dest}/{item_path}".rstrip("/"))

    def item(self, drive_id: str, item_id: str, *, select: str | None = None) -> dict[str, Any]:
        return {"id": item_id, "etag": f"etag-{item_id}"}


@pytest.mark.skipif(
    sys.platform == "win32",
    reason=(
        "ten thousand real files on the Windows runner take 62-67 s of a 90 s per-test timeout"
        " (3 s on macOS); under shard load it crossed 90 s and pytest-timeout killed the xdist"
        " worker in develop run 36706235618. The batching it pins is platform-independent and"
        " stays covered on Linux and macOS."
    ),
)
def test_a_burst_of_ten_thousand_is_listed_in_five_gzip_batches_before_any_upload(
    tree: Path,
) -> None:
    """Ten thousand files written in one tick are five ``tree`` batches of
    two thousand rows, compressed, each carrying the file's size, modified
    time and mode — every one of them on the wire before the first byte of
    any file is uploaded, and inside the metadata window of the burst."""
    names = [f"f{index:05d}.txt" for index in range(10_000)]
    for name in names:
        (tree / name).write_bytes(b"x")
    clock = FakeClock()
    route = _Route(clock=clock)

    def push(**kwargs: Any) -> None:
        for path in kwargs["paths"]:
            route.events.append(("upload", Path(path).name, clock.monotonic()))

    api = RestLiveApi(
        files=_ListedFiles(route, "Chats/c"),
        http=httpx.Client(transport=httpx.MockTransport(route), base_url="http://drive"),
        root=tree,
        drive_id="drive-1",
        lease_node_id="lease-9",
        dest="Chats/c",
        push=push,
    )
    started = clock.monotonic()
    sync = LiveSync(
        root=tree,
        record=record(),
        cadence=LiveCadence(settle_ms=0),
        api=api,
        watcher=FakeWatcher([{(Change.added, str(tree / name)) for name in names}]),
        fence=SelfFence(grace=30.0, monotonic=clock.monotonic),
        clock=clock,
    )

    asyncio.run(sync.run())

    first_upload = next(i for i, event in enumerate(route.events) if event[0] == "upload")
    before = route.events[:first_upload]
    assert [event[:2] for event in before] == [("tree", 2000)] * 5
    assert all(at - started <= 0.3 for _kind, _n, at in before)
    rows: dict[str, dict[str, Any]] = {}
    for headers, body, _at in route.trees[:5]:
        assert headers.get("content-encoding") == "gzip"
        for entry in body["entries"]:
            rows[entry["path"]] = entry
    assert sorted(rows) == names
    for name in (names[0], names[4_321], names[-1]):
        size, mtime_ns, mode = _stat_row(tree / name)
        assert rows[name] == {
            "op": "upsert",
            "path": name,
            "kind": "file",
            "size": size,
            "mtime_ns": mtime_ns,
            "mode": mode,
            "hash": None,
        }
    # And every file's bytes did land after, onto the rows already listed.
    assert sorted(name for kind, name, _at in route.events if kind == "upload") == names


def test_rows_leave_on_their_own_clock_while_the_bytes_wait_for_theirs(tree: Path) -> None:
    """The metadata window is the rows' and the content window the bytes':
    a file is listed once the metadata window has passed since it appeared,
    not before, and long before the content round that uploads it."""
    clock = FakeClock()
    api = FakeLiveApi(root=tree, clock=clock)
    path = write(tree, "notes.md", b"# notes\n")
    watcher = TickingWatcher(
        clock, [{(Change.added, str(path))}, set(), set(), set(), set()], step=0.1
    )
    sync = make_sync(
        tree,
        api,
        clock,
        cadence=LiveCadence(settle_ms=0, metadata_every_ms=300, batch_every_ms=60_000),
        watcher=watcher,
    )
    appeared = clock.monotonic() + 0.1

    asyncio.run(sync.run())

    listed_at = next(at for kind, _n, at in api.events if kind == "tree")
    uploaded_at = next(at for kind, _n, at in api.events if kind == "upload")
    assert listed_at - appeared == pytest.approx(0.3)
    assert api.events[0][0] == "tree"
    assert uploaded_at > listed_at


# -- coalescing -----------------------------------------------------------------


def test_a_file_changed_three_times_in_one_window_is_one_row_with_the_last_stat(
    tree: Path,
) -> None:
    clock = FakeClock()
    api = FakeLiveApi(root=tree, clock=clock)
    sync = make_sync(tree, api, clock)
    path = tree / "report.csv"
    for attempt, body in enumerate([b"a", b"a,b", b"a,b,c,d"]):
        path.write_bytes(body)
        stamp = time.time() + attempt
        os.utime(path, (stamp, stamp))
        sync.classify(Change.modified if attempt else Change.added, str(path))
        clock.advance(0.1)

    sync.flush()

    size, mtime_ns, mode = _stat_row(path)
    assert api.trees[0] == [
        TreeEntry(
            op="upsert",
            path="report.csv",
            kind="file",
            size=size,
            mtime_ns=mtime_ns,
            mode=mode,
            hash=None,
        )
    ]
    assert size == 7


def _upsert_then_delete(tree: Path, sync: LiveSync) -> None:
    path = write(tree, "scratch.tmp", b"half")
    sync.classify(Change.added, str(path))
    path.unlink()
    sync.classify(Change.deleted, str(path))


def _delete_then_upsert(tree: Path, sync: LiveSync) -> None:
    path = tree / "scratch.tmp"
    path.unlink()
    sync.classify(Change.deleted, str(path))
    write(tree, "scratch.tmp", b"written again, longer")
    sync.classify(Change.added, str(path))


@pytest.mark.parametrize(
    ("listed_first", "steps", "expected_op", "on_drive"),
    [
        pytest.param(False, _upsert_then_delete, "delete", False, id="delete-after-upsert"),
        pytest.param(True, _delete_then_upsert, "upsert", True, id="upsert-after-delete"),
    ],
)
def test_the_latest_word_on_a_path_is_the_one_row_sent(
    tree: Path,
    listed_first: bool,
    steps: Callable[[Path, LiveSync], None],
    expected_op: str,
    on_drive: bool,
) -> None:
    clock = FakeClock()
    api = FakeLiveApi(root=tree, clock=clock)
    sync = make_sync(tree, api, clock)
    if listed_first:
        sync.classify(Change.added, str(write(tree, "scratch.tmp", b"first")))
        sync.flush()
        api.trees.clear()

    steps(tree, sync)
    assert [entry.op for entry in sync.metadata.values()] == [expected_op]
    sync.flush()

    assert [(entry.op, entry.path) for entry in api.trees[0]] == [(expected_op, "scratch.tmp")]
    assert ("scratch.tmp" in api.nodes) is on_drive
    if on_drive:
        assert api.trees[0][0].size == len(b"written again, longer")


# -- refusals -------------------------------------------------------------------


def test_a_batch_the_route_finds_too_large_is_halved_until_every_row_lands(
    tree: Path,
) -> None:
    clock = FakeClock()
    api = JudgingApi(
        root=tree,
        clock=clock,
        judge=lambda entries: (
            _refusal(413, code="files.batch_too_large") if len(entries) > 3 else None
        ),
    )
    sync = make_sync(tree, api, clock)
    names = [f"n{index}.txt" for index in range(10)]
    for name in names:
        sync.classify(Change.added, str(write(tree, name, name.encode())))

    sync.flush()
    # The next round sends the digests the uploads proved, split the same way.
    sync.flush()

    assert sorted(api.listed()) == names
    assert all(entry.hash is not None for entry in api.listed().values())
    assert all(len(batch) <= 3 for batch in api.trees)
    assert sync.metadata == {}
    assert sorted(api.uploads) == names


@pytest.mark.parametrize(
    ("retry_after", "waits"),
    [
        pytest.param("5", 5.0, id="the-wait-the-server-named"),
        pytest.param("120", 30.0, id="capped-at-thirty-seconds"),
    ],
)
def test_a_throttled_batch_waits_while_the_queue_coalesces_and_never_drops_a_delete(
    tree: Path, retry_after: str, waits: float
) -> None:
    clock = FakeClock()
    throttled = {"on": False}
    api = JudgingApi(
        root=tree,
        clock=clock,
        judge=lambda _entries: (
            _refusal(429, headers={"Retry-After": retry_after}) if throttled["on"] else None
        ),
    )
    sync = make_sync(tree, api, clock)
    gone = write(tree, "gone.txt", b"to be deleted")
    kept = write(tree, "kept.txt", b"v1")
    sync.classify(Change.added, str(gone))
    sync.classify(Change.added, str(kept))
    sync.flush()
    sync.flush()  # and the digests the uploads proved
    node_of_gone = api.nodes["gone.txt"]
    api.trees.clear()

    throttled["on"] = True
    gone.unlink()
    sync.classify(Change.deleted, str(gone))
    sync.flush()
    assert api.refused == [["gone.txt"]]
    throttled["on"] = False

    # The wait is on the clock, not a sleep: the loop keeps classifying, the
    # bytes keep landing, and the rows wait.
    write(tree, "kept.txt", b"v2, rewritten while the rows wait")
    sync.classify(Change.modified, str(kept))
    clock.advance(waits - 0.5)
    sync.fence.beat()
    sync.flush()
    assert api.trees == []
    assert api.stored["kept.txt"] == b"v2, rewritten while the rows wait"
    assert sync.metadata["gone.txt"].op == "delete"

    clock.advance(0.5)
    sync.fence.beat()
    sync.flush()
    sent = {entry.path: entry.op for batch in api.trees for entry in batch}
    assert sent["gone.txt"] == "delete"
    assert sent["kept.txt"] == "upsert"
    assert "gone.txt" not in api.nodes
    assert node_of_gone in api.trashed
    assert "gone.txt" in sync.tombstones


@pytest.mark.parametrize(
    "run_loop",
    [pytest.param(False, id="a-flush"), pytest.param(True, id="the-loop")],
)
def test_a_fenced_tree_batch_stops_everything(tree: Path, run_loop: bool) -> None:
    clock = FakeClock()
    api = FakeLiveApi(root=tree, clock=clock, refuse="files.lease_fenced", refuse_on="tree")
    path = write(tree, "late.txt", b"bytes")
    sync = make_sync(tree, api, clock, watcher=FakeWatcher([{(Change.added, str(path))}]))

    with pytest.raises(LeaseSupersededError):
        if run_loop:
            asyncio.run(sync.run())
        else:
            sync.classify(Change.added, str(path))
            sync.flush()

    assert api.uploads == []
    assert api.batches == []
    assert set(sync.metadata) == {"late.txt"}
    assert set(sync.pending) == {"late.txt"}


def test_a_lease_with_too_many_rows_pauses_the_rows_and_not_the_bytes(
    tree: Path, caplog: pytest.LogCaptureFixture
) -> None:
    clock = FakeClock()
    api = FakeLiveApi(root=tree, clock=clock, refuse="files.live_too_many", refuse_on="tree")
    sync = make_sync(tree, api, clock)
    sync.classify(Change.added, str(write(tree, "one.txt", b"first")))

    with caplog.at_level(logging.WARNING, logger="alkera_cli.files.live_sync"):
        sync.flush()

    assert api.uploads == ["one.txt"]
    assert api.trees == []
    assert "one.txt" in sync.metadata
    assert [r for r in caplog.records if "as many rows" in r.getMessage()]

    api.refuse = None
    clock.advance(0.5)
    sync.flush()
    assert api.trees == [], "the rows wait out their backoff"
    clock.advance(0.5)
    sync.flush()
    assert "one.txt" in api.listed()
    assert sync.metadata == {}


def _mismatch_on(path: str, *, detail: Callable[[Sequence[TreeEntry]], Any] | None = None) -> Any:
    def judge(entries: Sequence[TreeEntry]) -> Exception | None:
        if all(entry.path != path for entry in entries):
            return None
        named = detail(entries) if detail is not None else None
        return _refusal(409, code="files.lease_mismatch", detail=named)

    return judge


@pytest.mark.parametrize(
    "judge",
    [
        pytest.param(_mismatch_on("odd/name.txt"), id="found-by-halving"),
        pytest.param(
            _mismatch_on(
                "odd/name.txt",
                detail=lambda entries: {
                    "indexes": [i for i, e in enumerate(entries) if e.path == "odd/name.txt"]
                },
            ),
            id="named-by-the-answer",
        ),
    ],
)
def test_a_path_the_route_refuses_is_dropped_and_the_rest_land(
    tree: Path, judge: Any, caplog: pytest.LogCaptureFixture
) -> None:
    clock = FakeClock()
    api = JudgingApi(root=tree, clock=clock, judge=judge)
    sync = make_sync(tree, api, clock)
    names = ["a.txt", "b.txt", "odd/name.txt", "c.txt", "d.txt"]
    for name in names:
        sync.classify(Change.added, str(write(tree, name, name.encode())))

    with caplog.at_level(logging.WARNING, logger="alkera_cli.files.live_sync"):
        sync.flush()
        # The digests the uploads proved follow, the refused path's with them.
        sync.flush()

    listed = {path for path, entry in api.listed().items() if entry.kind == "file"}
    assert listed == {"a.txt", "b.txt", "c.txt", "d.txt"}
    assert sync.metadata == {}
    assert [r for r in caplog.records if "odd/name.txt" in r.getMessage()]


def test_a_lease_the_route_will_not_take_at_all_keeps_every_row(tree: Path) -> None:
    """Refused whole and in every part, it is the lease and not a path: no
    row is dropped for it, and the rows land once the lease is live again."""
    clock = FakeClock()
    refusing = {"on": True}
    api = JudgingApi(
        root=tree,
        clock=clock,
        judge=lambda _entries: (
            _refusal(409, code="files.lease_mismatch") if refusing["on"] else None
        ),
    )
    sync = make_sync(tree, api, clock)
    names = [f"n{index}.txt" for index in range(8)]
    for name in names:
        sync.classify(Change.added, str(write(tree, name, name.encode())))

    sync.flush()
    assert sorted(sync.metadata) == names
    assert api.trees == []

    refusing["on"] = False
    clock.advance(1.0)
    sync.flush()
    assert sorted(api.listed()) == names


# -- what never enters the queue --------------------------------------------------


@pytest.fixture
def repository(tree: Path) -> Path:
    """A git working tree: its ``.gitignore`` is what the export obeys."""
    (tree / ".git").mkdir()
    write(tree, ".git/HEAD", b"ref: refs/heads/main\n")
    write(tree, ".gitignore", b"build/\n*.log\n")
    write(tree, "src/main.py", b"print('hi')\n")
    write(tree, "build/out.bin", b"\0" * 8)
    write(tree, "debug.log", b"noise")
    write(tree, "node_modules/left-pad/index.js", b"module.exports = 1\n")
    write(tree, ".lock", b"{}")
    write(tree, "chart.alkerareport", b"{}")
    (tree / "shortcut.py").symlink_to(tree / "src" / "main.py")
    return tree


@pytest.mark.parametrize(
    ("relative", "enters"),
    [
        pytest.param("src/main.py", True, id="the-work-itself"),
        pytest.param(".git/HEAD", True, id="repository-metadata-travels"),
        pytest.param("build/out.bin", False, id="under-a-gitignored-folder"),
        pytest.param("debug.log", False, id="a-gitignored-file"),
        pytest.param("node_modules/left-pad/index.js", False, id="a-preset-excluded-folder"),
        pytest.param(".lock", False, id="machine-local-state"),
        pytest.param("chart.alkerareport", False, id="a-pointer"),
        pytest.param("shortcut.py", False, id="a-link"),
    ],
)
def test_only_what_the_export_carries_enters_the_metadata_queue(
    repository: Path, relative: str, enters: bool
) -> None:
    clock = FakeClock()
    api = FakeLiveApi(root=repository, clock=clock)
    sync = make_sync(repository, api, clock)
    sync.exclude_presets = ("node_modules",)

    sync.classify(Change.added, str(repository / relative))

    assert (relative in sync.metadata) is enters


@pytest.mark.parametrize(
    ("relative", "enters"),
    [
        pytest.param(".uvcache/archive-v0/pkg/wheel.whl", False, id="uv-cache"),
        pytest.param(".uvpython/cpython-3.13/bin/python", False, id="uv-python"),
        pytest.param(".local/share/uv/tools/x", False, id="xdg-local"),
        pytest.param(".cache/pip/http/y", False, id="xdg-cache"),
        pytest.param(".venv/lib/site.py", False, id="venv"),
        pytest.param(".venv/bin/python", False, id="venv-interpreter"),
        pytest.param("analysis/.venv/lib/site.py", False, id="a-nested-venv"),
        pytest.param("src/__pycache__/plot.cpython-313.pyc", False, id="pycache"),
        pytest.param("node_modules/left-pad/index.js", False, id="node-modules"),
        pytest.param("web/node_modules/left-pad/index.js", False, id="nested-node-modules"),
        pytest.param(".venvs/notes.md", True, id="a-name-that-only-resembles-one"),
        pytest.param("notes/plan.md", True, id="the-work"),
    ],
)
def test_a_held_folder_folds_tool_caches_unless_told_otherwise(
    repository: Path, relative: str, enters: bool
) -> None:
    """A chat's scratch folder doubles as the agent's home, so interpreter caches,
    package indexes and virtual environments pile up inside the leased tree —
    one box carried seventeen thousand such files. None of it is the work, and
    the live sync folds it by default; a caller that names presets chooses."""
    clock = FakeClock()
    api = FakeLiveApi(root=repository, clock=clock)
    sync = make_sync(repository, api, clock)
    target = repository / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"x")

    sync.classify(Change.added, str(target))

    assert (relative in sync.metadata) is enters


def test_the_first_sweep_lists_the_tree_the_export_would_carry(repository: Path) -> None:
    clock = FakeClock()
    api = FakeLiveApi(root=repository, clock=clock)
    sync = make_sync(repository, api, clock, watcher=FakeWatcher([set()]))
    sync.exclude_presets = ("node_modules",)

    asyncio.run(sync.run())

    assert sorted(api.listed()) == [".git", ".git/HEAD", ".gitignore", "src", "src/main.py"]


def test_an_empty_folder_is_listed_and_a_second_look_sends_nothing(tree: Path) -> None:
    clock = FakeClock()
    api = FakeLiveApi(root=tree, clock=clock)
    sync = make_sync(tree, api, clock)
    (tree / "results").mkdir()

    sync.classify(Change.added, str(tree / "results"))
    sync.flush()
    assert api.trees == [[TreeEntry(op="upsert", path="results", kind="dir")]]

    sync.classify(Change.modified, str(tree / "results"))
    sync.classify(Change.modified, str(tree))
    sync.flush()
    assert len(api.trees) == 1


# -- the bytes, onto the rows ---------------------------------------------------


@dataclass
class TargetingApi(FakeLiveApi):
    """A drive that records the node an upload names, and nothing in its place."""

    targeted: list[tuple[str, str]] = field(default_factory=list)

    def upload(self, rel_path: str, node_id: str, size: int, **kwargs: Any) -> str | None:
        self.targeted.append((rel_path, node_id))
        return super().upload(rel_path, node_id, size, **kwargs)


def _rewrite(tree: Path, relative: str, body: bytes) -> Path:
    path = write(tree, relative, body)
    stamp = time.time() + 5
    os.utime(path, (stamp, stamp))
    return path


def _fresh(tree: Path, sync: LiveSync, api: TargetingApi) -> tuple[str, str]:
    sync.classify(Change.added, str(write(tree, "a.txt", b"alpha")))
    sync.flush()
    return "a.txt", api.nodes["a.txt"]


def _renamed(tree: Path, sync: LiveSync, api: TargetingApi) -> tuple[str, str]:
    _fresh(tree, sync, api)
    node = api.nodes["a.txt"]
    (tree / "a.txt").rename(tree / "b.txt")
    sync.classify(Change.deleted, str(tree / "a.txt"))
    sync.classify(Change.added, str(tree / "b.txt"))
    sync.flush()
    sync.classify(Change.modified, str(_rewrite(tree, "b.txt", b"beta, edited after the move")))
    sync.flush()
    return "b.txt", node


def _recreated(tree: Path, sync: LiveSync, api: TargetingApi) -> tuple[str, str]:
    _fresh(tree, sync, api)
    (tree / "a.txt").unlink()
    sync.classify(Change.deleted, str(tree / "a.txt"))
    sync.flush()
    sync.classify(Change.added, str(_rewrite(tree, "a.txt", b"a new file under the old name")))
    sync.flush()
    return "a.txt", api.nodes["a.txt"]


@pytest.mark.parametrize(
    "scenario",
    [
        pytest.param(_fresh, id="created-before-its-row"),
        pytest.param(_renamed, id="after-a-rename"),
        pytest.param(_recreated, id="after-a-delete-and-a-new-file"),
    ],
)
def test_an_upload_lands_on_the_row_the_tree_batch_made(
    tree: Path, scenario: Callable[[Path, LiveSync, TargetingApi], tuple[str, str]]
) -> None:
    clock = FakeClock()
    api = TargetingApi(root=tree, clock=clock)
    sync = make_sync(tree, api, clock)

    relative, node = scenario(tree, sync, api)

    assert api.targeted[-1] == (relative, node)
    assert api.nodes[relative] == node


def _held_move_setup(tree: Path, body_after: bytes) -> tuple[LiveSync, FakeLiveApi, str]:
    clock = FakeClock()
    api = FakeLiveApi(root=tree, clock=clock)
    source = write(tree, "draft.md", b"the same bytes")
    sync = make_sync(tree, api, clock)
    sync.classify(Change.added, str(source))
    sync.flush()
    node = api.nodes["draft.md"]
    source.unlink()
    write(tree, "final.md", body_after)
    watcher = TickingWatcher(
        clock,
        [
            {(Change.deleted, str(source)), (Change.added, str(tree / "final.md"))},
            set(),
            set(),
            set(),
        ],
        step=0.2,
    )
    sync.watcher = watcher
    sync.cadence = LiveCadence(settle_ms=0, metadata_every_ms=300, batch_every_ms=700)
    sync.fence.beat()
    return sync, api, node


def test_a_move_whose_rows_leave_first_is_still_one_rename(tree: Path) -> None:
    """The delete and the new name reach the metadata window before the
    content round has read a byte. Sent as they stood, they would trash the
    node carrying the file's history; held for that round, they are one
    rename."""
    sync, api, node = _held_move_setup(tree, b"the same bytes")

    asyncio.run(sync.run())

    assert api.renames == [(node, "draft.md", "final.md")]
    assert api.trashed == []
    assert api.nodes["final.md"] == node


def test_a_delete_beside_a_different_file_of_the_same_size_is_not_a_move(tree: Path) -> None:
    sync, api, node = _held_move_setup(tree, b"other the bytes")

    asyncio.run(sync.run())

    assert api.renames == []
    assert api.trashed == [node]
    assert api.nodes["final.md"] != node


def test_a_path_listed_again_is_no_longer_skipped_by_the_checkpoint_push(tree: Path) -> None:
    """A folder the box deleted is a tombstone the checkpoint push skips, with
    everything under it; a file written into it again lifts that, or the
    push would never send the new file."""
    clock = FakeClock()
    api = FakeLiveApi(root=tree, clock=clock)
    sync = make_sync(tree, api, clock)
    (tree / "out").mkdir()
    sync.classify(Change.added, str(tree / "out"))
    sync.flush()
    (tree / "out").rmdir()
    sync.classify(Change.deleted, str(tree / "out"))
    sync.flush()
    assert "out" in sync.tombstones

    sync.classify(Change.added, str(write(tree, "out/report.md", b"again")))
    sync.flush()

    assert not {"out", "out/report.md"} & sync.tombstones


# -- the tree route on the wire -------------------------------------------------


def _wired(
    tree: Path, handler: Callable[[httpx.Request], httpx.Response], *, inside: str = ""
) -> RestLiveApi:
    return RestLiveApi(
        files=_ListedFiles(_Route(clock=FakeClock()), "Chats/c"),
        http=httpx.Client(transport=httpx.MockTransport(handler), base_url="http://drive"),
        root=tree,
        drive_id="drive-1",
        lease_node_id="lease-9",
        dest="Chats/c/scratch" if inside else "Chats/c",
        inside=inside,
        push=lambda **_kwargs: None,
    )


ENTRIES = [
    TreeEntry(op="upsert", path="papers", kind="dir"),
    TreeEntry(
        op="upsert", path="papers/a.md", kind="file", size=4, mtime_ns=17, mode=0o644, hash="b3:ab"
    ),
    TreeEntry(
        op="rename",
        path="papers/b.md",
        from_="papers/b.tmp",
        kind="file",
        size=2,
        mtime_ns=18,
        mode=0o600,
        hash=None,
    ),
    TreeEntry(op="delete", path="old"),
]


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param({"live_seq": 9, "applied": 4, "landing_count": 2}, id="snake-case"),
        pytest.param({"liveSeq": 9, "applied": 4, "landingCount": 2}, id="camel-case"),
    ],
)
def test_a_tree_batch_posts_the_contract_under_the_leased_folder(
    tree: Path, answer: dict[str, int]
) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=answer)

    got = _wired(tree, handler, inside="scratch").tree("batch-1", ENTRIES, gzip_above=8192)

    assert got == TreeAnswer(live_seq=9, applied=4, landing_count=2)
    (request,) = seen
    assert (request.method, request.url.path) == (
        "POST",
        "/api/v1/files/drives/drive-1/items/lease-9/lease/tree",
    )
    assert "content-encoding" not in request.headers
    assert json.loads(request.content) == {
        "batch_id": "batch-1",
        "entries": [
            {"op": "upsert", "path": "scratch/papers", "kind": "dir"},
            {
                "op": "upsert",
                "path": "scratch/papers/a.md",
                "kind": "file",
                "size": 4,
                "mtime_ns": 17,
                "mode": 0o644,
                "hash": "b3:ab",
            },
            {
                "op": "rename",
                "path": "scratch/papers/b.md",
                "from": "scratch/papers/b.tmp",
                "kind": "file",
                "size": 2,
                "mtime_ns": 18,
                "mode": 0o600,
                "hash": None,
            },
            {"op": "delete", "path": "scratch/old"},
        ],
    }


@pytest.mark.parametrize(
    ("threshold", "compressed"),
    [pytest.param(64, True, id="above-the-threshold"), pytest.param(1 << 20, False, id="below")],
)
def test_a_tree_body_above_the_threshold_travels_gzipped(
    tree: Path, threshold: int, compressed: bool
) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"live_seq": 1, "applied": 4, "landing_count": 0})

    _wired(tree, handler).tree("batch-2", ENTRIES, gzip_above=threshold)

    (request,) = seen
    assert (request.headers.get("content-encoding") == "gzip") is compressed
    raw = gzip.decompress(request.content) if compressed else request.content
    assert json.loads(raw)["entries"][3] == {"op": "delete", "path": "old"}


def test_a_name_that_is_not_utf8_travels_byte_exact(tree: Path) -> None:
    seen: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.content)
        return httpx.Response(200, json={"live_seq": 1, "applied": 1, "landing_count": 0})

    latin1 = b"caf\xe9.txt".decode("utf-8", "surrogateescape")
    _wired(tree, handler).tree("b", [TreeEntry(op="delete", path=latin1)], gzip_above=8192)

    path = json.loads(seen[0])["entries"][0]["path"]
    assert path.encode("utf-8", "surrogateescape") == b"caf\xe9.txt"


@pytest.mark.parametrize(
    ("block", "expected"),
    [
        pytest.param(
            {"metadataEveryMs": 150, "metadataMaxEntries": 500, "metadataGzipBytes": 1024},
            (150, 500, 1024),
            id="served",
        ),
        pytest.param({"batchEveryMs": 900}, (300, 2000, 8192), id="an-older-server"),
    ],
)
def test_the_metadata_cadence_is_read_off_the_grant(
    block: dict[str, int], expected: tuple[int, int, int]
) -> None:
    cadence = LiveCadence.from_grant(block)

    assert (
        cadence.metadata_every_ms,
        cadence.metadata_max_entries,
        cadence.metadata_gzip_bytes,
    ) == expected
