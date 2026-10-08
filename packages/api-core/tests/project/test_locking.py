"""FileLock unit tests — single-actor semantics, same-process thread
concurrency, cross-process concurrency (subprocess hammers + a SIGKILL'd
holder), and the stale-reclaim protocol. Chat-store-level concurrent-access
tests live in `packages/api-core/tests/project/chats/test_concurrent_access.py`.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import pytest
from alkera_core.process import process_start_id
from alkera_core.project.locking import (
    FileLock,
    LockHeldError,
    _link_excl,
    _process_alive,
    acquire,
    live_holder,
)


def test_process_alive_delegates_cross_platform() -> None:
    """`_process_alive` is now a thin cross-platform delegate — no win32
    NotImplementedError, alive for self, dead for a reaped pid."""
    assert _process_alive(os.getpid()) is True
    assert _process_alive(_find_dead_pid()) is False


def test_link_excl_publishes_then_fails_on_existing(tmp_path: Path) -> None:
    """`_link_excl` is the cross-platform exclusive publish: it lands the
    payload, and a second publish to the same destination raises
    FileExistsError (the semantics every lock acquire relies on)."""
    src1 = tmp_path / "src1"
    src1.write_text("first")
    dst = tmp_path / "dst"
    _link_excl(src1, dst)
    assert dst.read_text() == "first"

    src2 = tmp_path / "src2"
    src2.write_text("second")
    with pytest.raises(FileExistsError):
        _link_excl(src2, dst)
    assert dst.read_text() == "first"  # the live file is never clobbered


def test_acquire_and_release_basic(tmp_path: Path) -> None:
    lock = acquire(tmp_path / ".lock")
    try:
        assert lock.held is True
        assert (tmp_path / ".lock").exists()
        payload = json.loads((tmp_path / ".lock").read_bytes())
        assert payload["pid"] == os.getpid()
        assert "nonce" in payload
    finally:
        lock.release()
    assert lock.held is False
    assert not (tmp_path / ".lock").exists()


def test_release_is_idempotent(tmp_path: Path) -> None:
    lock = acquire(tmp_path / ".lock")
    lock.release()
    lock.release()  # no-op, no exception
    assert not (tmp_path / ".lock").exists()


def test_context_manager(tmp_path: Path) -> None:
    path = tmp_path / ".lock"
    with FileLock(path) as lock:
        assert lock.held
        assert path.exists()
    assert not path.exists()


def test_same_process_double_acquire_raises(tmp_path: Path) -> None:
    """Acquiring the same lock twice (same process) raises — recursion
    would silently invite double-release bugs."""
    lock1 = acquire(tmp_path / ".lock")
    try:
        with pytest.raises(LockHeldError):
            acquire(tmp_path / ".lock")
    finally:
        lock1.release()


def test_release_after_lock_file_disappears(tmp_path: Path) -> None:
    """If the lock file is gone by the time we try to release (e.g.
    something nuked it manually), release is still a no-op + flips
    the in-memory flag."""
    lock = acquire(tmp_path / ".lock")
    (tmp_path / ".lock").unlink()
    lock.release()
    assert lock.held is False


def test_stale_lock_reclaimed_when_pid_is_dead(tmp_path: Path) -> None:
    """Plant a lock file with a PID that's guaranteed dead, then
    acquire — reclaim path must succeed silently."""
    path = tmp_path / ".lock"
    dead_pid = _find_dead_pid()
    import socket as _s

    payload = {
        "pid": dead_pid,
        "host": _s.gethostname(),
        "acquired_at": "2024-01-01T00:00:00Z",
        "nonce": "0" * 16,
    }
    path.write_text(json.dumps(payload))

    lock = acquire(path)
    try:
        assert lock.held
        # Our lock should have the current process's PID, not the dead one.
        assert lock.payload is not None
        assert lock.payload["pid"] == os.getpid()
    finally:
        lock.release()


def _same_host_holder(pid: int, **extra: object) -> dict[str, object]:
    """A lock payload as a holder on this host would leave it, plus ``extra``."""
    import socket as _s

    return {
        "pid": pid,
        "host": _s.gethostname(),
        "acquired_at": "2024-01-01T00:00:00Z",
        "nonce": "0" * 16,
        **extra,
    }


def _crashed_holder_payload() -> dict[str, object]:
    """The lock a holder that died without releasing leaves behind: a real
    child's pid and the creation stamp the kernel gave that child, captured
    while it was alive — exactly what ``acquire()`` writes, minus the release
    that never came. Once the child is dead its pid is the kernel's to hand
    out again, so the stamp is what keeps this payload stale whoever owns the
    number next."""
    import subprocess
    import sys

    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        start_id = process_start_id(proc.pid)
        assert start_id is not None
    finally:
        proc.kill()
        proc.wait(timeout=30)
    assert not _process_alive(proc.pid)
    return _same_host_holder(proc.pid, start_id=start_id)


def test_acquiring_records_the_holders_process_start_stamp(tmp_path: Path) -> None:
    """The payload names WHICH incarnation of the pid holds the lock, so a
    later process handed the same number is not mistaken for the holder."""
    path = tmp_path / ".lock"
    lock = acquire(path)
    try:
        own = process_start_id(os.getpid())
        assert own is not None
        assert lock.payload is not None
        assert lock.payload["start_id"] == own
        assert json.loads(path.read_text())["start_id"] == own
    finally:
        lock.release()


def test_crashed_holder_with_its_start_stamp_is_reclaimed(tmp_path: Path) -> None:
    """The stale shape a crashed writer leaves: dead pid + the stamp it had."""
    path = tmp_path / ".lock"
    corpse = _crashed_holder_payload()
    path.write_text(json.dumps(corpse))

    assert live_holder(path) is None
    lock = acquire(path)
    try:
        assert lock.payload is not None
        assert lock.payload["pid"] == os.getpid()
        rotated = [p for p in tmp_path.iterdir() if ".stale." in p.name]
        assert len(rotated) == 1
        assert json.loads(rotated[0].read_text())["pid"] == corpse["pid"]
    finally:
        lock.release()


def test_reused_pid_is_reclaimed_not_held(tmp_path: Path) -> None:
    """A dead holder's pid handed to an unrelated live process — Windows does
    this within milliseconds on a loaded machine — must NOT wedge the lock.

    This process stands in for the stranger: its pid is unmistakably alive,
    but the recorded stamp belongs to an earlier incarnation of the number. A
    liveness probe alone vouches for the stranger and answers "held" forever;
    the pre-read (``live_holder``) and the reclaim path must both see a stale
    holder and take the path over.
    """
    path = tmp_path / ".lock"
    own = process_start_id(os.getpid())
    assert own is not None
    earlier_incarnation = own - 1
    path.write_text(json.dumps(_same_host_holder(os.getpid(), start_id=earlier_incarnation)))

    assert live_holder(path) is None
    lock = acquire(path)
    try:
        assert lock.held
        assert lock.payload is not None
        assert lock.payload["start_id"] == own
        rotated = [p for p in tmp_path.iterdir() if ".stale." in p.name]
        assert len(rotated) == 1
        assert json.loads(rotated[0].read_text())["start_id"] == earlier_incarnation
    finally:
        lock.release()


def test_live_holder_with_its_own_start_stamp_stays_held(tmp_path: Path) -> None:
    """The stamp only ever RELEASES a lock from a stranger; a holder whose
    stamp matches its live pid is still the holder, to the pre-read and to
    ``acquire`` alike."""
    path = tmp_path / ".lock"
    own = process_start_id(os.getpid())
    assert own is not None
    path.write_text(json.dumps(_same_host_holder(os.getpid(), start_id=own)))

    assert live_holder(path) == _same_host_holder(os.getpid(), start_id=own)
    with pytest.raises(LockHeldError):
        acquire(path)
    assert sorted(p.name for p in tmp_path.iterdir()) == [".lock"]


def test_unreadable_start_stamp_leaves_a_live_holder_held(tmp_path: Path, monkeypatch) -> None:
    """When the stamp of a live pid cannot be read the question is undecided,
    and undecided means held — never a reclaim on a guess."""
    import alkera_core.project.locking as locking

    path = tmp_path / ".lock"
    path.write_text(json.dumps(_same_host_holder(os.getpid(), start_id=12345)))
    monkeypatch.setattr(locking, "process_start_id", lambda _pid: None)

    assert live_holder(path) is not None
    with pytest.raises(LockHeldError):
        acquire(path)
    assert sorted(p.name for p in tmp_path.iterdir()) == [".lock"]


_ABSENT = object()


@pytest.mark.parametrize(
    "stamp",
    [
        pytest.param(_ABSENT, id="absent"),
        pytest.param(None, id="null"),
        pytest.param("1789739015996944", id="string"),
        pytest.param(True, id="bool"),
    ],
)
def test_holder_without_a_usable_start_stamp_is_judged_by_liveness_alone(
    tmp_path: Path, stamp: object
) -> None:
    """A lock written before stamps existed (or with a malformed one) keeps
    today's contract: a live same-host pid is the holder."""
    path = tmp_path / ".lock"
    payload = _same_host_holder(os.getpid())
    if stamp is not _ABSENT:
        payload["start_id"] = stamp
    path.write_text(json.dumps(payload))

    assert live_holder(path) is not None
    with pytest.raises(LockHeldError):
        acquire(path)
    assert sorted(p.name for p in tmp_path.iterdir()) == [".lock"]


