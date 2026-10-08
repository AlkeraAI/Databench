"""ProjectFileWatcher — watch the repo (KB) + connections' source files (lineage); react to
add / modify / delete.

These pin: the change→(handles, kb_dirty) classification (classify / handles_for_changes); the
VCS-aware filtering (an explicit artifact file is always honored even when gitignored — dbt's
target/ — while a file merely UNDER a watched dir respects .gitignore via discover_files; a
deletion always reacts); the REPO-WIDE KB signal (any tracked file under the root marks kb_dirty,
even one no connection owns; a file outside the root does not); the self-trigger guard (the watch
filter drops .alkera/ + junk); the watched-dir computation; and that a REAL filesystem change
fires on_change.
"""

from __future__ import annotations

import asyncio
import gc
import logging
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from alkera_cli.files.tree_watch import Change
from alkera_cli.harness import file_watcher as file_watcher_module
from alkera_cli.harness.file_watcher import (
    ProjectFileWatcher,
    stop_all_watchers,
    watch_filter,
)

# The last two tests are a pair — one leaves a watch running, the next proves the suite ended it
# — which only means anything if they run in order on one worker. The automatic per-module group
# already does that; naming it is the written record that this module must not be spread.
pytestmark = pytest.mark.xdist_group("file_watcher")

# The suite-wide session loop shares its anyio thread limiter and default executor with
# every test that ran before on the same worker; leaked to_thread work there starves the
# watcher's awatch/classify thread hops past any deadline. A per-test loop gets fresh pools.
_OWN_LOOP = pytest.mark.asyncio(loop_scope="function")


def _conn(handle: str, **attributes: str) -> SimpleNamespace:
    return SimpleNamespace(handle=handle, attributes=dict(attributes))


async def _noop(_handles: set[str], _kb_dirty: bool) -> None:
    pass


def test_artifact_file_is_honored_even_when_gitignored(tmp_path: Path, monkeypatch: Any) -> None:
    # dbt's target/manifest.json is itself gitignored, but it's the connection's source of
    # truth — the watcher reacts regardless of what discover_files (gitignore) would list.
    manifest = tmp_path / "proj" / "target" / "manifest.json"
    monkeypatch.setattr(
        "alkera_cli.harness.repo_files.discover_files", lambda _root: []
    )  # "all ignored"
    w = ProjectFileWatcher([_conn("dbt", manifest_path=str(manifest))], _noop)
    assert w.handles_for_changes([(Change.modified, str(manifest))]) == {"dbt"}
    assert w.handles_for_changes([(Change.deleted, str(manifest))]) == {"dbt"}  # removal too


def test_dir_root_file_respects_gitignore(tmp_path: Path, monkeypatch: Any) -> None:
    dags = tmp_path / "dags"
    dags.mkdir()
    tracked = dags / "good_dag.py"
    ignored = dags / "ignored_dag.py"
    # discover_files (the existing gitignore-aware lister) lists only the tracked DAG.
    monkeypatch.setattr("alkera_cli.harness.repo_files.discover_files", lambda _root: [tracked])
    w = ProjectFileWatcher([_conn("af", dags_path=str(dags))], _noop)

    assert w.handles_for_changes([(Change.modified, str(tracked))]) == {"af"}  # tracked → react
    assert w.handles_for_changes([(Change.added, str(ignored))]) == set()  # gitignored → skip
    # A DELETION reacts regardless — a vanished file can't be gitignore-checked (gated no-op).
    assert w.handles_for_changes([(Change.deleted, str(ignored))]) == {"af"}


def test_handles_for_changes_is_one_to_many(tmp_path: Path) -> None:
    shared = tmp_path / "shared.duckdb"
    a = _conn("a", path=str(shared))
    b = _conn("b", path=str(shared))  # two connections backed by the same file
    c = _conn("c", path=str(tmp_path / "other.duckdb"))
    w = ProjectFileWatcher([a, b, c], _noop)
    assert w.handles_for_changes([(Change.modified, str(shared))]) == {"a", "b"}


def test_watch_dirs_are_the_existing_artifact_parents(tmp_path: Path) -> None:
    manifest = tmp_path / "proj" / "target" / "manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("{}")
    duck = tmp_path / "w.duckdb"
    duck.write_text("x")
    conns = [
        _conn("dbt", manifest_path=str(manifest)),
        _conn("dd", path=str(duck)),
        _conn("snow", account="acct"),  # live warehouse → no artifact → no watch dir
    ]
    w = ProjectFileWatcher(conns, _noop)  # no workspace_root → just the connection dirs
    assert set(w.watch_dirs) == {str(manifest.parent.resolve()), str(tmp_path.resolve())}


