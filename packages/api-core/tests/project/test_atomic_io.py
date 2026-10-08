"""Tests for atomic write helpers + JSONL iteration."""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from alkera_core import atomic_io
from alkera_core.atomic_io import (
    PRIVATE_FILE_MODE,
    append_line,
    append_line_safe,
    sweep_stale_temps,
    write_bytes_atomic,
    write_json_atomic,
    write_text_atomic,
)
from alkera_core.project.jsonl import iter_jsonl


def test_replace_retries_on_windows_sharing_violation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transient ``PermissionError`` from ``os.replace`` (Windows sharing) is
    retried, not propagated — the write still lands."""
    target = tmp_path / "policy.yml"
    target.write_text("old")
    calls = {"n": 0}
    real_replace = os.replace

    def _flaky(src: object, dst: object) -> None:
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError("sharing violation")
        real_replace(src, dst)

    monkeypatch.setattr(atomic_io.os, "replace", _flaky)
    monkeypatch.setattr(atomic_io, "_SHARING_BACKOFF_S", 0.0)
    write_text_atomic(target, "new")
    assert target.read_text() == "new"
    assert calls["n"] == 3  # two failures, then success


@pytest.mark.skipif(sys.platform != "win32", reason="real Windows open-handle semantics")
def test_replace_with_retry_rides_out_a_real_open_handle_win32(tmp_path: Path) -> None:
    """The monkeypatch test above SIMULATES the sharing violation; this proves
    it real: CPython's ``open()`` takes no FILE_SHARE_DELETE, so ``os.replace``
    onto the held file genuinely fails until the handle closes — which a timer
    does well inside the retry budget."""
    import threading

    dst = tmp_path / "held.txt"
    dst.write_text("old")
    tmp = tmp_path / "incoming.txt"
    tmp.write_text("new")

    handle = dst.open("r")
    releaser = threading.Timer(0.08, handle.close)
    releaser.start()
    try:
        atomic_io.replace_with_retry(tmp, dst)
    finally:
        releaser.cancel()
        if not handle.closed:
            handle.close()

    assert dst.read_text() == "new"
    assert not tmp.exists()


def test_replace_gives_up_after_retries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The retry is bounded: a destination that never frees up is a real failure
    the caller sees, not an endless spin."""
    target = tmp_path / "policy.yml"
    calls = {"n": 0}

    def _always_fail(src: object, dst: object) -> None:
        calls["n"] += 1
        raise PermissionError("locked")

    monkeypatch.setattr(atomic_io.os, "replace", _always_fail)
    monkeypatch.setattr(atomic_io, "_SHARING_BACKOFF_S", 0.0)
    with pytest.raises(PermissionError):
        write_text_atomic(target, "x")
    assert calls["n"] == atomic_io._SHARING_ATTEMPTS
    # the temp is swept on failure (no leak)
    assert [p for p in tmp_path.glob("*.tmp")] == []


def _deny_every_replace(monkeypatch: pytest.MonkeyPatch, message: str = "locked") -> None:
    def _always_fail(src: object, dst: object) -> None:
        raise PermissionError(13, message)

    monkeypatch.setattr(atomic_io.os, "replace", _always_fail)