def test_unparseable_lock_treated_as_stale(tmp_path: Path) -> None:
    """A garbled lock file (e.g. half-written by a crashed writer)
    counts as stale — next acquirer reclaims."""
    path = tmp_path / ".lock"
    path.write_text("this is not json")
    lock = acquire(path)
    try:
        assert lock.held
    finally:
        lock.release()


def test_empty_lock_treated_as_stale(tmp_path: Path) -> None:
    path = tmp_path / ".lock"
    path.touch()  # 0-byte file
    lock = acquire(path)
    try:
        assert lock.held
    finally:
        lock.release()


def test_different_host_lock_is_held(tmp_path: Path) -> None:
    """We can't probe a remote process; treat as live → refuse."""
    path = tmp_path / ".lock"
    payload = {
        "pid": 1,  # PID 1 is alive on every UNIX, but the host check fires first
        "host": "some-other-machine.example.com",
        "acquired_at": "2024-01-01T00:00:00Z",
        "nonce": "0" * 16,
    }
    path.write_text(json.dumps(payload))
    with pytest.raises(LockHeldError) as exc:
        acquire(path)
    assert "some-other-machine.example.com" in str(exc.value)


def test_reclaim_rotates_stale_lock_aside(tmp_path: Path) -> None:
    """Stale locks are moved aside with `.stale.<ts>` suffix — useful
    for forensics, GC can sweep them later."""
    path = tmp_path / ".lock"
    import socket as _s

    dead_pid = _find_dead_pid()
    payload = {
        "pid": dead_pid,
        "host": _s.gethostname(),
        "acquired_at": "2024-01-01T00:00:00Z",
        "nonce": "0" * 16,
    }
    path.write_text(json.dumps(payload))

    lock = acquire(path)
    try:
        rotated = [p for p in tmp_path.iterdir() if ".stale." in p.name]
        assert len(rotated) == 1
        # Original stale payload preserved for forensics
        assert json.loads(rotated[0].read_text())["pid"] == dead_pid
    finally:
        lock.release()