def test_watch_dirs_include_root_and_skip_in_tree_connection_dirs(tmp_path: Path) -> None:
    # WITH a workspace_root: the root is watched (recursive → covers in-tree connection dirs, which
    # are therefore NOT added separately); an OUT-OF-TREE artifact dir IS still added.
    (tmp_path / "dags").mkdir()  # in-tree → covered by the recursive root watch
    external = tmp_path.parent / f"{tmp_path.name}-ext"
    external.mkdir()
    (external / "w.duckdb").write_text("x")
    w = ProjectFileWatcher(
        [
            _conn("af", dags_path=str(tmp_path / "dags")),
            _conn("ext", path=str(external / "w.duckdb")),
        ],
        _noop,
        workspace_root=tmp_path,
    )
    assert set(w.watch_dirs) == {
        str(tmp_path.resolve()),
        str(external.resolve()),
    }  # root + ext only


def test_start_async_is_none_when_nothing_to_watch(tmp_path: Path) -> None:
    # Only a live warehouse + NO workspace_root → no files → no watch task.
    w = ProjectFileWatcher([_conn("snow", account="a")], _noop)
    assert w.watch_dirs == [] and w.start_async() is None


@_OWN_LOOP
async def test_real_filesystem_change_fires_on_change(tmp_path: Path) -> None:
    import contextlib

    duck = tmp_path / "w.duckdb"
    duck.write_text("v1")
    fired = asyncio.Event()
    captured: list[set[str]] = []

    async def _on_change(handles: set[str], _kb_dirty: bool) -> None:
        captured.append(handles)
        fired.set()

    # force_polling so the test doesn't depend on OS notifications firing in the sandbox.
    w = ProjectFileWatcher(
        [_conn("dd", path=str(duck))], _on_change, debounce_ms=50, force_polling=True
    )
    assert w.start_async() is not None
    try:
        # Keep nudging the watched file until it's reported — robust against poll/scheduling
        # jitter (each write is a fresh mtime, so a missed one is just retried).
        for i in range(40):  # up to ~10s
            if fired.is_set():
                break
            duck.write_text(f"v{i + 2}")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(fired.wait(), timeout=0.25)
        assert fired.is_set() and captured[0] == {"dd"}
    finally:
        await w.stop()