def test_replace_backoff_doubles_up_to_a_cap_and_buys_over_a_second(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The schedule at its ceiling: pauses double until they hit the cap, and the
    whole budget adds up to seconds rather than the fifth of a second that a herd
    of sixteen concurrent writers was observed to exhaust."""
    delays: list[float] = []
    _deny_every_replace(monkeypatch)
    monkeypatch.setattr(atomic_io.time, "sleep", delays.append)

    with pytest.raises(PermissionError):
        atomic_io.replace_with_retry(tmp_path / "src", tmp_path / "dst", jitter=lambda: 1.0)

    assert delays == pytest.approx([0.01, 0.02, 0.04, 0.08, 0.16, 0.32, 0.4, 0.4, 0.4, 0.4, 0.4])
    assert sum(delays) > 2.0


def test_replace_backoff_is_jittered_so_a_herd_disperses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Writers that collide once must not retry in lockstep — an equal pause just
    re-synchronises them into the next collision. Every pause is an independent
    draw below its ceiling, so the herd spreads out instead."""
    delays: list[float] = []
    _deny_every_replace(monkeypatch)
    monkeypatch.setattr(atomic_io.time, "sleep", delays.append)

    with pytest.raises(PermissionError):
        atomic_io.replace_with_retry(tmp_path / "src", tmp_path / "dst")

    capped = delays[-4:]  # these share one ceiling, so only jitter can tell them apart
    assert len(set(capped)) == len(capped)
    assert all(0.0 <= delay <= atomic_io._SHARING_BACKOFF_CAP_S for delay in delays)


def test_a_refused_temp_sweep_does_not_mask_the_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows can refuse the cleanup unlink too, while a scanner still holds the
    temp. The caller must be told why the WRITE failed, not what the janitor hit;
    the leftover temp is `sweep_stale_temps`' problem, later."""
    target = tmp_path / "policy.yml"
    _deny_every_replace(monkeypatch, "the destination is held")
    monkeypatch.setattr(atomic_io, "_SHARING_BACKOFF_S", 0.0)

    def _refuse_unlink(self: Path, missing_ok: bool = False) -> None:
        raise PermissionError(13, "the temp is held")

    monkeypatch.setattr(Path, "unlink", _refuse_unlink)

    with pytest.raises(PermissionError, match="the destination is held"):
        write_text_atomic(target, "x")

    assert len(list(tmp_path.glob("*.tmp"))) == 1


def _deny_unlink_n_times(
    monkeypatch: pytest.MonkeyPatch, target: Path, denials: int, calls: dict[str, int]
) -> None:
    """Make deleting `target` refuse `denials` times the way Windows does when
    another process has the file open, then let the real delete through. Other
    paths are untouched, so pytest's own temp cleanup still works."""
    real_unlink = os.unlink

    def _flaky(path, **kwargs):
        if Path(os.fspath(path)) != target:
            real_unlink(path, **kwargs)
            return
        calls["n"] += 1
        if calls["n"] <= denials:
            raise PermissionError(13, "Access is denied")
        real_unlink(path, **kwargs)

    monkeypatch.setattr(atomic_io.os, "unlink", _flaky)
    monkeypatch.setattr(atomic_io, "_SHARING_BACKOFF_S", 0.0)


@pytest.mark.parametrize(
    "denials",
    [
        pytest.param(0, id="no_denial"),
        pytest.param(3, id="a_burst_of_denials"),
        pytest.param(atomic_io._SHARING_ATTEMPTS - 1, id="the_last_attempt_that_can_still_land"),
    ],
)
def test_unlink_rides_out_a_refused_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, denials: int
) -> None:
    """A delete a concurrent reader is blocking gets the same patience a rename
    does — the file is gone when the refusals clear."""
    target = tmp_path / "held.lock"
    target.write_text("payload")
    calls = {"n": 0}
    _deny_unlink_n_times(monkeypatch, target, denials, calls)

    atomic_io.unlink_with_retry(target)

    assert not target.exists()
    assert calls["n"] == denials + 1