def test_release_does_not_clobber_a_different_holders_lock(tmp_path: Path) -> None:
    """If our lock file got reclaimed by someone else (different
    nonce), our late release MUST NOT delete it."""
    lock = acquire(tmp_path / ".lock")
    # Simulate: someone reclaimed our lock and wrote a fresh payload
    # with a different nonce.
    fresh_payload = {
        "pid": os.getpid(),
        "host": "doesnt-matter",
        "acquired_at": "2026-01-01T00:00:00Z",
        "nonce": "a-DIFFERENT-nonce",
    }
    (tmp_path / ".lock").write_text(json.dumps(fresh_payload))
    # Now release our (in-memory) lock — should NOT delete the file.
    lock.release()
    assert (tmp_path / ".lock").exists()
    assert json.loads((tmp_path / ".lock").read_text())["nonce"] == "a-DIFFERENT-nonce"
    # Clean up so tmp_path teardown is happy.
    (tmp_path / ".lock").unlink()


# ---------------------------------------------------------------------------
# retrying_lock, the spin-on-contention primitive
# ---------------------------------------------------------------------------


def test_retrying_lock_acquires_and_releases(tmp_path: Path) -> None:
    from alkera_core.project.locking import retrying_lock

    lock = FileLock(tmp_path / ".lock")
    with retrying_lock(lock):
        assert lock.held is True
        assert (tmp_path / ".lock").exists()
    assert lock.held is False
    assert not (tmp_path / ".lock").exists()


def test_retrying_lock_waits_out_a_live_holder(tmp_path: Path) -> None:
    """A held lock is contention to spin out, not a fail-fast — once the holder
    releases (here, mid-poll), the retrying acquirer wins instead of raising."""
    from alkera_core.project.locking import retrying_lock

    path = tmp_path / ".lock"
    holder = acquire(path)  # someone else holds it (same process, different instance)
    released = {"done": False}

    # Release the holder shortly after we start polling — simulate contention
    # clearing. We can't truly background it in one process, so release first,
    # then prove retrying_lock still acquires within the timeout.
    holder.release()
    released["done"] = True

    waiter = FileLock(path)
    with retrying_lock(waiter, timeout_seconds=1.0):
        assert released["done"] is True
        assert waiter.held is True


def test_retrying_lock_times_out_on_a_permanent_live_holder(tmp_path: Path) -> None:
    """If the live holder never lets go, the retry budget is exhausted and the
    underlying ``LockHeldError`` propagates (not a silent hang)."""
    from alkera_core.project.locking import retrying_lock

    path = tmp_path / ".lock"
    holder = acquire(path)
    try:
        waiter = FileLock(path)
        with pytest.raises(LockHeldError):
            with retrying_lock(waiter, timeout_seconds=0.05, poll_interval_seconds=0.005):
                pass  # pragma: no cover — never reached
    finally:
        holder.release()


def test_a_waiter_backs_off_while_the_same_holder_keeps_the_lock(tmp_path: Path) -> None:
    """A crowd polling one lock file at the opening rate is a load the holder has
    to fight to release. So the interval doubles toward its ceiling for as long as
    the same holder is in place: a hold that lasts minutes is waited out in tens
    of polls, not tens of thousands.

    The sleep is injected, so the schedule is asserted rather than lived — and
    releasing the holder from inside it is how the wait ends deterministically.
    """
    from alkera_core.project.locking import retrying_lock

    path = tmp_path / ".lock"
    holder = acquire(path)
    pauses: list[float] = []

    def record(seconds: float) -> None:
        pauses.append(seconds)
        if len(pauses) == 8:
            holder.release()

    waiter = FileLock(path)
    with retrying_lock(waiter, timeout_seconds=60.0, sleep=record, jitter=lambda: 1.0):
        assert waiter.held is True

    assert pauses == pytest.approx([0.005, 0.01, 0.02, 0.04, 0.08, 0.1, 0.1, 0.1])


def test_a_waiter_polls_fast_again_when_the_lock_changes_hands(tmp_path: Path) -> None:
    """Backing off is for a holder that is not moving. A lock that changes hands
    is a queue draining, and the next gap is worth catching — so a new holder
    resets the interval to the opening one instead of inheriting the crowd's
    patience and starving this waiter behind every other contender."""
    from alkera_core.project.locking import retrying_lock

    path = tmp_path / ".lock"
    first = acquire(path)
    second = FileLock(path)
    pauses: list[float] = []

    def record(seconds: float) -> None:
        pauses.append(seconds)
        if len(pauses) == 3:
            first.release()
            second.acquire()  # a different claim, with its own nonce
        elif len(pauses) == 5:
            second.release()

    waiter = FileLock(path)
    with retrying_lock(waiter, timeout_seconds=60.0, sleep=record, jitter=lambda: 1.0):
        assert waiter.held is True

    assert pauses == pytest.approx([0.005, 0.01, 0.02, 0.005, 0.01])