@_OWN_LOOP
async def test_watcher_restarts_after_a_transient_crash(tmp_path: Path, monkeypatch: Any) -> None:
    # A non-cancel exception from awatch (a watchfiles-internal / transient OS error) must NOT
    # kill the watcher permanently: _run re-enters awatch after a backoff, so file-driven refresh
    # self-heals without a daemon restart (the beat starts the watcher only once).
    target = tmp_path / "w.duckdb"
    target.write_text("v1")
    fired = asyncio.Event()

    async def _on_change(_handles: set[str], _kb_dirty: bool) -> None:
        fired.set()

    calls = {"n": 0}

    async def _fake_awatch(*_a: Any, **_k: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient watch failure")  # crash the first watch
        yield {(Change.modified, str(target))}  # restarted watch reports a change
        await asyncio.sleep(3600)  # then idle until stop() cancels

    monkeypatch.setattr("alkera_cli.files.tree_watch.watch_tree", _fake_awatch)
    monkeypatch.setattr("alkera_cli.harness.file_watcher._RESTART_BACKOFF_S", 0.01)

    w = ProjectFileWatcher([_conn("dd", path=str(target))], _on_change, debounce_ms=10)
    assert w.start_async() is not None
    try:
        await asyncio.wait_for(fired.wait(), timeout=3.0)
        assert calls["n"] >= 2  # crashed once, restarted, then watched
    finally:
        await w.stop()


# --- the repo-wide KB signal (classify / kb_dirty) -------------------------


def test_classify_marks_kb_dirty_for_a_tracked_non_connection_file(
    tmp_path: Path, monkeypatch: Any
) -> None:
    # The headline of this feature: the KB indexes the WHOLE repo, so a plain tracked source edit
    # owned by NO connection marks kb_dirty (handles empty). Previously such an edit only reached
    # the KB on the hourly cadence.
    src = tmp_path / "src" / "model.py"
    monkeypatch.setattr(
        "alkera_cli.harness.repo_files.discover_files", lambda _root: [src]
    )  # tracked
    w = ProjectFileWatcher([], _noop, workspace_root=tmp_path)
    assert w.classify([(Change.modified, str(src))]) == (set(), True)


def test_classify_does_not_mark_kb_dirty_for_a_gitignored_file(
    tmp_path: Path, monkeypatch: Any
) -> None:
    junk = tmp_path / "build" / "out.js"
    monkeypatch.setattr(
        "alkera_cli.harness.repo_files.discover_files", lambda _root: []
    )  # all ignored
    w = ProjectFileWatcher([], _noop, workspace_root=tmp_path)
    assert w.classify([(Change.modified, str(junk))]) == (set(), False)


def test_classify_gitignored_artifact_is_lineage_not_kb(tmp_path: Path, monkeypatch: Any) -> None:
    # dbt's target/manifest.json (under the root, gitignored): drives LINEAGE (artifact) but NOT
    # the KB (the KB never ingests it).
    manifest = tmp_path / "target" / "manifest.json"
    monkeypatch.setattr(
        "alkera_cli.harness.repo_files.discover_files", lambda _root: []
    )  # gitignored
    w = ProjectFileWatcher(
        [_conn("dbt", manifest_path=str(manifest))], _noop, workspace_root=tmp_path
    )
    assert w.classify([(Change.modified, str(manifest))]) == ({"dbt"}, False)


def test_classify_tracked_connection_dir_file_is_both(tmp_path: Path, monkeypatch: Any) -> None:
    # A tracked Airflow DAG under the connection's dags/ (under the root): LINEAGE (its connection)
    # AND KB (a tracked repo file).
    dags = tmp_path / "dags"
    dag = dags / "etl.py"
    monkeypatch.setattr(
        "alkera_cli.harness.repo_files.discover_files", lambda _root: [dag]
    )  # tracked
    w = ProjectFileWatcher([_conn("af", dags_path=str(dags))], _noop, workspace_root=tmp_path)
    assert w.classify([(Change.modified, str(dag))]) == ({"af"}, True)


def test_classify_file_outside_root_is_lineage_only(tmp_path: Path) -> None:
    # An out-of-tree connection artifact still drives LINEAGE but is NOT in the KB's repo walk.
    external = tmp_path.parent / f"{tmp_path.name}-ext" / "warehouse.duckdb"
    w = ProjectFileWatcher(
        [_conn("ext", path=str(external))], _noop, workspace_root=tmp_path / "repo"
    )
    assert w.classify([(Change.modified, str(external))]) == ({"ext"}, False)


def test_classify_deletion_under_root_marks_kb_dirty(tmp_path: Path, monkeypatch: Any) -> None:
    # A vanished tracked file can't be gitignore-checked → react (the re-seed drops its cards).
    gone = tmp_path / "src" / "removed.py"
    monkeypatch.setattr(
        "alkera_cli.harness.repo_files.discover_files", lambda _root: []
    )  # gone now
    w = ProjectFileWatcher([], _noop, workspace_root=tmp_path)
    assert w.classify([(Change.deleted, str(gone))])[1] is True


def test_watch_filter_drops_alkera_state_and_junk_but_keeps_source(tmp_path: Path) -> None:
    # The coarse OS-watch filter MUST drop our own .alkera/ writes (scheduler/progress/store/chat)
    # — else the repo-wide watch would self-trigger an endless refresh — plus the usual junk, while
    # passing real source AND the gitignored-but-watched dbt artifact (refined later by classify).
    f = watch_filter
    assert f(Change.modified, str(tmp_path / ".alkera" / "scheduler" / "job.json")) is False
    assert f(Change.modified, str(tmp_path / ".git" / "index")) is False
    assert f(Change.modified, str(tmp_path / "node_modules" / "p" / "i.js")) is False
    assert f(Change.modified, str(tmp_path / "__pycache__" / "m.pyc")) is False
    assert f(Change.modified, str(tmp_path / "src" / "model.py")) is True
    assert f(Change.modified, str(tmp_path / "target" / "manifest.json")) is True


@pytest.mark.parametrize(
    ("relative", "kept"),
    [
        pytest.param("src/model.py", True, id="source"),
        pytest.param("models/orders.sql", True, id="sql"),
        pytest.param("src/x.pyc", False, id="compiled-python"),
        pytest.param("src/x.pyo", False, id="optimised-python"),
        pytest.param("src/model.py.___jb_tmp___", False, id="jetbrains-temp"),
        pytest.param("src/.model.py.swp", False, id="vim-swap"),
        pytest.param("src/model.py~", False, id="backup"),
        pytest.param("src/.#model.py", False, id="emacs-lock"),
        pytest.param("src/flycheck_model.py", False, id="flycheck"),
        pytest.param("notes/.DS_Store", False, id="finder"),
        pytest.param("a/.hg/store", False, id="mercurial"),
        pytest.param("a/.svn/entries", False, id="subversion"),
        pytest.param("a/.tox/py/x", False, id="tox"),
        pytest.param("a/.venv/bin/python", False, id="virtualenv"),
        pytest.param("a/.idea/workspace.xml", False, id="jetbrains-project"),
        pytest.param("a/.mypy_cache/x.json", False, id="mypy-cache"),
        pytest.param("a/.pytest_cache/v", False, id="pytest-cache"),
        pytest.param("a/.hypothesis/examples", False, id="hypothesis"),
        pytest.param("src/model.swift", True, id="a-name-that-only-starts-like-swap"),
        pytest.param("src/my.alkera/notes.md", True, id="a-dir-only-named-like-ours"),
    ],
)
def test_the_watch_filter_drops_exactly_the_tool_litter(
    tmp_path: Path, relative: str, kept: bool
) -> None:
    assert watch_filter(Change.added, str(tmp_path / relative)) is kept


@_OWN_LOOP
async def test_real_filesystem_kb_refresh_for_a_non_connection_file(tmp_path: Path) -> None:
    import contextlib

    # No connections — just the repo-wide KB watch. Editing a plain tracked file fires on_change
    # with empty handles + kb_dirty=True (the gap this feature closes).
    notes = tmp_path / "notes.md"
    notes.write_text("v1")
    fired = asyncio.Event()
    captured: list[tuple[set[str], bool]] = []

    async def _on_change(handles: set[str], kb_dirty: bool) -> None:
        captured.append((handles, kb_dirty))
        fired.set()

    w = ProjectFileWatcher(
        [], _on_change, workspace_root=tmp_path, debounce_ms=50, force_polling=True
    )
    assert w.start_async() is not None  # the root watch runs even with ZERO connections
    try:
        for i in range(40):  # up to ~10s, robust against poll jitter (fresh mtime each write)
            if fired.is_set():
                break
            notes.write_text(f"v{i + 2}")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(fired.wait(), timeout=0.25)
        assert fired.is_set() and captured[0] == (set(), True)
    finally:
        await w.stop()


# --- stopping gives the worker thread back ---------------------------------
#
# watchfiles does its waiting inside an anyio worker thread, and a loop only ever hands out forty
# of those. A watch nobody stops is therefore not merely untidy: forty of them is every thread the
# loop will ever give, and everything else that needs one — a FastAPI route running a synchronous
# dependency, say — waits behind them for as long as the loop lives. That is not hypothetical: an
# xdist worker that ran the daemon-method tests and then a backend Files route module timed out
# every test in it, on requests that normally take milliseconds.


async def _until(predicate: Callable[[], bool], *, seconds: float = 10.0) -> bool:
    """Poll ``predicate`` until it holds or the budget runs out."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return predicate()


def _thread_limiter() -> Any:
    """The worker-thread allowance of the running loop — what a watch borrows from."""
    import anyio.to_thread

    return anyio.to_thread.current_default_thread_limiter()


@_OWN_LOOP
async def test_a_stopped_watch_gives_its_worker_thread_back(tmp_path: Path) -> None:
    limiter = _thread_limiter()
    borrowed = limiter.borrowed_tokens
    w = ProjectFileWatcher([], _noop, workspace_root=tmp_path)
    assert w.start_async() is not None
    assert await _until(lambda: limiter.borrowed_tokens > borrowed), "the watch never took a thread"

    await w.stop()
    assert not w.is_watching
    assert limiter.borrowed_tokens == borrowed


@_OWN_LOOP
async def test_stop_all_watchers_ends_a_watch_nobody_holds(tmp_path: Path) -> None:
    # A watch task is its own strong reference: drop every other reference to the watcher and it
    # keeps watching anyway, which is how a caller that is long gone still costs a worker thread.
    limiter = _thread_limiter()
    borrowed = limiter.borrowed_tokens
    ProjectFileWatcher([], _noop, workspace_root=tmp_path).start_async()
    assert await _until(lambda: limiter.borrowed_tokens > borrowed), "the watch never took a thread"
    gc.collect()
    assert limiter.borrowed_tokens > borrowed, "the watch stopped when its owner was collected"

    stop_all_watchers()
    assert await _until(lambda: limiter.borrowed_tokens == borrowed), "the thread never came back"


# --- a stop is bounded, and never costs the caller its own cancellation ------
#
# A watch ends the moment its cancellation lands. One that cannot be reached — an OS-watcher call
# that never returns — must not hold a shutdown or a teardown with it, and a caller that gives up
# on the wait must be the one to hear its own deadline: swallowed as the watch's own cancellation,
# the deadline never fired and the run was killed at the suite's limit with no test named.


def _a_watch_that_ignores_its_cancellation(monkeypatch: Any, obey: asyncio.Event) -> asyncio.Event:
    """Stand the OS watcher in with one whose wait cannot be cancelled until ``obey`` is
    set. Returns the event set once the watch is inside that wait."""
    from alkera_cli.files import tree_watch

    reached = asyncio.Event()

    async def awatch(*_paths: Any, **_kw: Any) -> Any:
        reached.set()
        while not obey.is_set():
            try:
                await obey.wait()
            except asyncio.CancelledError:
                continue
        return
        yield set()  # an async generator, like the real one

    monkeypatch.setattr(tree_watch, "watch_tree", awatch)
    return reached


async def test_a_stop_the_caller_gives_up_on_is_the_callers_to_hear(
    tmp_path: Path, monkeypatch: Any
) -> None:
    obey = asyncio.Event()
    reached = _a_watch_that_ignores_its_cancellation(monkeypatch, obey)
    w = ProjectFileWatcher([], _noop, workspace_root=tmp_path)
    task = w.start_async()
    assert task is not None
    await reached.wait()
    try:
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.1):
                await w.stop()
        assert task.cancelling(), "the watch was not even asked to stop"
    finally:
        obey.set()
        await _until(task.done)


async def test_a_watch_that_never_ends_is_abandoned_after_the_grace_period_and_said_so(
    tmp_path: Path, monkeypatch: Any, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(file_watcher_module, "STOP_TIMEOUT_S", 0.1)
    obey = asyncio.Event()
    reached = _a_watch_that_ignores_its_cancellation(monkeypatch, obey)
    w = ProjectFileWatcher([], _noop, workspace_root=tmp_path)
    task = w.start_async()
    assert task is not None
    await reached.wait()
    try:
        with caplog.at_level(logging.WARNING, logger="alkera_cli.harness.file_watcher"):
            async with asyncio.timeout(5.0):  # the test's own guard, far past the grace period
                await w.stop()
        assert not w.is_watching
        assert not task.done(), "abandoned means left running, not pretended ended"
        said = [r.getMessage() for r in caplog.records if "did not stop" in r.getMessage()]
        assert said and w.watch_dirs[0] in said[0] and "abandoning" in said[0], said
    finally:
        obey.set()
        await _until(task.done)


# --- and the suite does not let one survive a test -------------------------

#: Set by the test below for the one after it. The two are deliberately a pair: what the first
#: forgets, the second proves the suite ended. They stay on one worker because a module is one
#: xdist group, and they share the session event loop on purpose — the watch has to outlive a
#: test for there to be anything to prove.
_FORGOTTEN: list[tuple[ProjectFileWatcher, int]] = []


async def test_a_watch_the_test_forgets_is_still_running_when_the_test_ends(
    tmp_path: Path,
) -> None:
    limiter = _thread_limiter()
    borrowed = limiter.borrowed_tokens
    w = ProjectFileWatcher([], _noop, workspace_root=tmp_path)
    assert w.start_async() is not None
    assert await _until(lambda: limiter.borrowed_tokens > borrowed), "the watch never took a thread"
    _FORGOTTEN.append((w, borrowed))  # left running on purpose — nothing here stops it


async def test_the_suite_ends_a_watch_the_previous_test_forgot() -> None:
    (w, borrowed) = _FORGOTTEN[0]
    limiter = _thread_limiter()
    assert not w.is_watching, "a forgotten watch was still running in the next test"
    assert await _until(lambda: limiter.borrowed_tokens <= borrowed), (
        "a forgotten watch was still holding a worker thread in the next test"
    )