def test_unlink_surfaces_a_denial_that_never_clears(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The budget is bounded, and its exhaustion is the caller's business: a
    caller that is told a file is gone while it is still there acts on a lie."""
    target = tmp_path / "held.lock"
    target.write_text("payload")
    calls = {"n": 0}
    _deny_unlink_n_times(monkeypatch, target, denials=10**6, calls=calls)

    with pytest.raises(PermissionError):
        atomic_io.unlink_with_retry(target)

    assert calls["n"] == atomic_io._SHARING_ATTEMPTS
    assert target.exists()


def test_unlink_of_an_absent_path_is_a_success(tmp_path: Path) -> None:
    """ "Gone" is the goal, so a path that is already gone needs no complaint —
    two releases of the same lock must not turn the second into an error."""
    atomic_io.unlink_with_retry(tmp_path / "never-existed")


@pytest.mark.skipif(sys.platform != "win32", reason="real Windows open-handle semantics")
def test_unlink_with_retry_rides_out_a_real_open_handle_win32(tmp_path: Path) -> None:
    """The monkeypatched tests above SIMULATE the refusal; this is the real one.
    A single reader's handle is enough to make the delete fail — which is what a
    lock file being polled by a crowd looks like — and the delete has to land
    anyway once the handle closes, because the delete is how a lock is released."""
    import threading

    target = tmp_path / "held.lock"
    target.write_text("payload")

    handle = target.open("r")
    releaser = threading.Timer(0.08, handle.close)
    releaser.start()
    try:
        atomic_io.unlink_with_retry(target)
    finally:
        releaser.cancel()
        if not handle.closed:
            handle.close()

    assert not target.exists()


def test_sweep_stale_temps_removes_old_only(tmp_path: Path) -> None:
    fresh = tmp_path / "a.json.deadbeef.tmp"
    stale = tmp_path / "b.json.cafef00d.tmp"
    fresh.write_text("x")
    stale.write_text("y")
    old = time.time() - 7200  # 2h ago
    os.utime(stale, (old, old))
    removed = sweep_stale_temps(tmp_path, older_than_seconds=3600.0)
    assert removed == 1
    assert fresh.exists()  # a live writer's temp is preserved
    assert not stale.exists()


def test_write_text_atomic_creates_file(tmp_path: Path) -> None:
    target = tmp_path / "out.txt"
    write_text_atomic(target, "hello")
    assert target.read_text() == "hello"


def test_write_text_atomic_overwrites(tmp_path: Path) -> None:
    target = tmp_path / "out.txt"
    write_text_atomic(target, "first")
    write_text_atomic(target, "second")
    assert target.read_text() == "second"


def test_write_text_atomic_does_not_leak_temp_files(tmp_path: Path) -> None:
    """After a successful write, no `.tmp` siblings remain."""
    target = tmp_path / "out.txt"
    for i in range(5):
        write_text_atomic(target, f"v{i}")
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == [], f"expected no .tmp files, found: {leftovers}"


def test_write_json_atomic_round_trips(tmp_path: Path) -> None:
    target = tmp_path / "data.json"
    payload = {"a": 1, "b": [1, 2, 3], "c": {"nested": True}}
    write_json_atomic(target, payload)
    assert json.loads(target.read_text()) == payload


def test_write_json_atomic_pretty_printed(tmp_path: Path) -> None:
    """Human-readable output — indent=2 + sort_keys=True for stable diffs."""
    target = tmp_path / "data.json"
    write_json_atomic(target, {"b": 1, "a": 2})
    text = target.read_text()
    # Sorted keys — "a" comes before "b"
    assert text.index('"a"') < text.index('"b"')
    # Indented — there's a newline + spaces
    assert "\n  " in text


@pytest.mark.skipif(
    sys.platform == "win32", reason="chmod mode bits are a POSIX concept; no-op on Windows"
)
def test_write_bytes_atomic_respects_mode(tmp_path: Path, require_effective_chmod: None) -> None:
    target = tmp_path / "secret"
    write_bytes_atomic(target, b"sensitive", mode=0o600)
    assert oct(target.stat().st_mode & 0o777) == "0o600"


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="a chmod-0500 dir does not block file creation on Windows; the no-leftover "
    "invariant is covered cross-platform by test_replace_gives_up_after_retries",
)
def test_write_bytes_atomic_cleans_up_on_failure(
    tmp_path: Path, require_effective_chmod: None
) -> None:
    """If `os.replace` failed (we simulate by using a read-only parent),
    no temp files are left behind."""
    # Make parent read-only.
    target = tmp_path / "subdir" / "out.txt"
    target.parent.mkdir()
    target.parent.chmod(0o500)
    try:
        with pytest.raises(PermissionError):
            write_bytes_atomic(target, b"data")
    finally:
        target.parent.chmod(0o700)
    leftovers = [p.name for p in target.parent.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


def test_append_line_writes_with_newline(tmp_path: Path) -> None:
    target = tmp_path / "log.jsonl"
    append_line(target, json.dumps({"a": 1}))
    append_line(target, json.dumps({"a": 2}))
    lines = target.read_text().splitlines()
    assert lines == ['{"a": 1}', '{"a": 2}']


def test_append_line_adds_missing_newline(tmp_path: Path) -> None:
    target = tmp_path / "log.jsonl"
    append_line(target, "no-newline")  # caller forgot
    assert target.read_text() == "no-newline\n"


def test_append_line_creates_parent(tmp_path: Path) -> None:
    target = tmp_path / "deep" / "nested" / "log.jsonl"
    append_line(target, "x")
    assert target.exists()


def test_append_line_safe_terminates_torn_tail(tmp_path: Path) -> None:
    # Simulate a writer killed mid-line: the file ends without a newline. A
    # naive append would glue the next JSON onto the fragment; append_line_safe
    # must close the fragment first so the new line is independently parseable.
    target = tmp_path / "log.jsonl"
    target.write_bytes(b'{"a": 1}\n{"a": 2')  # second line torn
    append_line_safe(target, json.dumps({"a": 3}))
    raw = target.read_text()
    assert raw == '{"a": 1}\n{"a": 2\n{"a": 3}\n'
    # iter_jsonl recovers the two clean records and drops the torn fragment.
    assert list(iter_jsonl(target)) == [{"a": 1}, {"a": 3}]


def test_append_line_safe_no_extra_newline_when_tail_is_clean(tmp_path: Path) -> None:
    target = tmp_path / "log.jsonl"
    append_line_safe(target, json.dumps({"a": 1}))
    append_line_safe(target, json.dumps({"a": 2}))
    assert target.read_text() == '{"a": 1}\n{"a": 2}\n'


def test_append_line_safe_on_missing_and_empty_file(tmp_path: Path) -> None:
    missing = tmp_path / "new.jsonl"
    append_line_safe(missing, "x")
    assert missing.read_text() == "x\n"
    empty = tmp_path / "empty.jsonl"
    empty.touch()
    append_line_safe(empty, "y")
    assert empty.read_text() == "y\n"


@pytest.fixture
def permissive_umask() -> Iterator[None]:
    """The usual 022 a laptop shell starts with, under which a file made with
    no explicit mode is readable by every local user."""
    previous = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(previous)


def _mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


_POSIX_MODES = pytest.mark.skipif(
    sys.platform == "win32", reason="mode bits are a POSIX concept; chmod is a no-op on Windows"
)


@_POSIX_MODES
@pytest.mark.parametrize(
    "write",
    [
        pytest.param(lambda p: write_bytes_atomic(p, b"state"), id="bytes"),
        pytest.param(lambda p: write_text_atomic(p, "state"), id="text"),
        pytest.param(lambda p: write_json_atomic(p, {"state": 1}), id="json"),
        pytest.param(lambda p: append_line(p, "{}"), id="append"),
    ],
)
def test_alkera_state_is_owner_only_whatever_the_umask(
    tmp_path: Path, permissive_umask: None, require_effective_chmod: None, write: Any
) -> None:
    target = tmp_path / "state"
    write(target)
    assert _mode(target) == PRIVATE_FILE_MODE == 0o600


@_POSIX_MODES
def test_rewriting_a_world_readable_store_closes_it(
    tmp_path: Path, permissive_umask: None, require_effective_chmod: None
) -> None:
    """A store an earlier build wrote 0644 is closed by its next write: the
    rename puts a new file in its place, never the old one's mode."""
    target = tmp_path / "cost_state.json"
    target.write_text("{}")
    target.chmod(0o644)
    write_json_atomic(target, {"spent": 1})
    assert _mode(target) == 0o600


@_POSIX_MODES
def test_appending_keeps_the_mode_an_existing_file_has(
    tmp_path: Path, permissive_umask: None, require_effective_chmod: None
) -> None:
    target = tmp_path / "log.jsonl"
    target.write_text("")
    target.chmod(0o640)
    append_line(target, "{}")
    assert _mode(target) == 0o640


@_POSIX_MODES
def test_a_project_file_follows_the_umask_when_asked(
    tmp_path: Path, permissive_umask: None, require_effective_chmod: None
) -> None:
    """``mode=None`` is for the person's own work (a file they may commit):
    it is made as any file they make, never closed by Alkera."""
    target = tmp_path / "graph.yml"
    write_text_atomic(target, "nodes: []\n", mode=None)
    assert _mode(target) == 0o644