def test_a_waiter_jitters_its_pauses_so_a_crowd_disperses(tmp_path: Path) -> None:
    """Waiters that collide once must not collide again on every poll. Half of
    each pause is a fresh draw, so pauses that share a ceiling still differ —
    and none of them is zero, which would put the crowd back on the file."""
    from alkera_core.project.locking import retrying_lock

    path = tmp_path / ".lock"
    holder = acquire(path)
    pauses: list[float] = []

    def record(seconds: float) -> None:
        pauses.append(seconds)
        if len(pauses) == 12:
            holder.release()

    waiter = FileLock(path)
    with retrying_lock(waiter, timeout_seconds=60.0, sleep=record):
        pass

    capped = pauses[-5:]  # these share one ceiling, so only jitter tells them apart
    assert len(set(capped)) == len(capped)
    assert all(0.05 <= pause <= 0.1 for pause in capped)
    assert min(pauses) > 0.0


# ---------------------------------------------------------------------------
# Cost of waiting — a refused acquisition must not write
# ---------------------------------------------------------------------------


def test_polling_a_live_holder_writes_nothing_until_it_releases(
    tmp_path: Path, monkeypatch
) -> None:
    """A waiter learns "still held" by READING the lock file, never by writing.

    Every refused acquisition used to stage a file, fsync it and unlink it
    again just to be told the path was taken, so one waiter on a held lock
    churned the lock's directory at the poll rate — the cost that made a
    handful of contenders on a sub-second hold take tens of seconds. The
    holder's pid is already on disk, so the answer costs one read.

    ``_write_excl`` is the module's only file-creating primitive, so counting
    its calls counts every byte a waiter puts on disk.
    """
    import alkera_core.project.locking as locking

    path = tmp_path / ".lock"
    holder = acquire(path)
    # Instrument only AFTER the holder is established, so every recorded write
    # belongs to the waiter.
    writes: list[Path] = []
    real_write_excl = locking._write_excl

    def counting_write_excl(p: Path, payload: dict) -> None:
        writes.append(p)
        real_write_excl(p, payload)

    monkeypatch.setattr(locking, "_write_excl", counting_write_excl)

    waiter = FileLock(path)
    for _ in range(50):
        with pytest.raises(LockHeldError):
            waiter.acquire()
    assert writes == []
    # Nothing was rotated aside either: a live lock is never mistaken for stale.
    assert sorted(p.name for p in tmp_path.iterdir()) == [".lock"]

    holder.release()
    waiter.acquire()
    try:
        assert waiter.held is True
        # Exactly one write, the create that won the path.
        assert writes == [path]
    finally:
        waiter.release()


def test_reading_a_free_path_does_not_grant_the_lock(tmp_path: Path, monkeypatch) -> None:
    """Reading before writing is an economy, not the arbiter.

    Two contenders can both read an absent lock file and both conclude "free" —
    the exclusive create still has to break the tie. Here a competitor takes the
    path in exactly that window (after our acquirer read it as free, before its
    own create lands), so an acquirer that trusted its read would end up a
    second holder. It must lose to the create instead.
    """
    import alkera_core.project.locking as locking

    path = tmp_path / ".lock"
    assert not path.exists()  # genuinely free when the acquirer looks
    real_write_excl = locking._write_excl
    competitor: list[FileLock] = []
    fired = {"done": False}

    def a_competitor_wins_the_path_first(p: Path, payload: dict) -> None:
        if p == path and not fired["done"]:
            fired["done"] = True
            competitor.append(acquire(path))
        real_write_excl(p, payload)

    monkeypatch.setattr(locking, "_write_excl", a_competitor_wins_the_path_first)
    try:
        with pytest.raises(LockHeldError):
            FileLock(path).acquire()
        # The competitor still owns the path, untouched.
        assert competitor[0].payload is not None
        assert json.loads(path.read_text())["nonce"] == competitor[0].payload["nonce"]
        assert [p.name for p in tmp_path.iterdir() if ".stale." in p.name] == []
    finally:
        monkeypatch.undo()
        for lock in competitor:
            lock.release()


# ---------------------------------------------------------------------------
# Releasing against a reader that blocks the delete
#
# Windows refuses to unlink a file another process merely has open for reading,
# and a lock a crowd is polling is open almost continuously. A release that
# gives up there leaves a file naming a pid that is very much alive and holding
# nothing, so every waiter reads "held" until its own timeout — the shape that
# stalled a 32-worker CI shard behind a template build that had long finished.
# Injected rather than provoked with a real handle, so the contract holds on
# every OS instead of only the one that produces the refusal.
# ---------------------------------------------------------------------------


def _deny_lock_delete(monkeypatch, path: Path, denials: int, calls: dict[str, int]) -> None:
    """Refuse to delete `path` `denials` times with the error Windows raises for
    a file another process has open, then let the delete through. Every other
    path deletes normally, so staging files and tmp_path teardown are unaffected."""
    from alkera_core import atomic_io

    real_unlink = os.unlink

    def _flaky(target, **kwargs):
        if Path(os.fspath(target)) != path:
            real_unlink(target, **kwargs)
            return
        calls["n"] += 1
        if calls["n"] <= denials:
            raise PermissionError(13, "Access is denied")
        real_unlink(target, **kwargs)

    monkeypatch.setattr(atomic_io.os, "unlink", _flaky)
    monkeypatch.setattr(atomic_io, "_SHARING_BACKOFF_S", 0.0)


