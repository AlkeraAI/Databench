"""PID-aware advisory file locks for `.alkera/` directory entries.

A `FileLock` is a small JSON file at a well-known path (e.g.
`<chat>/.lock`) that records the holder's PID + host + acquisition
timestamp, plus the kernel's creation stamp of the holder's process.
Acquisition is fail-fast: if the lock exists AND its holder is alive AND
running on the same host AND is the same incarnation of that pid AND has
not marked itself released, the new acquirer raises `LockHeldError`. A
holder file that is MISSING when read means the lock was just released —
that's "free, retry the create", never "stale". Only a present-but-dead
(or unparseable, or self-marked-released) holder is stale and reclaimed —
and a dead holder whose pid the kernel has since handed to an unrelated
process counts as dead: the stranger's creation stamp is not the one the
holder recorded. The released marker is the release's own fallback: on a
platform that refuses to delete a file another process has open, ending
the claim in the payload beats leaving a live pid's name on a lock
nobody holds. Reclaim is serialized through a sibling
`<path>.reclaim` guard with the staleness decision re-verified under it —
so two contenders acting on the same stale read can never rotate a LIVE
lock aside.

Two arbitration levels, both required:
- between PROCESSES, the lock FILE (atomic create-via-link) is the arbiter;
- between THREADS of one process sharing a `FileLock` instance (the cost
  ledger, the scheduler store), an internal condition makes acquire/release
  state transitions atomic — without it a release can interleave with the
  next winner's acquire and stomp `_payload`, orphaning the holder file — and
  lets a contender BLOCK until the in-process holder releases (woken via
  notify) instead of spin-polling, which is deterministic and starvation-free
  (a spin-retry starves threads under Windows lock convoys).

Why advisory (file-based) instead of `fcntl.flock`:
- `flock`/`lockf` are per-file-descriptor on Linux, per-PID on macOS.
  Both have surprising semantics around forks, NFS, and dup'd fds.
- We need a stable, human-inspectable record of who's holding the lock
  (PID + host + time). `flock` doesn't carry payload.
- File locks survive process death — that's a feature for crash
  recovery here. We mitigate the obvious downside (deadlock if a
  process dies dirty) with PID-liveness reclaim.

Cleanup paths (best-effort, all installed at acquisition):
- `atexit.register(_release_all)` — fires on `sys.exit`, normal return.
- `signal.signal(SIGTERM, …)` / `SIGINT` — wraps any prior handler so
  we chain through after releasing. (Note: handlers don't run on
  SIGKILL or power-loss — that's the PID-liveness reclaim's job.)
- `weakref.finalize(lock, _release_one)` — GC fallback if the caller
  forgot to release.

The lock is NOT recursive. Acquiring the same path twice from the
same process raises `LockHeldError`.
"""

from __future__ import annotations

import atexit
import contextlib
import json
import logging
import os
import platform
import random as _random
import secrets
import signal
import socket
import sys
import threading
import time
import weakref
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType
from typing import Any

from alkera_core.atomic_io import Jitter, replace_with_retry, unlink_with_retry
from alkera_core.process import process_alive, process_start_id
from alkera_core.project.local_state import is_local_state

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


class LockHeldError(RuntimeError):
    """Raised when the lock is held by a different live process."""

    def __init__(self, path: Path, holder: dict[str, Any]) -> None:
        super().__init__(
            f"{path}: held by pid={holder.get('pid')} "
            f"on host={holder.get('host')!r} since {holder.get('acquired_at')}"
        )
        self.path = path
        self.holder = holder


