"""Chat uids an org worker hands out, recorded in the org's own data root.

On a box without per-org workers a chat's uid is a system user in the host's
``/etc/passwd`` (``sandbox_uid.ensure_chat_uid``). An org worker cannot write
the host's user table and must not: a uid there belongs to the whole box. Its
chat uids live in its own user namespace instead, so ``20000..59999`` inside
it are ids in the org's host range alone, and which chat holds which is
recorded here, in a file under the org's root (``ALKERA_SANDBOX_UID_LEDGER``,
set by the supervisor). The record outlives the processes: a chat that comes
back gets the uid its files already have, and a uid is never handed to a
second chat while the first's entry stands.

The ledger is the only thing standing between two chats and one uid, so it is
never trusted further than the disk proves it:

* it is replaced whole (a temporary file beside it, synced, renamed over it,
  the directory synced), under a lock on a separate ``.lock`` file, so a crash
  at any point leaves the old record or the new one, never a torn one;
* a ledger that exists but cannot be read (empty, truncated, not the shape
  this module writes) is a refusal whenever any chat's tree under the org's
  root is owned by a uid of the range: handing out ids again from an empty
  record would give a new chat a uid that still owns another chat's files;
* a fresh uid is one no record holds AND no tree under the org's root is
  owned by (:data:`TREE_DEPTH` deep, where every chat's trees start; the
  ownership steps hand each tree to its uid whole);
* when the range is spent, a uid is taken back from a chat whose files are
  all gone: a sweep of the whole org root finds nothing it owns, no process
  runs as it, and it was handed out more than :data:`RECLAIM_GRACE_S` ago
  (a chat whose uid was just recorded has not had its trees handed to it yet).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import stat
import time
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Final

from alkera_cli.harness.sandbox_ownership import trees_floor
from alkera_cli.harness.sandbox_steps import SandboxRefusedError

logger = logging.getLogger(__name__)

ENV_UID_LEDGER: Final = "ALKERA_SANDBOX_UID_LEDGER"
#: How deep below the org's root the check for a fresh uid looks: every
#: chat's trees (its folder, its runtime state, its agent state, a
#: workspace's shared tree) start within it.
TREE_DEPTH: Final = 8
#: How long after a uid was handed out it may be taken back from a chat that
#: owns nothing: long past the spawn that hands the chat's trees to it.
RECLAIM_GRACE_S: Final = 24 * 3600.0

RunningUids = Callable[[], set[int]]
"""The uids some process on this host runs as."""


def ledger_path(env: Mapping[str, str] | None = None) -> Path | None:
    """The ledger this process records chat uids in, or ``None`` on a box
    whose chat uids are host users. Only an absolute path is honoured."""
    raw = (os.environ if env is None else env).get(ENV_UID_LEDGER, "").strip()
    return Path(raw) if raw.startswith("/") else None


def running_uids() -> set[int]:
    """The real, effective, saved and filesystem uids of every process this
    host shows in ``/proc``; empty where there is none."""
    found: set[int] = set()
    try:
        entries = os.listdir("/proc")
    except OSError:
        return found
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/status", encoding="utf-8") as status:
                for line in status:
                    if line.startswith("Uid:"):
                        found.update(int(v) for v in line.split()[1:])
                        break
        except (OSError, ValueError):
            continue
    return found


def owners_under(root: Path, low: int, high: int, *, depth: int | None) -> set[int]:
    """The uids in ``low..high`` that own an entry below ``root``: at most
    ``depth`` levels down (``None``: all of it), on ``root``'s filesystem,
    no link followed."""
    found: set[int] = set()
    try:
        device = os.lstat(root).st_dev
    except OSError:
        return found
    pending: list[tuple[str, int]] = [(str(root), 0)]
    while pending:
        directory, level = pending.pop()
        try:
            listing = os.scandir(directory)
        except OSError:
            continue
        with listing:
            for entry in listing:
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if low <= info.st_uid <= high:
                    found.add(info.st_uid)
                deeper = depth is None or level + 1 < depth
                if stat.S_ISDIR(info.st_mode) and info.st_dev == device and deeper:
                    pending.append((entry.path, level + 1))
    return found


@contextlib.contextmanager
def _locked(path: Path) -> Iterator[None]:
    import fcntl  # POSIX only, as an org worker is

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path.with_name(f"{path.name}.lock"), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _read(path: Path) -> tuple[dict[str, object], dict[str, object]] | None:
    """The ledger's uids and when each was handed out; ``({}, {})`` when there
    is no ledger yet, ``None`` when there is one that cannot be read."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return {}, {}
    except OSError:
        return None
    with os.fdopen(fd, "r", encoding="utf-8") as handle:
        try:
            data = json.loads(handle.read())
        except (ValueError, UnicodeDecodeError):
            return None
    if not isinstance(data, dict):
        return None
    held, issued = data.get("uids"), data.get("issued", {})
    if not isinstance(held, dict) or not isinstance(issued, dict):
        return None
    return held, issued