@pytest.mark.parametrize(
    "denials",
    [
        pytest.param(1, id="one_refusal"),
        pytest.param(5, id="a_burst_of_refusals"),
    ],
)
def test_a_release_rides_out_a_refused_delete(tmp_path: Path, monkeypatch, denials: int) -> None:
    """The delete IS the release, so a transient refusal is waited out rather
    than accepted: the file goes, and the next acquirer gets the lock."""
    path = tmp_path / ".lock"
    holder = acquire(path)
    calls = {"n": 0}
    _deny_lock_delete(monkeypatch, path, denials, calls)

    holder.release()

    assert calls["n"] == denials + 1
    assert not path.exists()
    monkeypatch.undo()
    next_holder = acquire(path)
    next_holder.release()


def test_a_release_that_can_never_delete_frees_the_lock_and_says_so(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    """A refusal that outlasts the retry budget must not end in a file that
    names this live process as the holder — the next acquirer would be told the
    lock is taken by a process that released it, and would keep being told that
    for as long as this one runs. The claim is ended in the payload instead,
    and the operator hears about the file that could not be removed.
    """
    path = tmp_path / ".lock"
    holder = acquire(path)
    calls = {"n": 0}
    _deny_lock_delete(monkeypatch, path, denials=10**6, calls=calls)

    with caplog.at_level(logging.WARNING, logger="alkera_core.project.locking"):
        holder.release()

    assert holder.held is False
    # The file survived the release — and yet nothing claims the lock.
    assert path.exists()
    assert live_holder(path) is None
    assert json.loads(path.read_text())["released"] is True
    assert any("could not be deleted" in record.message for record in caplog.records)

    # And the point of all of it: the lock is takeable again.
    monkeypatch.undo()
    next_holder = acquire(path)
    try:
        assert next_holder.held is True
    finally:
        next_holder.release()


def test_a_release_that_can_neither_delete_nor_mark_released_raises(
    tmp_path: Path, monkeypatch
) -> None:
    """When the fallback fails too, the lock file really is stuck claiming a
    live pid. Returning normally there would hand the caller a lie it cannot
    detect; the instance still frees, and the failure is raised."""
    path = tmp_path / ".lock"
    holder = acquire(path)
    _deny_lock_delete(monkeypatch, path, denials=10**6, calls={"n": 0})
    real_open = Path.open

    def _refuse_writes(self: Path, mode: str = "r", *args, **kwargs):
        if self == path and ("+" in mode or "w" in mode or "a" in mode):
            raise PermissionError(13, "Access is denied")
        return real_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", _refuse_writes)

    with pytest.raises(PermissionError):
        holder.release()

    assert holder.held is False  # the instance frees whatever the disk did
    monkeypatch.undo()
    path.unlink()  # tmp_path teardown would otherwise trip over our own lock


def test_a_released_marker_is_not_a_holder(tmp_path: Path) -> None:
    """The marker only ever means "free". A payload identical to it but WITHOUT
    the marker still reads as held — the flag is doing the work, not the read."""
    from alkera_core.project import locking

    live = {
        "pid": os.getpid(),
        "host": locking._hostname(),
        "acquired_at": "2026-01-01T00:00:00+00:00",
        "nonce": "n",
        "start_id": process_start_id(os.getpid()),
    }
    path = tmp_path / ".lock"
    path.write_text(json.dumps(live))
    assert live_holder(path) is not None

    path.write_text(json.dumps({**live, "released": True}))
    assert live_holder(path) is None
    assert locking._holder_is_stale({**live, "released": True}) is True


# ---------------------------------------------------------------------------
# A lock file that cannot be READ
#
# The same open handles that block the delete also make the file unopenable
# while a delete on it is pending. That refusal reaches `acquire` through every
# read it does, and a waiter watching for `LockHeldError` has no idea what to do
# with a `PermissionError` — it dies at the poll before the window closes.
# ---------------------------------------------------------------------------


def _refuse_reads(monkeypatch, path: Path, times: int, calls: dict[str, int]) -> None:
    """Refuse to READ `path` `times` times with the error Windows raises while a
    delete on the file is pending, then read it for real. Other paths — the
    reclaim guard, the rotated forensic — are read normally throughout."""
    from alkera_core.project import locking

    real_read = locking._read_holder

    def _flaky(target: Path):
        if target != path:
            return real_read(target)
        calls["n"] += 1
        if calls["n"] <= times:
            raise PermissionError(13, "Access is denied")
        return real_read(target)

    monkeypatch.setattr(locking, "_read_holder", _flaky)


def _released_payload_on_disk(path: Path) -> None:
    """The lock file a holder left behind when the OS refused its delete."""
    from alkera_core.project import locking

    path.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "host": locking._hostname(),
                "acquired_at": "2026-01-01T00:00:00+00:00",
                "nonce": "gone",
                "start_id": process_start_id(os.getpid()),
                "released": True,
            }
        )
    )


def test_a_waiter_polls_through_an_unreadable_lock_file(tmp_path: Path, monkeypatch) -> None:
    """A read the OS refuses is a window, not a verdict. The waiter keeps
    polling and takes the lock on the read that finally lands — here one that
    finds the released marker a refused delete left behind."""
    from alkera_core.project.locking import retrying_lock

    path = tmp_path / ".lock"
    _released_payload_on_disk(path)
    calls = {"n": 0}
    _refuse_reads(monkeypatch, path, times=4, calls=calls)
    pauses: list[float] = []

    waiter = FileLock(path)
    with retrying_lock(waiter, timeout_seconds=60.0, sleep=pauses.append):
        assert waiter.held is True
    assert calls["n"] > 4  # the refusals happened, and were waited out
    assert len(pauses) == 4  # one poll per refusal, none wasted after