class FileLock:
    """Held lock. Idempotent release. Use via ``acquire()`` (recommended)
    or via ``__enter__`` / ``__exit__`` on an instance.

    Direct instantiation is fine but the class doesn't acquire on
    construction — call ``.acquire()`` or use the module-level helper.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._held = False
        self._finalizer: weakref.finalize | None = None  # type: ignore[type-arg]
        self._payload: dict[str, Any] | None = None
        # Serializes acquire/release state transitions between threads sharing
        # this instance, AND lets a contending thread BLOCK until the in-process
        # holder releases (woken via notify) rather than spin-polling through
        # `retrying_lock` — an in-process holder always releases, so the wait is
        # bounded and fair. (Spin-polling starves threads under Windows lock
        # convoys: the source of a flaky same-instance stress-test timeout.)
        # Backed by an RLock: the SIGINT/SIGTERM cleanup path may call release()
        # on the main thread while it is mid-acquire — a plain Lock would
        # deadlock the dying process.
        self._cond = threading.Condition(threading.RLock())
        # The thread currently holding this instance (drives the re-entrant
        # acquire guard); None when free.
        self._owner: threading.Thread | None = None

    # --- properties ----------------------------------------------------

    @property
    def path(self) -> Path:
        return self._path

    @property
    def held(self) -> bool:
        return self._held

    @property
    def payload(self) -> dict[str, Any] | None:
        """The JSON dict we wrote into the lock file (pid, host, time).

        ``None`` if the lock isn't held by us.
        """
        return self._payload

    # --- lifecycle -----------------------------------------------------

    def acquire(self) -> None:
        """Acquire the lock. Fail-fast (no waiting).

        Raises ``LockHeldError`` if a live different-process holder
        is present. On a stale holder, reclaims atomically.

        A refused acquisition writes nothing: the live holder is detected by
        reading the existing lock file, so polling on a held lock costs one
        read per poll rather than a create/fsync/unlink cycle.

        Every refusal arrives as ``LockHeldError``, including one the operating
        system rather than a holder is responsible for — a lock file it will
        not let us read or rename right now. "Can I have it?" has only two
        honest answers here, and a caller that polls needs the refusal in the
        one shape it knows how to wait on.
        """
        with self._cond:
            self._acquire_locked()

    def _acquire_locked(self) -> None:
        me = threading.current_thread()
        if self._held:
            if self._owner is me:
                # A genuine re-entrant acquire on the SAME thread — fail loud, a
                # silent double-release would follow.
                raise LockHeldError(self._path, {"pid": os.getpid(), "host": _hostname()})
            # Another thread of THIS process holds the instance. WAIT for it to
            # release rather than returning a "held" the caller must spin-poll
            # on: the holder is in-process and WILL release, so a condition wait
            # is deterministic and fair. Woken on release; loop guards spurious
            # wakeups and the race where a sibling re-took it first.
            while self._held:
                self._cond.wait()

        holder: dict[str, Any] | None = None
        # Three bounded attempts cover the legitimate retry chain — observe a
        # just-released (missing) file, then a reclaimed stale file, then the
        # final create — without looping forever under pathological churn.
        for _ in range(3):
            # Answer "is someone else holding this?" by READING, before paying
            # for any write. The lock file is already on disk and carries the
            # holder's pid, so a live holder is one read plus one liveness
            # probe away. Going straight to the exclusive create instead would
            # make every failed acquisition a staging write + fsync + unlink in
            # the lock's directory — a caller polling a held lock generates
            # hundreds of those a second, which is what turns a few contenders
            # on a short hold into a multi-second pile-up (worst on NTFS).
            # Only an absent, corrupt or dead holder gets past here: the
            # exclusive create below remains the sole arbiter between two
            # contenders that both read the path as free, and a stale holder
            # still reaches the reclaim protocol via the create's
            # FileExistsError.
            try:
                live = live_holder(self._path)
            except PermissionError as refusal:
                # The file cannot be READ at all right now: a delete on it is
                # pending, or a peer has it open in a mode that excludes us.
                # Both windows close in milliseconds, and both answer the only
                # question being asked here the same way — not yours yet. The
                # filesystem error is the wrong answer to give a waiter: a
                # retry loop watches for `LockHeldError` and nothing else, so
                # raising it crashes the caller over a window whose next read
                # would have found the file gone or marked released. The
                # refusal is kept as the cause, not thrown away.
                raise LockHeldError(self._path, {}) from refusal
            if live is not None:
                raise LockHeldError(self._path, live)

            payload = _holder_payload()
            try:
                _write_excl(self._path, payload)
            except FileExistsError:
                try:
                    holder = _read_holder(self._path)
                except FileNotFoundError:
                    # Released between our failed create and the read — the
                    # lock is FREE, not stale. Retry the create; reclaiming
                    # here would rotate aside whatever sits at the path by
                    # the time the rename runs, including a fresh LIVE lock.
                    continue
                except PermissionError as refusal:
                    raise LockHeldError(self._path, {}) from refusal
                if _holder_is_stale(holder) and _reclaim_stale(self._path):
                    continue
                raise LockHeldError(self._path, holder or {}) from None

            # Won the file. Wire up cleanup paths.
            self._held = True
            self._owner = me
            self._payload = payload
            _OPEN_LOCKS_LOCK_register(self)
            self._finalizer = weakref.finalize(self, _release_payload, self._path, payload)
            return

        # Retry budget exhausted under heavy churn — truthful answer: held.
        raise LockHeldError(self._path, holder or {})

    def release(self) -> None:
        """Release the lock. Idempotent — calling twice is a no-op.

        The instance is free once this returns, whatever happened on disk. It
        raises only when the lock file could neither be deleted nor marked
        released — a file that would otherwise go on naming this live process as
        its holder and block every waiter, which the caller has to hear about.
        """
        with self._cond:
            if not self._held:
                return
            # `release_payload` only deletes the file if we still own it
            # (nonce check). That prevents us from clobbering a different
            # process's lock if e.g. the GC fired late.
            try:
                _release_payload(self._path, self._payload or {})
            finally:
                self._held = False
                self._owner = None
                self._payload = None
                _OPEN_LOCKS_LOCK_unregister(self)
                if self._finalizer is not None:
                    self._finalizer.detach()
                    self._finalizer = None
                # Wake any in-process thread blocked in _acquire_locked waiting
                # for this instance to free up.
                self._cond.notify_all()

    def __enter__(self) -> FileLock:
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


# ---------------------------------------------------------------------------
# Module-level open-lock registry — drives best-effort cleanup
# ---------------------------------------------------------------------------


_OPEN_LOCKS: weakref.WeakSet[FileLock] = weakref.WeakSet()
"""Live FileLock instances. WeakSet so dropping the last reference
doesn't keep the lock pinned forever."""

