"""What a chat's directory holds that belongs to ONE process on ONE machine.

A chat folder travels. A box leases it, pulls it down, runs turns in it, pushes
it back and hands it to whichever box wakes the chat next — so everything under
it is copied between machines that share nothing but the bytes. Most of the
tree survives that trip unchanged. A few entries cannot, because they are not
facts about the chat at all: they are facts about the process that was holding
it on the machine that wrote them.

The per-chat write lock is the whole shape of the problem. ``.lock`` records a
pid and a hostname, and the rule that decides whether it may be reclaimed is
"the pid is dead ON THIS HOST" — so the moment it is copied to another machine
it is a different host's lock, is treated as live, and can never be reclaimed
there. The reclaim path then rotates it aside for forensics as
``.lock.stale.<ts>.<rand>``, and that rotation is a real file which is pushed,
pulled and rotated beside the next one: one more per take, forever.

Naming them here rather than at each call site is the point — the push, the
pull and the box that drives both ask this module the same question, so
"travels" and "does not travel" cannot drift apart. New local state is added by
:func:`register_local_state`, never by a second list somewhere else.

Deliberately NOT local: ``.runtime/``. The name looks process-scoped and its
contents are opaque, but ``.runtime/agent/`` is the harness's per-chat session
storage, and a chat's ``manifest.json`` pins the agent session id inside it. A
box that pulled the manifest without the storage it names cannot attach to the
pinned session and refuses to start the chat at all, so excluding the directory
would trade a few stale lock files for every resume on a fresh box. The
process-scoped files under it that HAVE been told apart are registered here by
their own paths: the agent database's write-ahead log and shared-memory index,
and the agent's own log files (:data:`AGENT_SESSION_SCRATCH_PATTERNS`).
"""

from __future__ import annotations

import fnmatch
from typing import Final

__all__ = [
    "is_local_state",
    "local_state_patterns",
    "register_local_state",
    "unregister_local_state",
]

#: Glob patterns, matched against a path RELATIVE to the directory that travels
#: (the chat folder), with ``/`` as the separator. Anchored at that root: a
#: pattern names where the state lives, so a file the agent itself wrote called
#: ``.lock`` deeper in its sandbox is the agent's file and travels with it.
_PATTERNS: set[str] = set()


def register_local_state(*patterns: str) -> None:
    """Declare that ``patterns`` name state that never leaves this machine.

    Idempotent, so a module that registers at import time is safe to import
    twice. Patterns are ``fnmatch`` globs against the root-relative path.
    """
    for pattern in patterns:
        cleaned = pattern.strip().strip("/")
        if not cleaned:
            raise ValueError("a local-state pattern must name a path")
        _PATTERNS.add(cleaned)


def is_local_state(relative: bytes | str) -> bool:
    """Whether ``relative`` names state that must not travel with the folder.

    Accepts the bytes the file walkers carry (a path on disk is bytes, and a
    name that is not UTF-8 still has to answer this question) as well as the
    string a wire path is.
    """
    # Not os.fsdecode: its error handler follows the host filesystem, and on
    # Windows that is surrogatepass, which raises on the very bytes this has
    # to answer for. The undecodable byte round-trips through a surrogate.
    text = relative.decode("utf-8", "surrogateescape") if isinstance(relative, bytes) else relative
    text = text.replace("\\", "/").strip("/")
    if not text:
        return False
    return any(fnmatch.fnmatchcase(text, pattern) for pattern in _PATTERNS)


def unregister_local_state(*patterns: str) -> None:
    """Drop patterns registered by :func:`register_local_state`.

    Registration is process-wide, so a caller that adds a rule for one tree
    (or a test that proves the seam) needs a way to take it back out.
    """
    for pattern in patterns:
        _PATTERNS.discard(pattern.strip().strip("/"))


def local_state_patterns() -> frozenset[str]:
    """The registered patterns, for a caller that reports rather than matches."""
    return frozenset(_PATTERNS)