def _write(path: Path, held: Mapping[str, object], issued: Mapping[str, object]) -> None:
    """Replace the ledger whole: a crash leaves the old record or the new."""
    temporary = path.with_name(f".{path.name}.tmp")
    with contextlib.suppress(FileNotFoundError):
        temporary.unlink()
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump({"uids": dict(held), "issued": dict(issued)}, handle, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _reclaim(
    held: dict[str, object],
    issued: dict[str, object],
    *,
    floor: Path,
    low: int,
    high: int,
    now: float,
    running: RunningUids,
) -> list[str]:
    """Drop the entries whose uid owns nothing under ``floor``, runs nothing,
    and was handed out longer ago than the grace; the names dropped."""
    owned = owners_under(floor, low, high, depth=None)
    busy = running()
    dropped: list[str] = []
    for name, uid in list(held.items()):
        when = issued.get(name, 0)
        since = now - when if isinstance(when, int | float) else now
        if isinstance(uid, int) and (uid in owned or uid in busy or since < RECLAIM_GRACE_S):
            continue
        del held[name]
        issued.pop(name, None)
        dropped.append(name)
    return dropped


def ledger_uid(
    path: Path,
    name: str,
    *,
    low: int,
    high: int,
    floor: Path | None = None,
    running: RunningUids = running_uids,
) -> int:
    """``name``'s uid in the ledger at ``path``: the one it holds, else the
    lowest id in ``low..high`` no record holds and no tree under the org's
    root (``floor``, the worker's trees floor when not given) is owned by,
    recorded before it is returned. Serialized by a lock beside the ledger, so
    two chats starting at once never take one id."""
    root = floor if floor is not None else trees_floor()
    with _locked(path):
        read = _read(path)
        if read is None:
            # Never guessed around: a uid handed out twice is two chats in
            # each other's files.
            if root is None or owners_under(root, low, high, depth=TREE_DEPTH):
                raise SandboxRefusedError(
                    f"the chat uid ledger {path} cannot be read while chat trees exist under "
                    "the org's root; restore it (or remove the trees) before chats can start"
                )
            logger.warning(
                "the chat uid ledger %s could not be read and no chat tree exists; "
                "starting a new one",
                path,
            )
            read = {}, {}
        held, issued = read
        uid = held.get(name)
        if isinstance(uid, int) and low <= uid <= high:
            return uid
        owned = owners_under(root, low, high, depth=TREE_DEPTH) if root is not None else set()

        def free() -> int | None:
            taken = {v for v in held.values() if isinstance(v, int)} | owned
            return next((i for i in range(low, high + 1) if i not in taken), None)

        now = time.time()
        fresh = free()
        if fresh is None and root is not None:
            dropped = _reclaim(
                held, issued, floor=root, low=low, high=high, now=now, running=running
            )
            if dropped:
                logger.info(
                    "took back the chat uids of %d chat(s) with no files left", len(dropped)
                )
                owned = owners_under(root, low, high, depth=TREE_DEPTH)
                fresh = free()
        if fresh is None:
            raise SandboxRefusedError("every chat uid of this org is taken")
        held[name] = fresh
        issued[name] = now
        _write(path, held, issued)
        return fresh


__all__ = [
    "ENV_UID_LEDGER",
    "RECLAIM_GRACE_S",
    "TREE_DEPTH",
    "RunningUids",
    "ledger_path",
    "ledger_uid",
    "owners_under",
    "running_uids",
]