_OPEN_LOCKS_GUARD = threading.Lock()
"""Threadsafe access to ``_OPEN_LOCKS``."""

_CLEANUP_INSTALLED = False


def _OPEN_LOCKS_LOCK_register(lock: FileLock) -> None:  # noqa: N802
    """Register `lock` for atexit/signal cleanup. Install handlers
    lazily on first lock acquired in this process."""
    global _CLEANUP_INSTALLED
    with _OPEN_LOCKS_GUARD:
        _OPEN_LOCKS.add(lock)
        if not _CLEANUP_INSTALLED:
            atexit.register(_release_all_on_exit)
            _install_signal_handlers()
            _CLEANUP_INSTALLED = True


def _OPEN_LOCKS_LOCK_unregister(lock: FileLock) -> None:  # noqa: N802
    with _OPEN_LOCKS_GUARD:
        _OPEN_LOCKS.discard(lock)


def _release_all_on_exit() -> None:
    """Release every live FileLock. Called by atexit + signal handlers."""
    with _OPEN_LOCKS_GUARD:
        # Copy so iteration is safe under concurrent mutation.
        locks = list(_OPEN_LOCKS)
    for lock in locks:
        try:
            lock.release()
        except Exception:  # noqa: S110 — see docstring on _release_all_on_exit
            # Cleanup must never raise — we may be inside a signal
            # handler running during a shutdown that's already
            # cascading. Swallow and move on to the next lock.
            pass