def test_an_unreadable_lock_file_times_out_as_held_not_as_a_filesystem_error(
    tmp_path: Path, monkeypatch
) -> None:
    """When the refusal never clears, the waiter must still fail in the shape its
    caller handles: the deadline reports the lock as held. The underlying refusal
    is kept as the cause, so the reason is not lost — it is just not the thing a
    retry loop is made to catch."""
    from alkera_core.project.locking import retrying_lock

    path = tmp_path / ".lock"
    _released_payload_on_disk(path)
    _refuse_reads(monkeypatch, path, times=10**6, calls={"n": 0})

    waiter = FileLock(path)
    with pytest.raises(LockHeldError) as excinfo:
        with retrying_lock(waiter, timeout_seconds=0.05, sleep=lambda _s: None):
            pass  # pragma: no cover — never reached
    assert isinstance(excinfo.value.__cause__, PermissionError)


def test_reading_for_display_still_reports_the_refusal(tmp_path: Path, monkeypatch) -> None:
    """The conversion belongs to `acquire` alone. `live_holder` answers "who
    holds this?" for a display, and a file it could not read is not an answer of
    "nobody" — that would show a contended lock as free."""
    path = tmp_path / ".lock"
    _released_payload_on_disk(path)
    _refuse_reads(monkeypatch, path, times=10**6, calls={"n": 0})

    with pytest.raises(PermissionError):
        live_holder(path)


# ---------------------------------------------------------------------------
# Same-process concurrency — threads sharing one instance, and per-thread
# instances racing on one path. The lock FILE arbitrates between processes;
# the instance mutex arbitrates between threads. Both must hold.
# ---------------------------------------------------------------------------