#: The per-chat write lock, its forensic rotations and the guard that serializes
#: a reclaim. All three are keyed to a pid on a host (see
#: :mod:`alkera_core.project.locking`), so none of them means anything anywhere
#: else — and the rotations accumulate one per take if they are allowed to ride.
LOCK_PATTERNS: Final[tuple[str, ...]] = (
    ".lock",
    ".lock.stale.*",
    ".lock.reclaim",
    ".lock.reclaim.stale.*",
)

#: The blob store's GC lock, held only while a sweep runs in one process.
GC_LOCK_PATTERNS: Final[tuple[str, ...]] = ("blobs.gc.lock",)

#: The agent's own runtime directories. A box runs a chat's commands with
#: ``HOME`` at the chat's working directory, so every tool that keeps a cache or
#: a per-user install under ``~`` (pip, uv, npm, the shell's history) grows
#: ``.cache/`` and ``.local/`` there. They are that machine's scratch, not the
#: chat's work: streamed, they filled the drive and then its Trash every time a
#: tool rewrote them. Spelled from both roots a path is read against -- the
#: working directory the holder walks and the chat folder the drive files it
#: under.
AGENT_HOME_PATTERNS: Final[tuple[str, ...]] = tuple(
    f"{root}{name}{tail}"
    for root in ("", "scratch/")
    for name in (".cache", ".local")
    for tail in ("", "/*")
)

#: The chat's default Python environment. A box makes it under the harness
#: state on every spawn (``uv venv`` + ``ensurepip``, thousands of interpreter
#: files) and remakes it wherever the chat is served next, so it is that box's
#: scratch and never the chat's work: streamed, it filled the drive and queued
#: the real files behind it. The rest of ``.runtime`` (the agent's own database)
#: travels with the chat as before.
CHAT_ENVIRONMENT_PATTERNS: Final[tuple[str, ...]] = tuple(
    f"{root}.runtime/envs{tail}" for root in ("", "scratch/") for tail in ("", "/*")
)

#: The container's root overlay: what a sandboxed chat writes outside its own
#: folder (a package it installed system-wide, a file under ``/tmp``) lands in
#: this upper directory beside the chat's records. It is the shape of one
#: box's container, not the chat's work, and it is remade empty wherever the
#: chat is served next.
CONTAINER_OVERLAY_PATTERNS: Final[tuple[str, ...]] = tuple(
    f"{root}.overlay{tail}" for root in ("", "scratch/") for tail in ("", "/*")
)

#: The agent database's side files and the agent's own logs. ``agent.db``
#: travels — the manifest pins the session inside it — but its write-ahead
#: log and shared-memory index are one process's view of that file: pushed
#: beside it they are rewritten on every turn (a node pushed both every time
#: the agent wrote a part) and, on another machine, either ignored by SQLite
#: or read against a database they no longer match. The box folds the log
#: into the database before the folder is handed back, so the file alone
#: carries the session. The log directory is this box's diagnostics.
AGENT_SESSION_SCRATCH_PATTERNS: Final[tuple[str, ...]] = tuple(
    f"{root}.runtime/agent/{name}"
    for root in ("", "scratch/")
    for name in ("agent.db-wal", "agent.db-shm", "agent.db-journal", "log", "log/*")
)

#: What the harness writes beside the agent's state about the agent PROCESS:
#: the pid breadcrumb (a pid and its creation time on this host, read by this
#: host's orphan reaper) and the loopback URL the agent server listens on. Both
#: name one process on one machine; carried to the drive they sat in every
#: reader's copy of the chat, and on the next box they name a process that is
#: not there.
AGENT_PROCESS_PATTERNS: Final[tuple[str, ...]] = tuple(
    f"{root}.runtime/{name}" for root in ("", "scratch/") for name in ("pid", "listen-url")
)

register_local_state(
    *LOCK_PATTERNS,
    *GC_LOCK_PATTERNS,
    *AGENT_HOME_PATTERNS,
    *CHAT_ENVIRONMENT_PATTERNS,
    *CONTAINER_OVERLAY_PATTERNS,
    *AGENT_SESSION_SCRATCH_PATTERNS,
    *AGENT_PROCESS_PATTERNS,
)