# ---------------------------------------------------------------------------
# Signal handlers
# ---------------------------------------------------------------------------


_PRIOR_HANDLERS: dict[int, Any] = {}


def _install_signal_handlers() -> None:
    """Wire SIGTERM + SIGINT through the cleanup path, chaining any
    pre-existing handler the host process installed.

    Only safe to call from the main thread (Python's `signal.signal`
    requirement). If we're not on the main thread, skip installation —
    atexit is still wired up.
    """
    if threading.current_thread() is not threading.main_thread():
        return
    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            prior = signal.getsignal(signum)
        except (ValueError, OSError):
            continue
        _PRIOR_HANDLERS[signum] = prior

        def _handler(sig: int, frame: FrameType | None, _prior: Any = prior) -> None:
            _release_all_on_exit()
            # Chain through to the prior handler so the host's existing
            # behavior is preserved.
            if callable(_prior):
                _prior(sig, frame)
            elif _prior == signal.SIG_DFL:
                # Re-raise default behavior by re-installing + re-sending.
                signal.signal(sig, signal.SIG_DFL)
                os.kill(os.getpid(), sig)
            # signal.SIG_IGN → swallow.

        try:
            signal.signal(signum, _handler)
        except (ValueError, OSError):
            # Non-main thread, or signal not available on this OS.
            continue


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _hostname() -> str:
    """Stable hostname identifier — same one we record in the lock file
    so a different machine can spot that the holder is unreachable."""
    try:
        return socket.gethostname()
    except OSError:
        return f"unknown-{platform.node()}"


def _holder_payload() -> dict[str, Any]:
    """What this process writes to claim a path.

    ``pid`` + ``host`` say who; ``start_id`` — the kernel's creation stamp of
    this process — says WHICH process of that number, so once we are gone
    and the kernel hands our pid to a stranger, the stranger's stamp gives it
    away and the lock is reclaimable instead of wedged. The ``nonce`` is
    unique per acquisition, so release deletes only the file it created even
    if a different process reclaimed the same name in between. A platform
    that reports no stamp writes none; the holder is then judged by liveness
    alone, as before.
    """
    payload: dict[str, Any] = {
        "pid": os.getpid(),
        "host": _hostname(),
        "acquired_at": datetime.now(UTC).isoformat(),
        "nonce": secrets.token_hex(8),
    }
    start_id = process_start_id(os.getpid())
    if start_id is not None:
        payload["start_id"] = start_id
    return payload


def _link_excl(src: Path, dst: Path) -> None:
    """Atomically publish a fully-written `src` to `dst`, failing with
    ``FileExistsError`` if `dst` already exists.

    POSIX uses ``os.link`` (hardlink); Windows uses ``os.rename``. Both raise
    ``FileExistsError`` when the destination is present — the exclusive-create
    semantics the lock relies on — and both leave the destination carrying the
    full payload (never a zero-byte window). We can't use ``os.link`` on Windows
    because hardlinks need NTFS and break on FAT/exFAT/network/cross-volume
    ``~/.alkera`` dirs; ``os.rename`` works everywhere there.
    """
    if sys.platform == "win32":
        os.rename(src, dst)
    else:
        os.link(src, dst)