def _hammer_counter(tmp_path: Path, make_lock, threads: int = 8, iters: int = 40) -> None:
    """N threads x M increments of a plain text counter, each under
    ``retrying_lock``. Any lost update, stolen lock, or orphaned lock file
    fails the assertions — this is the distilled form of the cost-ledger
    concurrent-records race."""
    import threading

    from alkera_core.project.locking import retrying_lock

    counter = tmp_path / "counter"
    counter.write_text("0")
    errors: list[BaseException] = []

    def worker() -> None:
        lock = make_lock()
        try:
            for _ in range(iters):
                with retrying_lock(lock, timeout_seconds=30.0):
                    counter.write_text(str(int(counter.read_text()) + 1))
        except BaseException as exc:
            errors.append(exc)

    ts = [threading.Thread(target=worker) for _ in range(threads)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert errors == []
    assert int(counter.read_text()) == threads * iters
    # The lock was always released cleanly: no orphaned holder file, and no
    # live lock was ever mistaken for stale + rotated aside.
    assert not (tmp_path / ".lock").exists()
    assert [p.name for p in tmp_path.iterdir() if ".stale." in p.name] == []


def test_threads_sharing_one_instance_lose_no_updates(tmp_path: Path) -> None:
    """One FileLock instance shared by many threads (the CostLedger /
    SchedulerStore shape): acquire/release instance state must be
    thread-safe, or a release can stomp the next winner's state — orphaning
    the holder file and wedging every later acquirer."""
    shared = FileLock(tmp_path / ".lock")
    _hammer_counter(tmp_path, lambda: shared)


def test_shared_instance_second_thread_blocks_until_release(tmp_path: Path) -> None:
    """A second thread acquiring a SHARED instance BLOCKS until the holder
    releases (a deterministic, woken hand-off) rather than spin-polling — while
    the SAME thread re-acquiring still fails loud. The old spin-retry could
    starve a contender under Windows lock convoys (a flaky-timeout source)."""
    import threading

    lock = FileLock(tmp_path / ".lock")
    lock.acquire()
    # Same thread, same instance → the re-entrant guard fires immediately.
    with pytest.raises(LockHeldError):
        lock.acquire()

    acquired = threading.Event()

    def contender() -> None:
        lock.acquire()  # must BLOCK until the main thread releases
        acquired.set()
        lock.release()

    t = threading.Thread(target=contender)
    t.start()
    try:
        # Still held by the main thread → the contender is parked, not spinning.
        assert not acquired.wait(0.2)
        lock.release()
        # Released → the contender is woken promptly (notify, not a poll).
        assert acquired.wait(2.0)
    finally:
        t.join()
    # Clean hand-off left no orphaned holder file.
    assert not (tmp_path / ".lock").exists()


def test_threads_with_per_thread_instances_lose_no_updates(tmp_path: Path) -> None:
    """Per-thread FileLock instances on the same path: the FILE is the only
    arbiter, so a vanished-then-recreated holder file must read as 'free /
    held', never as 'stale' — or a contender renames a live lock aside and
    two threads hold at once."""
    path = tmp_path / ".lock"
    _hammer_counter(tmp_path, lambda: FileLock(path))


def test_vanished_holder_file_is_free_not_stale(tmp_path: Path, monkeypatch) -> None:
    """The lock file vanishing between a failed create and the holder read
    means the holder RELEASED — the lock is free to retry, not stale to
    reclaim. Reclaiming here is the theft window: by the time the rename
    runs, a third party may have legitimately acquired."""
    import alkera_core.project.locking as locking

    path = tmp_path / ".lock"
    holder = acquire(path)
    try:
        nonce = json.loads(path.read_text())["nonce"]
        real_read = locking._read_holder
        calls = {"n": 0}

        def vanish_twice(p: Path) -> dict | None:
            # Read 1 is the pre-create "is it held?" probe and read 2 the one
            # after the create loses — both must see the file gone to drive the
            # acquire loop into its free-retry branch. Read 3 tells the truth.
            calls["n"] += 1
            if calls["n"] <= 2:
                raise FileNotFoundError(p)  # simulate: released in the read window
            return real_read(p)

        monkeypatch.setattr(locking, "_read_holder", vanish_twice)
        with pytest.raises(LockHeldError):
            FileLock(path).acquire()
        # The live holder was left completely untouched — same file, same
        # nonce, nothing rotated aside.
        assert json.loads(path.read_text())["nonce"] == nonce
        assert [p.name for p in tmp_path.iterdir() if ".stale." in p.name] == []
    finally:
        holder.release()


def test_reclaim_reverifies_under_guard_and_backs_off_from_a_live_lock(
    tmp_path: Path, monkeypatch
) -> None:
    """Two contenders can both read the SAME stale holder; the slower one
    must not act on that stale read after the faster one already reclaimed
    and a fresh LIVE lock took the path. The staleness decision is re-made
    under the reclaim guard, so the slow contender backs off."""
    import socket as _s

    import alkera_core.project.locking as locking

    path = tmp_path / ".lock"
    stale = {
        "pid": _find_dead_pid(),
        "host": _s.gethostname(),
        "acquired_at": "2024-01-01T00:00:00Z",
        "nonce": "0" * 16,
    }
    path.write_text(json.dumps(stale))
    live = {
        "pid": os.getpid(),
        "host": _s.gethostname(),
        "acquired_at": "2026-01-01T00:00:00Z",
        "nonce": "f" * 16,
    }
    real_write_excl = locking._write_excl
    fired = {"done": False}

    def race_in_a_reclaimer(p: Path, payload: dict) -> None:
        # The first guard creation simulates the in-between interleave: a
        # faster reclaimer already rotated the stale file away and a fresh
        # holder won the path. THEN the slow contender gets its guard.
        if p.name.endswith(".reclaim") and not fired["done"]:
            fired["done"] = True
            path.unlink()
            path.write_text(json.dumps(live))
        real_write_excl(p, payload)

    monkeypatch.setattr(locking, "_write_excl", race_in_a_reclaimer)
    with pytest.raises(LockHeldError):
        FileLock(path).acquire()
    # The live lock survived: not renamed, not deleted, same nonce.
    assert json.loads(path.read_text())["nonce"] == live["nonce"]
    assert [p.name for p in tmp_path.iterdir() if ".stale." in p.name] == []
    path.unlink()


def test_reclaim_backs_off_while_another_reclaim_is_in_flight(tmp_path: Path) -> None:
    """A live reclaim guard means someone else is mid-reclaim — treat the
    lock as held (the retrying caller comes back) instead of racing them."""
    import socket as _s

    path = tmp_path / ".lock"
    stale = {
        "pid": _find_dead_pid(),
        "host": _s.gethostname(),
        "acquired_at": "2024-01-01T00:00:00Z",
        "nonce": "0" * 16,
    }
    path.write_text(json.dumps(stale))
    guard = tmp_path / ".lock.reclaim"
    guard.write_text(
        json.dumps(
            {
                "pid": os.getpid(),  # a LIVE reclaimer
                "host": _s.gethostname(),
                "acquired_at": "2026-01-01T00:00:00Z",
                "nonce": "1" * 16,
            }
        )
    )
    with pytest.raises(LockHeldError):
        FileLock(path).acquire()
    # Neither the stale lock nor the guard was touched.
    assert json.loads(path.read_text())["nonce"] == stale["nonce"]
    assert guard.exists()
    path.unlink()
    guard.unlink()


def test_stale_reclaim_guard_from_dead_process_is_swept(tmp_path: Path) -> None:
    """A reclaim guard left by a crashed reclaimer must not wedge the lock
    forever — a dead-pid guard is swept (by atomic rename, kept for
    forensics) and the reclaim proceeds."""
    import socket as _s

    path = tmp_path / ".lock"
    dead = _find_dead_pid()
    stale = {
        "pid": dead,
        "host": _s.gethostname(),
        "acquired_at": "2024-01-01T00:00:00Z",
        "nonce": "0" * 16,
    }
    path.write_text(json.dumps(stale))
    guard = tmp_path / ".lock.reclaim"
    guard.write_text(json.dumps({**stale, "nonce": "1" * 16}))

    lock = acquire(path)
    try:
        assert lock.held
        assert not guard.exists()  # the dead reclaimer's guard was swept
        # Both the stale lock AND the dead guard were rotated aside, payloads
        # preserved: `.lock.stale.<ts>.<rand>` + `.lock.reclaim.stale.<ts>.<rand>`.
        rotated_lock = [p for p in tmp_path.iterdir() if p.name.startswith(".lock.stale.")]
        swept_guard = [p for p in tmp_path.iterdir() if p.name.startswith(".lock.reclaim.stale.")]
        assert len(rotated_lock) == 1
        assert json.loads(rotated_lock[0].read_text())["nonce"] == stale["nonce"]
        assert len(swept_guard) == 1
        assert json.loads(swept_guard[0].read_text())["nonce"] == "1" * 16
    finally:
        lock.release()


def test_rotate_readback_restores_a_mistakenly_grabbed_live_lock(
    tmp_path: Path, monkeypatch
) -> None:
    """Defense-in-depth on the rotate itself: if (post-crash) guard exclusivity
    were ever violated and the rotate grabbed a lock that is NOT the stale
    payload that was re-verified, the read-back detects it, links the live
    lock back, and reports 'held' — never two holders."""
    import socket as _s

    import alkera_core.project.locking as locking

    path = tmp_path / ".lock"
    live = {
        "pid": os.getpid(),
        "host": _s.gethostname(),
        "acquired_at": "2026-01-01T00:00:00Z",
        "nonce": "f" * 16,
    }
    path.write_text(json.dumps(live))
    stale = {
        "pid": _find_dead_pid(),
        "host": _s.gethostname(),
        "acquired_at": "2024-01-01T00:00:00Z",
        "nonce": "0" * 16,
    }
    real_read = locking._read_holder

    def lie_about_the_lock(p: Path) -> dict | None:
        # The under-guard re-read reports the OLD stale holder while the file
        # actually carries a fresh live one — the exact stale-read scenario
        # the read-back exists for. Reads of any other path (the rotated
        # target, the guard) see the truth.
        if p == path:
            return stale
        return real_read(p)

    monkeypatch.setattr(locking, "_read_holder", lie_about_the_lock)
    assert locking._reclaim_stale(path) is False
    monkeypatch.undo()
    # The live lock is back at the path, byte-identical payload, and nothing
    # was left rotated aside (the guard came and went cleanly).
    assert json.loads(path.read_text())["nonce"] == live["nonce"]
    assert [p.name for p in tmp_path.iterdir() if ".stale." in p.name] == []
    assert not (tmp_path / ".lock.reclaim").exists()
    path.unlink()


# ---------------------------------------------------------------------------
# Cross-process concurrency — the lock FILE is the arbiter between processes
# ---------------------------------------------------------------------------

_PROC_WORKER = """
import sys
from pathlib import Path
from alkera_core.project.locking import FileLock, retrying_lock

lock_path, counter_path, iters = Path(sys.argv[1]), Path(sys.argv[2]), int(sys.argv[3])
lock = FileLock(lock_path)
for _ in range(iters):
    with retrying_lock(lock, timeout_seconds=60.0):
        counter_path.write_text(str(int(counter_path.read_text()) + 1))
"""


def test_processes_hammering_one_lock_lose_no_updates(tmp_path: Path) -> None:
    """K subprocesses x M increments under the same lock path: every update
    lands (full cross-process mutual exclusion), nothing is mistaken for
    stale, and the lock file is released cleanly at the end."""
    import subprocess
    import sys

    counter = tmp_path / "counter"
    counter.write_text("0")
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", _PROC_WORKER, str(tmp_path / ".lock"), str(counter), "25"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for _ in range(4)
    ]
    for p in procs:
        _, err = p.communicate(timeout=120)
        assert p.returncode == 0, err.decode()
    assert int(counter.read_text()) == 4 * 25
    assert not (tmp_path / ".lock").exists()
    assert [p.name for p in tmp_path.iterdir() if ".stale." in p.name] == []