def _write_excl(path: Path, payload: dict[str, Any]) -> None:
    """Atomically create `path` with `payload` as its JSON content.

    Implementation: write payload to a sibling staging file, fsync,
    then publish it to the final path via `_link_excl`, which fails
    with `FileExistsError` if the target already exists. The temp
    file is unlinked either way after the attempt.

    Why not just ``O_EXCL | O_CREAT | O_WRONLY`` directly on the lock
    path: that's atomic for FILE CREATION, but a concurrent reader
    that opens the file between the create and the payload-write
    sees a zero-byte file, mistakes it for a corrupt/stale lock, and
    reclaims — letting two processes both think they won. The
    publish-from-staging approach makes the file appear at its final
    path ONLY in its fully-written state.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.staging.{secrets.token_hex(6)}"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(json.dumps(payload).encode("utf-8"))
                f.flush()
                os.fsync(f.fileno())
        except BaseException:
            try:
                os.close(fd)
            except OSError:
                pass
            tmp.unlink(missing_ok=True)
            raise
        # Atomic publish — fails with FileExistsError if `path` exists.
        # The lock file therefore only ever appears at `path` already
        # carrying the full payload. On Windows a successful rename moves
        # `tmp`, so the finally-unlink below becomes a no-op (missing_ok).
        _link_excl(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


#: Windows file-sharing semantics, read side: while a delete on the lock file is
#: pending, opening it fails with a sharing violation (PermissionError). That
#: window closes as soon as the last handle drops, so a short bounded retry
#: rides it out. The DELETE side is the harder half and rides the shared,
#: jittered schedule in `atomic_io` instead. POSIX never takes these branches.
_SHARING_RETRIES = 20
_SHARING_BACKOFF_S = 0.01


def _read_holder(path: Path) -> dict[str, Any] | None:
    """Read the lock file's JSON payload.

    Returns the dict, or `None` if the file exists but is unparseable
    (treated as "lock file corruption — stale by default"). A MISSING
    file raises ``FileNotFoundError`` — that's a different fact: the
    lock was released, i.e. it's free, not stale.

    A Windows sharing violation (another contender mid-read/mid-unlink)
    is retried briefly; if it persists, the error propagates — failing
    loud beats misreading "contended" as "corrupt and reclaimable".
    """
    for attempt in range(_SHARING_RETRIES):
        try:
            raw = path.read_bytes()
            break
        except FileNotFoundError:
            raise
        except PermissionError:
            if attempt == _SHARING_RETRIES - 1:
                raise
            time.sleep(_SHARING_BACKOFF_S)
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _holder_is_stale(holder: dict[str, Any] | None) -> bool:
    """Decide whether to reclaim the lock.

    Stale iff:
    - Holder dict is None / missing pid / unparseable.
    - Holder marked itself released on the way out — the file outlived the
      claim because the OS refused the delete, and the pid it names is alive
      and holding nothing. Without this the file reads as a live holder
      forever and every waiter blocks to its timeout.
    - Holder is on the same host AND its pid is no longer alive.
    - Holder is on the same host, its pid is alive, but the process now
      owning that pid was created at a different moment than the one the
      holder recorded — the holder died and the kernel reused its number.

    Different-host holders are treated as LIVE (we can't probe them
    safely), and so is a live pid whose creation stamp cannot be read or
    was never recorded: an undecided question never reclaims. Callers see
    `LockHeldError` and decide retry/abort.

    This is the one predicate behind both the read-only pre-check in
    ``acquire`` / ``live_holder`` and the re-verification under the reclaim
    guard, so the two can never disagree about a holder.
    """
    if not holder:
        return True
    if holder.get("released") is True:
        return True
    pid = holder.get("pid")
    if not isinstance(pid, int):
        return True
    if holder.get("host") != _hostname():
        # Conservative — assume the remote machine knows what it's doing.
        return False
    if not _process_alive(pid):
        return True
    recorded = holder.get("start_id")
    if not isinstance(recorded, int) or isinstance(recorded, bool):
        return False
    current = process_start_id(pid)
    return current is not None and current != recorded


def prune_stale_locks(directory: Path) -> int:
    """Delete the forensic rotations of dead locks under ``directory``.

    ``_reclaim_stale`` never deletes the lock it takes over — it renames it to
    ``<name>.stale.<unix_ts>.<rand>`` so the dead holder's pid, host and time
    are still readable after the fact. That is the right default for a
    workstation, where one such file a year is a breadcrumb. It is the wrong
    default for a directory that is copied between machines: the rotation is
    pushed, pulled, and rotated beside the next one, so a chat that has moved
    boxes N times carries N of them and nothing ever removes one.

    A rotation is by construction the payload of a holder already judged dead
    (the reclaim re-verifies staleness under its own guard before renaming), so
    there is no liveness question left to ask here. Returns how many were
    removed; a directory that does not exist prunes nothing.
    """
    removed = 0
    try:
        names = sorted(directory.iterdir())
    except (FileNotFoundError, NotADirectoryError, PermissionError):
        return 0
    for candidate in names:
        if not is_local_state(candidate.name) or ".stale." not in candidate.name:
            continue
        try:
            candidate.unlink()
        except (IsADirectoryError, PermissionError, OSError):
            # Someone else is mid-read of it (Windows), or it is not ours to
            # remove. It is inert either way; the next prune tries again.
            continue
        removed += 1
    return removed


def live_holder(path: Path) -> dict[str, Any] | None:
    """The lock's holder payload iff a LIVE process holds it — ``None``
    when the file is missing, corrupt, or stale.

    Read-only: never reclaims a stale lock (that stays `acquire`'s
    job). For "is someone else working on this?" displays — e.g. the
    chat list's working-elsewhere spinner — where taking the lock just
    to look would itself be a conflict.

    A persistent Windows sharing violation propagates ``PermissionError``
    (inherited from ``_read_holder``) rather than being swallowed as
    free — failing loud beats misreporting a contended lock as available.
    A display that cannot read the file should say so; only `acquire`, whose
    question is "may I take it now", turns that refusal into `LockHeldError`.
    """
    try:
        holder = _read_holder(path)
    except FileNotFoundError:
        return None
    if holder is None or _holder_is_stale(holder):
        return None
    return holder


def _process_alive(pid: int) -> bool:
    """Whether `pid` is a live process. Cross-platform (POSIX signal-0 /
    Windows OpenProcess) via `alkera_core.process` — a same-host dead holder
    is what makes a lock reclaimable."""
    return process_alive(pid)


def _reclaim_stale(path: Path) -> bool:
    """Rotate a stale lock aside so the caller can retry the create.
    Returns True when the path is (now) free, False when the lock must be
    treated as held (a live holder appeared, or another reclaim is in flight).

    Reclaim is serialized through a sibling ``<path>.reclaim`` guard, and the
    staleness decision is RE-VERIFIED under it. Without the guard, two
    contenders that both read the same stale holder race: the faster one
    rotates it and a fresh holder wins the path, then the slower one — still
    acting on its stale read — rotates the fresh LIVE lock aside and both end
    up holding. Under the guard no live lock can ever be rotated: while the
    (re-verified stale) file occupies the path no create can succeed there,
    and no other reclaimer can remove it.

    A guard left behind by a reclaimer that crashed inside this window is
    swept by ATOMIC RENAME, never unlink: the rename's success picks a unique
    sweeper, and the loser falls back to contending the guard create — an
    unconditional unlink could remove a guard the winner had already
    re-created, re-opening the double-reclaim race one level down.

    We rename rather than delete so the holder's payload is still on disk
    for forensics — `<.lock>.stale.<unix_ts>.<rand>`. The next GC sweep can
    clean these up.

    Any filesystem REFUSAL inside this protocol — a file the OS will not let us
    read, a rename it will not take right now — answers False. Both are windows
    that close in milliseconds, and "not free yet" is the truthful, conservative
    reading of either; a caller that polls will come back and find the question
    answerable. Letting the refusal out instead would crash a waiter mid-reclaim
    with an error no retry loop is watching for.
    """
    guard_path = path.with_name(path.name + ".reclaim")
    guard_payload = _holder_payload()
    for _ in range(2):
        try:
            _write_excl(guard_path, guard_payload)
            break
        except FileExistsError:
            try:
                guard_holder = _read_holder(guard_path)
            except FileNotFoundError:
                continue  # that reclaim just finished — try to take the guard
            except PermissionError:
                return False
            if _holder_is_stale(guard_holder):
                # A dead reclaimer's guard is inert (its pending rename died
                # with it) — sweep it aside atomically and contend the create.
                try:
                    replace_with_retry(guard_path, _forensic_name(guard_path))
                except FileNotFoundError:
                    pass  # another sweeper won — contend the create anyway
                except PermissionError:
                    return False
                continue
            return False  # live reclaim in flight — back off, spin later
    else:
        return False

    try:
        try:
            current = _read_holder(path)
        except FileNotFoundError:
            return True  # freed while we took the guard — nothing to rotate
        except PermissionError:
            return False
        if not _holder_is_stale(current):
            return False  # a live holder won the path meanwhile — back off
        target = _forensic_name(path)
        try:
            replace_with_retry(path, target)
        except FileNotFoundError:
            return True  # already gone — equally free
        except PermissionError:
            return False
        # Defense-in-depth: with an intact guard the rotated inode can only be
        # the stale payload re-verified above, so this read-back is a no-op. It
        # only fires if guard exclusivity was violated (crashed reclaimers
        # racing a sweep) — then we may have grabbed a LIVE lock: put it back
        # and report "held" instead of letting two holders proceed.
        try:
            rotated = _read_holder(target)
        except FileNotFoundError:  # pragma: no cover — nothing rotates forensics
            return True
        except PermissionError:
            return False
        if not _holder_is_stale(rotated) and (rotated or {}).get("nonce") != (current or {}).get(
            "nonce"
        ):
            try:
                _link_excl(target, path)
                target.unlink(missing_ok=True)
            except FileExistsError:
                # A fresh lock already took the path; leave the displaced
                # payload at `target` for forensics.
                pass
            return False
        # We intentionally don't fsync the directory here — losing a stale
        # lock file on power failure is harmless.
        return True
    finally:
        _release_payload(guard_path, guard_payload)


def _forensic_name(path: Path) -> Path:
    """A unique sibling name to rotate a stale lock/guard aside for forensics."""
    return path.with_name(f"{path.name}.stale.{int(time.time())}.{secrets.token_hex(4)}")


def _release_payload(path: Path, payload: dict[str, Any]) -> None:
    """Delete the lock file IFF it still carries our nonce.

    The nonce check prevents the GC finalizer from clobbering a
    different process's lock if our FileLock object survived past
    `release()` for some reason (or another reclaimer rotated ours
    aside, then created a fresh one).

    The delete is the whole release: until the file is gone, every waiter
    reading it sees a holder, and this process — which goes on running — is
    still alive at the pid the file names, so staleness never rescues them.
    That is why a refusal is ridden out on the shared sharing-violation
    backoff, why a refusal that outlasts it falls back to marking the payload
    released (a marker readers honour as free), and why the final, doubly
    refused case raises instead of returning as if the lock were free.
    """
    try:
        holder = _read_holder(path)
    except FileNotFoundError:
        return
    if not holder:
        return
    if holder.get("nonce") != payload.get("nonce"):
        # We no longer own this file — leave it alone.
        return
    try:
        unlink_with_retry(path)
    except PermissionError:
        # Windows: a reader holding the file open blocks the delete, and the
        # backoff above has already ridden out seconds of that. Writing over the
        # payload in place is NOT blocked by a reader, so the claim can still be
        # ended even when the file cannot be removed.
        if not _mark_released(path, payload):
            raise
        logger.warning(
            "lock file could not be deleted on release; marked it released instead "
            "(path=%s pid=%s)",
            path,
            payload.get("pid"),
        )


def _mark_released(path: Path, payload: dict[str, Any]) -> bool:
    """Overwrite the lock file in place with a payload that reads as released.

    The last resort when the delete is refused: an in-place write survives a
    concurrent reader where the delete does not, so the next acquirer is told
    the truth instead of being wedged behind a pid that is alive and no longer
    holding anything. A reader that catches the write half-done gets invalid
    JSON, which the staleness rules already treat as reclaimable — every
    outcome of a torn read is "free", never "held". Returns whether the marker
    (or an intervening delete) ended the claim.
    """
    marker = dict(payload)
    marker["released"] = True
    try:
        with path.open("r+b") as handle:
            handle.write(json.dumps(marker).encode("utf-8"))
            handle.truncate()
            handle.flush()
            os.fsync(handle.fileno())
    except FileNotFoundError:
        return True  # the file went away after all — equally released
    except OSError:
        return False
    return True


# ---------------------------------------------------------------------------
# Convenience constructor
# ---------------------------------------------------------------------------


def acquire(path: Path) -> FileLock:
    """Acquire ``path`` as a `FileLock` and return the held instance.

    Caller is responsible for ``.release()`` — or use the lock as a
    context manager: ``with FileLock(path) as lock: ...``.
    """
    lock = FileLock(path)
    lock.acquire()
    return lock


#: Where a waiter's poll interval stops growing. A hundred milliseconds keeps a
#: crowd's combined read rate on one lock file in the tens per second instead of
#: the thousands a five-millisecond poll produces once a few dozen waiters share
#: a builder — and a read rate that high is what keeps the holder's own delete
#: refused on Windows, so an unbacked-off poll can outlast the hold it waits on.
_MAX_POLL_INTERVAL_S = 0.1


@contextlib.contextmanager
def retrying_lock(
    lock: FileLock,
    *,
    timeout_seconds: float = 5.0,
    poll_interval_seconds: float = 0.005,
    max_poll_interval_seconds: float = _MAX_POLL_INTERVAL_S,
    sleep: Callable[[float], None] = time.sleep,
    jitter: Jitter = _random.random,
) -> Iterator[None]:
    """Acquire ``lock``, RETRYING while another live process holds it (up to
    ``timeout_seconds``, then re-raise ``LockHeldError``); release on exit.

    Use this — rather than the plain ``with FileLock(...)`` fail-fast — for a
    lock held only for the microseconds of a single read-modify-write and
    contended by many short critical sections (the scheduler tick, the cost
    ledger). There a held lock is contention to spin out, not the ownership
    signal the chat lock wants. A *stale* holder (dead PID) is still reclaimed by
    ``FileLock.acquire`` itself; this only polls on a *live* contender.

    The poll starts at ``poll_interval_seconds`` and doubles toward
    ``max_poll_interval_seconds`` for as long as the SAME holder keeps the lock:
    a hold that lasts minutes — a migration, a build — deserves a cheap wait, and
    a crowd polling one file at the opening rate is a load the holder has to
    fight to release. The interval drops back to the opening one the moment the
    lock changes hands, because that is the queue moving and the next gap is
    worth catching. Each pause is half fixed, half a fresh draw, so waiters that
    collide once do not collide again on every retry. ``sleep`` and ``jitter``
    are injectable so a test can assert the schedule instead of living it.
    """
    deadline = time.monotonic() + timeout_seconds
    ceiling = max(max_poll_interval_seconds, poll_interval_seconds)
    interval = poll_interval_seconds
    seen: tuple[Any, ...] | None = None
    while True:
        try:
            lock.acquire()
            break
        except LockHeldError as held:
            now = time.monotonic()
            if now >= deadline:
                raise
            identity = _holder_identity(held.holder)
            if identity != seen:
                seen = identity
                interval = poll_interval_seconds
            else:
                interval = min(ceiling, interval * 2.0)
            pause = interval * (0.5 + 0.5 * jitter())
            sleep(min(pause, max(0.0, deadline - now)))
    try:
        yield
    finally:
        lock.release()


def _holder_identity(holder: dict[str, Any]) -> tuple[Any, ...]:
    """What tells one claim on a lock from the next. The nonce is fresh per
    acquisition, so a pid that releases and immediately takes the lock again
    reads as a new holder — which it is, and which means the queue is moving."""
    return (holder.get("pid"), holder.get("host"), holder.get("nonce"))


__all__ = [
    "FileLock",
    "LockHeldError",
    "acquire",
    "live_holder",
    "prune_stale_locks",
    "retrying_lock",
]