def test_sigkilled_holder_is_reclaimed_by_the_next_acquirer(tmp_path: Path) -> None:
    """A REAL crashed holder (SIGKILL — no atexit, no signal handler, no GC
    finalizer runs) leaves its lock file behind; the next acquirer must
    reclaim it via PID liveness and rotate the corpse aside for forensics."""
    import subprocess
    import sys
    import time

    from alkera_core.process import kill_process, process_alive

    path = tmp_path / ".lock"
    holder_src = """
import os, sys, time
from pathlib import Path
from alkera_core.project.locking import acquire

lock = acquire(Path(sys.argv[1]))  # keep the reference — GC would release it
print(f"HELD {os.getpid()}", flush=True)
time.sleep(60)
"""
    proc = subprocess.Popen(
        [sys.executable, "-c", holder_src, str(path)],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None
        held, child_pid_raw = proc.stdout.readline().split()
        assert held == "HELD"
        # The HOLDER's own pid — on Windows the venv python.exe is a launcher
        # whose real interpreter is a grandchild, so proc.pid can differ; the
        # lock payload carries the pid that must die for the lock to go stale.
        child_pid = int(child_pid_raw)
        kill_process(child_pid)  # hard kill — no atexit/handler/GC cleanup runs
        # Reap: on POSIX the killed child is a zombie (still "alive" to a PID
        # probe) until waited on; on Windows the launcher exits with its child.
        proc.wait(timeout=30)
        deadline = time.monotonic() + 10
        while process_alive(child_pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not process_alive(child_pid)
        assert path.exists()  # the hard kill bypassed every cleanup path

        lock = acquire(path)  # PID-liveness reclaim
        try:
            assert lock.payload is not None
            assert lock.payload["pid"] == os.getpid()
            rotated = [p for p in tmp_path.iterdir() if p.name.startswith(".lock.stale.")]
            assert len(rotated) == 1
            assert json.loads(rotated[0].read_text())["pid"] == child_pid
        finally:
            lock.release()
    finally:
        if proc.poll() is None:  # pragma: no cover — only on assertion failure
            proc.kill()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _find_dead_pid() -> int:
    """Find a PID guaranteed not to be in use.

    Strategy: try a few large PIDs that won't exist on a typical
    system. If they all exist (paranoid), fall back to fork+exit to
    capture a PID we know is dead.
    """
    import errno as _errno

    for candidate in (999_999, 998_888, 997_777):
        try:
            os.kill(candidate, 0)
        except OSError as exc:
            if exc.errno == _errno.ESRCH:
                return candidate
    # Fallback: spawn a quick subprocess, capture pid, wait for exit.
    import subprocess

    p = subprocess.Popen(["true"])
    p.wait()
    return p.pid
