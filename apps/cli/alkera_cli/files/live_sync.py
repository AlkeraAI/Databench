"""The holder's live push: what the box writes reaches the drive while it writes.

A leased folder is held by one machine. A checkpoint pushes the whole folder at
once; this module is the other half: a watcher's
changes are classified, coalesced and flushed continuously, so a reader on the
web sees a row appear while the file is still being written.

Three properties shape it, and each is a refusal rather than a feature:

* **it never sends a path it cannot prove is inside the folder.** A change is
  resolved first and kept only when what it resolves to lies under the watched
  root, so a link planted in the tree cannot make the holder upload a file from
  elsewhere on the host under an innocent name;
* **it never sends bytes it cannot claim are the file's last bytes.** A file
  whose size or mtime moved while it was being hashed goes back on the queue
  instead of being uploaded, because a half-written file that lands as a
  version is indistinguishable on the drive from one the agent finished;
* **it stops itself.** A holder that stops hearing from the server cannot be
  *told* it lost the lease, so its own fence ends its permission to write after
  a stretch of silence and no further batch leaves here until a beat lands.

Two queues leave here. The **metadata queue** tells the drive what exists —
path, kind, size, modified time, mode — within a fraction of a second, so a
reader sees every row before a byte of it has moved; it inherits the first and
third refusals and never reads a file. The **content queue** moves the bytes on
its own cadence, and is the only place a file is hashed.

A file the transcript is about to name is **landed** (:meth:`LiveSync.land`):
the reply that references a chart the agent just wrote is published only once
that chart's bytes are on the drive, so no reader — the web transcript, a
Slack thread — is ever shown a reference to bytes that are still only here.
The agent has finished writing a file its reply names, so a landing does not
wait out the settle time; the hash is still proven against an unmoved stamp.

The content queue has an order. A file a reader on the drive is waiting for is
**promoted** (:meth:`LiveSync.promote`) and goes first, past the bandwidth
window as far as a burst allows; then small files in the order they were seen;
then the rest smallest first; packed git history last. No file is read while
it is still being written — one modified within the settle time waits in the
queue, promoted or not — which is the second refusal applied before the hash
rather than after it.

When the drive sends bytes for a file the box has changed since the two last
agreed, neither side's bytes are dropped. Once the drive's bytes have arrived,
the box's are kept under a staging name (``.<name>.<token>.alkera-conflict``)
as well, the drive's replace them under the real name in one rename — so the
name is never empty for a watcher to report deleted — and the staged bytes are
submitted to the drive as a conflicted copy of that node; the drive names the
copy and the staging file takes that name.
The holder never invents a copy's name, and a staging file a crash left behind
is submitted again on the next start and on every reconciliation pass.
Events are the fast path, not the truth, so three things back them:

* **the journal** (:mod:`alkera_cli.files.journal`). A metadata batch is
  written down before it is sent and marked acked with the ``live_seq`` the
  drive answered; a file the content queue owes is noted until its bytes land.
  A sync that starts over a journal resends the unacked batches first, under
  their own ``batch_id``, then puts the owed files back on the content queue,
  and only then starts listening to the disk;
* **the walk** (:meth:`LiveSync.reconcile`). Every :data:`RECONCILE_EVERY`
  seconds — doubling to :data:`RECONCILE_CAP` while nothing differs, back to
  the start after any batch — the folder is walked, a digest per directory is
  compared with the drive's, and only the entries of directories that differ
  are queued. A change the watcher never saw is found here. One walk at a
  time, never sooner than the interval, and it yields to the loop as it goes;
* **the drain** (:meth:`LiveSync.drain`). A release gives the content queue a
  bounded time to empty and reports what it could not send by path.

The library knows nothing about chats or the cloud service: it is handed a
root, a watcher, an API and a clock, which is what makes every rule above
reachable from a test with no network and no box.
"""

from __future__ import annotations

import asyncio
import contextlib
import gzip
import json
import logging
import os
import stat as stat_module
import threading
import time
import uuid
from collections import deque
from collections.abc import AsyncIterator, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Final, Literal, Protocol

import httpx
from alkera_core.chat_records import CHAT_RECORD_NAMES
from alkera_core.project.local_state import register_local_state

from alkera_cli.files.chat_fs import ChatTree
from alkera_cli.files.conflict_staging import CONFLICT_SUFFIX as _CONFLICT_SUFFIX
from alkera_cli.files.conflict_staging import conflict_staging_name
from alkera_cli.files.conflict_staging import staged_original as _staged_original
from alkera_cli.files.conflict_staging import staging_files as _staging_files
from alkera_cli.files.digest import EMPTY, DirDigest, child_value, from_hex
from alkera_cli.files.inbound_backoff import InboundBackoff
from alkera_cli.files.journal import LiveJournal
from alkera_cli.files.live_fetch import FENCED_CODE
from alkera_cli.files.live_fetch import fenced as _fenced
from alkera_cli.files.live_fetch import fetch_inbound as _fetch_inbound
from alkera_cli.files.live_paths import answer_count as _count
from alkera_cli.files.live_paths import answer_node as _answer_node
from alkera_cli.files.live_paths import head_source as _head_source
from alkera_cli.files.live_paths import mentions as _mentions
from alkera_cli.files.live_paths import stamp as _stamp
from alkera_cli.files.live_paths import tree_safe as _tree_safe
from alkera_cli.files.live_peer import LivePeer, PeerStates, RestPeerApi, Taken
from alkera_cli.files.live_tree_send import MISMATCH_CODE, TreeSend
from alkera_cli.files.mount import LeaseSupersededError, MountRecord, SelfFence
from alkera_cli.files.name_rules import RefusedNames
from alkera_cli.files.nodemap import KnownNode, NodeMapStore, restore_agreements
from alkera_cli.files.pull import PulledFile
from alkera_cli.files.push import (
    AgreedBase,
    FilesApi,
    UploadSessionError,
    _blake3_hex,
    _hash_file,
    conflict_code,
    push_paths,
)
from alkera_cli.files.refusals import status_of as _status_of
from alkera_cli.files.round_retry import RETRY_FIRST_WAIT, RETRY_MAX_WAIT, RoundRetry
from alkera_cli.files.settle import LIVE_SETTLE_MS, SettleWatch
from alkera_cli.files.spool import InboundSpool, stream_into
from alkera_cli.files.target import ContainmentError, MaterializationTarget
from alkera_cli.files.tree_watch import Change
from alkera_cli.files.walk import LIVE_SYNC_DEFAULT_PRESETS, EntryKind, ExportRules, walk
from alkera_cli.files.watcher import POLL_THE_DISK as _POLL_THE_DISK
from alkera_cli.files.watcher import TreeWatcher, live_watch_filter, wake_on
from alkera_cli.files.wire import facet_content_hash
from alkera_cli.host.backoff import doubled, exponential_delay, retry_after_of

__all__ = [
    "CONFLICT_NOTICES_KEPT",
    "DIGEST_BATCH",
    "DIGEST_NAMES_BATCH",
    "FENCED_CODE",
    "INBOUND_DOWNLOAD_FLOOR",
    "INBOUND_MIN_BYTES_PER_SECOND",
    "KEEPALIVE_EVERY",
    "LAND_WAIT_BYTES",
    "MISMATCH_CODE",
    "RECHECK_WINDOW",
    "RECONCILE_CAP",
    "RECONCILE_EVERY",
    "RECORD_NAMES",
    "RETRY_MAX_WAIT",
    "SMALL_FILE_BYTES",
    "STAGING_SCAN_EVERY",
    "THROTTLE_ATTEMPTS",
    "TOO_MANY_CODE",
    "WALK_YIELD_EVERY",
    "AgreedBase",
    "Clock",
    "ConflictAnswer",
    "ConflictNotice",
    "ConflictTargetGoneError",
    "DriveDigests",
    "InboundEntry",
    "LiveApi",
    "LiveBatchAnswer",
    "LiveCadence",
    "LiveEntry",
    "LiveState",
    "LiveSync",
    "PendingChange",
    "RestLiveApi",
    "TreeAnswer",
    "TreeEntry",
    "TreeWatcher",
    "Watcher",
    "conflict_staging_name",
    "hash_file",
    "inbound_deadline",
    "live_watch_filter",
]


#: The least time one inbound download is given, whatever its size. Generous
#: on purpose: a file a person dropped into the chat on the web is downloaded
#: whole, and a ceiling that a slow link could not meet meant the file was
#: fetched, thrown away, and asked for again on every beat — it never landed
#: at all. Ten minutes covers a cold link; the size term below covers a large
#: file on one.
INBOUND_DOWNLOAD_FLOOR: Final = 600.0

#: The bandwidth an inbound download is budgeted at, above the floor. Deliberately
#: far below any real link (128 KiB/s), so the ceiling is a guard against a
#: transfer that has stalled and never a bound a working one meets.
INBOUND_MIN_BYTES_PER_SECOND: Final = 131_072

#: The code the plane answers when the lease already has more nodes in flight
#: than it will hold. The rows are the only thing refused: the bytes still
#: land, and as they do the rows the drive holds drain, so the refusal is a
#: "not now" and never a reason to stop streaming.
TOO_MANY_CODE: Final = "files.live_too_many"


#: How long, in seconds, a file the holder has just seen is looked at again on
#: its own, whatever the watcher says. The polling watcher compares a file's
#: modification time to the whole second and never its size, so a file
#: rewritten inside the second it was last seen in produces no event at all —
#: an agent saving its report twice in a row loses the second save, silently
#: and for good. Two seconds covers that second from wherever in it the watcher
#: looked, with room for a filesystem that keeps coarser times. Zero where the
#: watcher is told of every write (inotify), which has no such second.
RECHECK_WINDOW: Final = 2.0 if _POLL_THE_DISK else 0.0

#: The size at or under which a file is "small" in the content queue: small
#: files go in the order they were first seen, ahead of every larger one, so a
#: reader waiting on a note is never behind a dataset that happened to be
#: written a moment earlier.
SMALL_FILE_BYTES: Final = 1_048_576

#: The largest file a landing waits for. A reply naming a chart waits for the
#: chart; one naming a dataset larger than this does not hold the transcript
#: while it uploads — the file is still promoted, and a reader who opens it
#: meets the on-demand path.
LAND_WAIT_BYTES: Final = 16_777_216

#: Where a repository keeps its packed history. A pack is large, nobody reads
#: one on the web, and it is rewritten whole by every ``git gc``, so it waits
#: behind everything a person might open.
_COLD_SEGMENTS: Final = (".git", "objects", "pack")


def _is_cold(relative: str) -> bool:
    """Whether ``relative`` is a path the content queue sends last."""
    parts = relative.split("/")
    width = len(_COLD_SEGMENTS)
    return any(
        tuple(parts[start : start + width]) == _COLD_SEGMENTS for start in range(len(parts) - width)
    )


def byte_deadline(size: int | None, *, floor: float) -> float:
    """How long a transfer of ``size`` bytes may take before it is cut off.

    The floor, or the size over a conservative bandwidth, whichever is
    longer — so a small file on a slow link and a huge file on a fast one both
    have a ceiling only a stalled transfer reaches. Every wait that stands in
    front of bytes is sized through here rather than by a constant, because a
    constant that is generous for a note is a guillotine for a terabyte.

    ``floor`` is what the wait costs when nothing is known about the size: a
    sizeless transfer is budgeted at the caller's own patience.
    """
    if size is None or size <= 0:
        return floor
    return max(floor, size / INBOUND_MIN_BYTES_PER_SECOND)


def inbound_deadline(size: int | None) -> float:
    """The ceiling on one inbound download, floored at :data:`INBOUND_DOWNLOAD_FLOOR`."""
    return byte_deadline(size, floor=INBOUND_DOWNLOAD_FLOOR)


#: How many times one throttled batch is offered again before the round gives
#: up on it. Giving up is not fatal: what the server never accepted is what it
#: still holds, so the next beat offers the same rows again.
THROTTLE_ATTEMPTS: Final = 3

#: The longest this holder waits on a server's ``Retry-After`` before it stops
#: believing the number. A live sync that sleeps for minutes is not live, and
#: the rows are not lost by waiting less — they are offered again.
THROTTLE_MAX_WAIT: Final = 30.0

#: The first wait when the server throttles without saying for how long. It
#: doubles per attempt, bounded by :data:`THROTTLE_MAX_WAIT`.
THROTTLE_FIRST_WAIT: Final = 1.0

#: The answers that mean "not now" rather than "not ever". A 429 is a ceiling
#: this box shares with every other one behind its address, and a 503 is the
#: API between two of its own breaths: neither says anything about this
#: holder's right to the folder, so neither may end its live plane.
_THROTTLED: Final = frozenset({429, 503})

#: How long the wire may be quiet before a landed beat sends an empty batch
#: to say the plane is alive. The drive reads a lease that has not synced for
#: two sync intervals (a minute) as a holder that has gone quiet and tells
#: every reader so; a holder whose agent has simply written nothing for a
#: while is not that holder, and this is how it says so — well inside the
#: minute, on the beat that already runs.
KEEPALIVE_EVERY: Final = 30.0

#: How often, in seconds, the folder is walked and compared with the drive
#: while something has moved lately. A minute: a change the watcher dropped is
#: on the drive within one, and a 10 000-file clone walks in well under that.
RECONCILE_EVERY: Final = 60.0

#: The longest the walk waits while nothing differs. The interval doubles from
#: :data:`RECONCILE_EVERY` up to this, and any batch puts it back.
RECONCILE_CAP: Final = 600.0

#: How many entries the walk visits before it lets the loop run something else.
WALK_YIELD_EVERY: Final = 500

#: The most directories one digest request names: the route's own ceiling.
DIGEST_BATCH: Final = 4000
#: How many of the files a restarted box finds changed are offered to its text
#: peer before anything says people have them open (one read each): it knows
#: which are open only once a write back says so, and the agent's edits made
#: while it was down waited for that.
PEER_PROBES_AT_START: Final = 32

#: The most directories one digest request asks the children of: the route's
#: own ceiling on ``names``.
DIGEST_NAMES_BATCH: Final = 1000

#: The body size above which a digest request travels gzip-compressed, as a
#: ``tree`` batch does.
_DIGEST_GZIP_ABOVE: Final = 8192

#: How long a draining release waits between rounds that left work behind.
_DRAIN_STEP: Final = 0.5

#: The suffix of the temporary name an older daemon downloaded inbound bytes
#: under, beside the destination in the chat's tree. Downloads now stream into
#: the daemon's own spool (``alkera_cli.files.spool``); a leftover under this
#: name in a tree is still this machine's business and never synced.
_INBOUND_SUFFIX: Final = ".alkera-inbound"

# Both staging names are this machine's business alone. Registered as local
# state, every door that asks that one question refuses them: the watcher's
# filter, the live push's classifier (so neither the metadata queue nor the
# content queue carries them) and the checkpoint push's walk.
register_local_state(
    f".*{_INBOUND_SUFFIX}",
    f"*/.*{_INBOUND_SUFFIX}",
    f".*{_CONFLICT_SUFFIX}",
    f"*/.*{_CONFLICT_SUFFIX}",
)

#: How many conflicts per folder the holder remembers for the agent. Each is
#: said once, on the next tool result that names its path; the oldest go first
#: when a folder conflicts more often than the agent looks.
CONFLICT_NOTICES_KEPT: Final = 20

#: How often, at most, a reconciliation pass walks the whole root for staging
#: files it does not already know of. The copies this sync staged itself are
#: offered again on every pass; the walk is for the ones an earlier run left,
#: which the start of this one already looked for — so it is a belt, and a walk
#: of a large folder on every drain would cost more than it could find.
STAGING_SCAN_EVERY: Final = 30.0


logger = logging.getLogger(__name__)

#: What a holder can say about a node. ``applied`` and ``superseded`` are the
#: answers to something the server sent inbound, never part of a push. A diverged
#: inbound write or delete is answered ``applied`` with the name the box's own
#: bytes went to (:attr:`LiveEntry.displaced`); ``superseded`` stays in the
#: vocabulary for a server that still reads it.
LiveState = Literal["writing", "uploading", "on_box", "deferred", "applied", "superseded"]

#: What the server can ask a holder to take onto the box.
InboundState = Literal["inbound", "inbound_delete", "inbound_rename"]

#: A file's whole-file BLAKE3 and size — what a version's content hash is made
#: of, so the live push and the checkpoint push agree on what "unchanged" means.
hash_file = _hash_file


@dataclass(eq=False)
class _Landing:
    """One :meth:`LiveSync.land` call: the paths it still waits on, the ones
    that landed, and the event that wakes it."""

    waiting: set[str]
    landed: set[str] = field(default_factory=set)
    done: threading.Event = field(default_factory=threading.Event)


@dataclass(frozen=True, slots=True)
class LiveCadence:
    """The numbers the server serves with a live lease.

    Served rather than compiled in because they are a deployment's call: an
    install on a slow link wants a smaller bandwidth window, and a holder has
    to obey the window named by the server that pays for it.
    """

    debounce_ms: int = 300
    batch_every_ms: int = 500
    max_batch_entries: int = 256
    max_file_bytes: int = 33_554_432
    bandwidth_bytes_per_minute: int = 134_217_728
    max_pending_entries: int = 500_000
    inbound: bool = False
    metadata_every_ms: int = 300
    """How long the first change waits for company before the metadata queue
    is sent. The content cadence above is the bytes'; this one is the rows'."""
    metadata_max_entries: int = 2000
    """The most entries one ``tree`` batch carries, and the queue length that
    sends it without waiting for the timer."""
    metadata_gzip_bytes: int = 8192
    """The body size above which a ``tree`` batch travels gzip-compressed."""
    settle_ms: int = 2000
    """How long a file must have gone unchanged before its bytes are read.
    A file modified more recently than this is still being written as far as
    anyone can tell, and hashing it would only prove the hash raced the
    writer: it waits in the queue, promoted or not, and goes once it settles."""
    live_promote_burst_bytes: int = 67_108_864
    """How many bytes a minute a promoted file may move past the bandwidth
    window. A reader is waiting on a promotion, so it goes ahead of the window
    — but only this far, or a reader clicking through a folder of large files
    would spend the window the whole folder shares."""
    release_drain_ms: int = 120_000
    """How long a release gives the content queue to empty before it hands
    the folder back and reports what is still on the machine."""

    @classmethod
    def from_grant(cls, block: Mapping[str, Any] | None) -> LiveCadence:
        """The cadence a lease grant's live block names, defaults for the rest.

        An older server that serves no block — or a newer one that serves a key
        this build has never heard of — leaves a usable cadence behind rather
        than an error, the same forward compatibility the persisted models keep.
        """
        served = dict(block or {})
        fallback = cls()
        return cls(
            debounce_ms=int(served.get("debounceMs", fallback.debounce_ms)),
            batch_every_ms=int(served.get("batchEveryMs", fallback.batch_every_ms)),
            max_batch_entries=int(served.get("maxBatchEntries", fallback.max_batch_entries)),
            max_file_bytes=int(served.get("maxFileBytes", fallback.max_file_bytes)),
            bandwidth_bytes_per_minute=int(
                served.get("bandwidthBytesPerMinute", fallback.bandwidth_bytes_per_minute)
            ),
            max_pending_entries=int(served.get("maxPendingEntries", fallback.max_pending_entries)),
            inbound=bool(served.get("inbound", fallback.inbound)),
            metadata_every_ms=int(served.get("metadataEveryMs", fallback.metadata_every_ms)),
            metadata_max_entries=int(
                served.get("metadataMaxEntries", fallback.metadata_max_entries)
            ),
            metadata_gzip_bytes=int(served.get("metadataGzipBytes", fallback.metadata_gzip_bytes)),
            settle_ms=int(served.get("settleMs", fallback.settle_ms)),
            live_promote_burst_bytes=int(
                served.get("promoteBurstBytes", fallback.live_promote_burst_bytes)
            ),
            release_drain_ms=int(served.get("releaseDrainMs", fallback.release_drain_ms)),
        )


@dataclass(frozen=True, slots=True)
class LiveEntry:
    """One node's state, as the holder reports it in a batch."""

    node_id: str
    state: LiveState
    box_size: int | None = None
    box_mtime: datetime | None = None
    displaced: str | None = None
    """Where the box's own bytes went when the drive's replaced them: the
    conflicted copy's path under the watched root, as the drive named it."""

    def to_wire(self) -> dict[str, Any]:
        entry: dict[str, Any] = {"nodeId": self.node_id, "state": self.state}
        if self.box_size is not None:
            entry["boxSize"] = self.box_size
        if self.box_mtime is not None:
            entry["boxMtime"] = self.box_mtime.isoformat()
        if self.displaced is not None:
            entry["displaced"] = self.displaced
        return entry


@dataclass(frozen=True, slots=True)
class ConflictAnswer:
    """What the drive answers a conflict submission with: the copy it made."""

    node_id: str
    name: str
    conflict_id: str | None = None


@dataclass(frozen=True, slots=True)
class ConflictNotice:
    """One conflict this holder settled, as the agent is told about it."""

    path: str
    """The file whose name the drive's bytes kept, under the watched root."""
    copy: str
    """Where the box's own bytes are now, under the watched root."""
    deleted: bool = False
    """Whether the drive's side was a delete rather than newer bytes."""
    kept_ours: bool = False
    """The other direction: the box's bytes kept the name, and ``copy`` holds
    the web's version they displaced on the drive."""

    def sentence(self) -> str:
        if self.kept_ours:
            return (
                f"Your version of {self.path} kept the name; the version saved on the "
                f"web is at {self.copy}."
            )
        if self.deleted:
            return f"{self.path} was deleted on the web; your version is at {self.copy}."
        return f"A newer copy of {self.path} arrived from the web; your version is at {self.copy}."


@dataclass(frozen=True, slots=True)
class LiveBatchAnswer:
    """What the live plane answers a batch with."""

    live_seq: int = 0
    pending: int = 0


TreeOp = Literal["upsert", "rename", "delete"]


@dataclass(frozen=True, slots=True)
class TreeEntry:
    """One row of a ``tree`` batch: what exists at a path, or that nothing does.

    ``path`` (and a rename's ``from_``) is relative to the watched root; the
    API adds the step down from the leased folder when it puts it on the wire.
    """

    op: TreeOp
    path: str
    kind: Literal["file", "dir"] | None = None
    from_: str | None = None
    size: int | None = None
    mtime_ns: int | None = None
    mode: int | None = None
    hash: str | None = None

    def fingerprint(self) -> tuple[Any, ...]:
        """What the drive would hold for this path once the entry lands."""
        return (self.kind, self.size, self.mtime_ns, self.mode, self.hash)

    def to_wire(self, step: str = "") -> dict[str, Any]:
        """The entry as the route reads it, its paths measured from the lease."""

        def under(relative: str) -> str:
            return f"{step}/{relative}" if step else relative

        entry: dict[str, Any] = {"op": self.op, "path": under(self.path)}
        if self.op == "delete":
            return entry
        entry["kind"] = self.kind
        if self.op == "rename" and self.from_ is not None:
            entry["from"] = under(self.from_)
        if self.kind == "file":
            entry["size"] = self.size
            entry["mtime_ns"] = self.mtime_ns
            entry["mode"] = self.mode
            entry["hash"] = self.hash
        return entry

    def to_journal(self) -> dict[str, Any]:
        """The entry as the journal keeps it: every field, root-relative."""
        return {
            "op": self.op,
            "path": self.path,
            "kind": self.kind,
            "from": self.from_,
            "size": self.size,
            "mtime_ns": self.mtime_ns,
            "mode": self.mode,
            "hash": self.hash,
        }

    @classmethod
    def from_journal(cls, row: Mapping[str, Any]) -> TreeEntry:
        return cls(
            op=row["op"],
            path=row["path"],
            kind=row.get("kind"),
            from_=row.get("from"),
            size=row.get("size"),
            mtime_ns=row.get("mtime_ns"),
            mode=row.get("mode"),
            hash=row.get("hash"),
        )


@dataclass(frozen=True, slots=True)
class TreeAnswer:
    """What the tree route answers a batch with."""

    live_seq: int = 0
    applied: int = 0
    landing_count: int = 0


class ConflictTargetGoneError(RuntimeError):
    """The node a conflicted copy was to sit beside was trashed while its
    upload session was open, so the drive refuses the commit (404).

    The session is stored under the submission's idempotency key, so the same
    key replays it and meets the same refusal every time: only a submission
    under another key, naming no node, can file the bytes.
    """


@dataclass(frozen=True, slots=True)
class DriveDigests:
    """What the digest route answers: the drive's digest of each directory it
    lists, and the names of the children of each directory asked to list them.

    A directory the drive does not list is absent from ``digests``; one asked
    to list its children that the drive does not have lists none. A child the
    drive is still waiting for this holder to take is never named: its absence
    from the disk is not a delete.
    """

    digests: Mapping[str, DirDigest] = field(default_factory=dict)
    children: Mapping[str, Sequence[str]] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class InboundEntry:
    """A node the server wants this holder to take onto the box."""

    node_id: str
    state: InboundState
    seq: int = 0


@dataclass(frozen=True, slots=True)
class PendingChange:
    """A classified change. The queue is keyed by ``relative``, which is what
    makes repeated writes to one file inside one window a single upload."""

    relative: str
    kind: Literal["write", "delete"]
    path: Path


class Watcher(Protocol):
    """The source of filesystem changes — :class:`TreeWatcher` in production."""

    async def changes(self) -> AsyncIterator[set[tuple[Change, str]]]: ...


class Clock(Protocol):
    """Monotonic only: a wall clock a user (or NTP) moves backwards would hand
    a superseded holder more time to write, not less."""

    def monotonic(self) -> float: ...


class LiveApi(Protocol):
    """The slice of the fenced Files API a live sync drives.

    ``tree`` makes the rows: every path it names exists (or is gone) on the
    drive once it answers, bytes or no bytes. ``resolve`` reads the node id
    filed at each path and makes nothing — a path the drive does not hold is
    simply absent from the answer.
    """

    def tree(
        self, batch_id: str, entries: Sequence[TreeEntry], *, gzip_above: int
    ) -> TreeAnswer: ...

    def resolve(self, paths: Sequence[str]) -> dict[str, str]: ...

    def digests(self, paths: Sequence[str], *, names: Sequence[str] = ()) -> DriveDigests:
        """The drive's digest of each directory in ``paths`` (root-relative),
        and the children the drive files in each directory of ``names`` (each
        also one of ``paths``)."""
        ...

    def upload(
        self, rel_path: str, node_id: str, size: int, *, base: AgreedBase | None = None
    ) -> str | None:
        """Send the file's current bytes onto the node filed at ``rel_path``.

        ``base`` is what this holder last agreed the file held and at which
        etag. The write is fenced on it, so a head somebody else moved since is
        settled by the drive -- both byte sequences kept -- rather than
        overwritten. Answers the etag the drive filed these bytes at, or
        ``None`` when that could not be confirmed.
        """
        ...

    def live_batch(self, entries: Sequence[LiveEntry]) -> LiveBatchAnswer: ...

    def inbound(self) -> list[InboundEntry]: ...

    def item(self, node_id: str) -> Mapping[str, Any]: ...

    def download(
        self, node_id: str, into: Path, *, deadline: float = INBOUND_DOWNLOAD_FLOOR
    ) -> None: ...

    def submit_conflict(
        self, staged: Path, relative: str, *, conflict_of: str | None, token: str
    ) -> ConflictAnswer:
        """Send ``staged`` as a conflicted copy of the node filed at ``relative``.

        ``conflict_of`` is that node; ``None`` when the holder cannot say, and
        the drive files the bytes as a new file beside ``relative``. ``token``
        is the staging attempt's own, so a submission repeated after a crash is
        answered with the copy the first one made rather than a second copy.
        Raises :class:`ConflictTargetGoneError` when ``conflict_of`` was
        trashed after the session opened.
        """
        ...


def throttle_wait(failure: BaseException, attempt: int) -> float | None:
    """How long to wait before offering a refused call again, or ``None``.

    ``None`` means this was not a "not now" — a refusal that will be the same
    refusal after any wait, which the caller must act on rather than sleep
    through. A server that named a ``Retry-After`` is obeyed up to the
    holder's own ceiling, since it is the party that knows what it is
    protecting; a server that named nothing gets a doubling wait.
    """
    status = _status_of(failure)
    if status not in _THROTTLED:
        return None
    asked = retry_after_of(failure)
    if asked is None:
        asked = exponential_delay(attempt, first=THROTTLE_FIRST_WAIT, cap=THROTTLE_MAX_WAIT)
    return max(0.0, min(asked, THROTTLE_MAX_WAIT))


def _ignore(relative: str) -> None:
    """The default for a caller that does not care which file had to wait."""
    del relative


@dataclass
class LiveSync:
    """One leased folder's live push."""

    root: Path
    record: MountRecord
    cadence: LiveCadence
    api: LiveApi
    watcher: Watcher
    fence: SelfFence
    clock: Clock
    on_deferred: Callable[[str], None] = _ignore
    hasher: Callable[[Path], tuple[str, int]] = hash_file
    sleep: Callable[[float], None] = time.sleep
    """How a throttled round waits before offering itself again. Injected so a
    test drives the backoff without spending the wait."""

    root_path: str = ""
    """Where on the drive ``root`` is, which is what an inbound item's path is
    measured against. The leased node's own path unless the sync watches a
    directory inside the lease — an agent's working directory is one."""

    chat_id: str = ""
    """Which chat's folder this is. Carried for the log and nothing else: one
    box holds many folders at once and their lines interleave in one file, so
    a line that names only a count of entries names no folder an operator can
    go and look at."""

    tombstones: set[str] = field(default_factory=set)
    """Relative paths trashed live. The checkpoint push skips them, or it would
    put back the very file the box deleted."""

    respect_gitignore: bool = True
    """Whether a path the root's ``.gitignore`` names stays on the box, exactly
    as the checkpoint push leaves it there."""

    exclude_presets: tuple[str, ...] = LIVE_SYNC_DEFAULT_PRESETS
    """The export presets (``venv``, ``node_modules``) this folder leaves out."""

    recheck_window: float = RECHECK_WINDOW
    """How long a file just seen is looked at again without an event
    (:data:`RECHECK_WINDOW`). Zero turns the second look off."""

    wall_ns: Callable[[], int] = time.time_ns
    """The wall clock a file's modification time is measured against, to tell
    a file written just now from one that has sat untouched for an hour."""

    journal: LiveJournal | None = None
    """Where the promises this sync makes the drive are written down. None
    keeps them in memory only, which is what a caller without a mount has."""

    reconcile_every: float = RECONCILE_EVERY
    """The walk's interval while things move (:data:`RECONCILE_EVERY`)."""

    node_map: NodeMapStore | None = None
    """Where which node each file is gets written down for the next process
    on this folder (:meth:`seed`). None keeps it in memory only."""

    reconcile_cap: float = RECONCILE_CAP
    """The walk's interval once nothing has differed for a while."""

    peer: LivePeer | None = None
    """The folder's text peer (:mod:`.live_peer`): a held file goes to its live
    document (uploaded only while the drive lacks it); write backs are not fetched."""

    #: The chat's tree (every touch of ``root``) and where inbound bytes stream first.
    tree: ChatTree = field(init=False)
    spool: InboundSpool = field(init=False)

    remainder: list[str] | None = field(default=None, init=False)
    """What the last drain left on the machine, root-relative, sorted."""

    reconciling: bool = field(default=False, init=False)
    """Whether a walk is running now. There is never a second."""

    _pending: dict[str, PendingChange] = field(default_factory=dict, init=False)
    _nodes: dict[str, str] = field(default_factory=dict, init=False)
    _known: dict[str, tuple[str, int]] = field(default_factory=dict, init=False)
    _etags: dict[str, str] = field(default_factory=dict, init=False)
    """The etag the drive held each path's agreed bytes (:attr:`_known`) at:
    the base the next upload of that path is fenced on. Taken only from what
    the drive confirmed -- an upload it filed, an inbound item it described --
    never from a listing that merely saw the head move."""
    _etag_digests: dict[str, str] = field(default_factory=dict, init=False)
    """The digest of the bytes this holder uploaded and the drive filed at
    :attr:`_etags`, while the file on disk descends from them (see
    :meth:`_agreed_version`)."""
    _inbound_etags: dict[str, str] = field(default_factory=dict, init=False)
    """The etag each inbound node was described at, until its bytes land."""
    _inbound_copy_of: dict[str, str] = field(default_factory=dict, init=False)
    """The node each inbound conflicted copy was kept for, by the copy's node."""
    _swept: bool = field(default=False, init=False)
    _in_round: frozenset[str] = field(default=frozenset(), init=False)
    """What the round in progress is sending: no longer queued, not yet agreed."""
    """Whether this run swept the tree: each :meth:`run` arms a new watch, so each sweeps."""
    _session_heads: set[str] = field(default_factory=set, init=False)
    """Inbound nodes whose head a live session wrote back (``document_snapshot``),
    and what a restarted box's first sweep found changed, until its peer tried."""
    _inbound_hashes: dict[str, str] = field(default_factory=dict, init=False)
    """The content hash each inbound node was described with, until it lands."""
    _stamps: dict[str, tuple[int, int]] = field(default_factory=dict, init=False)
    """The ``(size, mtime_ns)`` each agreed digest in :attr:`_known` was taken
    at. A file still wearing its agreed stamp cannot hold different bytes, so
    it is the pre-check that keeps a coalesced folder event from reading a
    whole directory."""
    _spent: list[tuple[float, int]] = field(default_factory=list, init=False)
    _last_flush: float = field(default=0.0, init=False)
    _last_sent: float = field(default=0.0, init=False)
    _fenced_since: float | None = field(default=None, init=False)
    _retry: RoundRetry = field(default_factory=RoundRetry, init=False)
    _inbound_sizes: dict[str, int] = field(default_factory=dict, init=False)
    _inbound_backoff: InboundBackoff = field(default_factory=InboundBackoff, init=False)
    _refused_names: RefusedNames = field(default_factory=RefusedNames, init=False)
    _inbound_pass: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _meta: dict[str, TreeEntry] = field(default_factory=dict, init=False)
    """The metadata queue: the latest word on each path, keyed by the path."""
    _meta_since: float | None = field(default=None, init=False)
    _renames_from: dict[str, str] = field(default_factory=dict, init=False)
    """A queued rename's source, mapped to the destination it is keyed under."""
    _sent_meta: dict[str, tuple[Any, ...]] = field(default_factory=dict, init=False)
    """What the drive last accepted for each path, so a folder event that
    re-offers an untouched file queues nothing."""
    _meta_retry_at: float = field(default=0.0, init=False)
    _meta_wait: float = field(default=0.0, init=False)
    _meta_attempts: int = field(default=0, init=False)
    _meta_failing: bool = field(default=False, init=False)
    _recheck: dict[str, tuple[tuple[int, int], float]] = field(default_factory=dict, init=False)
    """Files seen within the watcher's blind second: the ``(size, mtime_ns)``
    each was seen at, and the monotonic time the second look stops."""
    _rules: ExportRules | None = field(default=None, init=False, repr=False)
    _rules_stamp: tuple[Any, ...] | None = field(default=None, init=False, repr=False)
    _promote_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    """Guards the two promotion tables below, which the machine channel reads
    and writes from the socket's thread while the flush runs on this sync's."""
    _promote_inbox: list[str] = field(default_factory=list, init=False)
    """Promotions asked for and not yet taken onto the queue by the flush."""
    _promoted: dict[str, int] = field(default_factory=dict, init=False)
    """Promoted paths on the queue, each with the order it was asked for in."""
    _promote_seq: int = field(default=0, init=False)
    _landings: list[_Landing] = field(default_factory=list, init=False)
    """The callers of :meth:`land` still waiting, each on its own paths."""
    _urgent: set[str] = field(default_factory=set, init=False)
    """Paths a landing asked for: read without waiting out the settle time."""
    _first_seen: dict[str, int] = field(default_factory=dict, init=False)
    """When each queued write was first seen, as a running count: the order
    small files go in."""
    _seen_seq: int = field(default=0, init=False)
    _burst_spent: list[tuple[float, int]] = field(default_factory=list, init=False)
    """What promotions moved past the bandwidth window in the last minute."""
    _bursting: set[str] = field(default_factory=set, init=False)
    """The uploads of this round that go on the burst rather than the window."""
    _settle: SettleWatch = field(init=False)
    """For a file too fresh to read: the stamp it was last seen at and the
    monotonic time it was first seen wearing it."""
    _conflicts: deque[ConflictNotice] = field(
        default_factory=lambda: deque(maxlen=CONFLICT_NOTICES_KEPT), init=False
    )
    """The conflicts the agent has not been told about yet, oldest first."""
    _conflict_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    """Guards :attr:`_conflicts`: the drain thread adds to it while the tool
    dispatch reads it on the chat's."""
    _staged: set[Path] = field(default_factory=set, init=False)
    """Staging files this sync knows of that the drive has not taken yet."""
    _scanned_at: float | None = field(default=None, init=False)
    """When the root was last walked for staging files, on the monotonic clock."""
    _unplaced: set[Path] = field(default_factory=set, init=False)
    """Staging files the drive answered with a name this holder would not put
    them under. Not submitted again by this process, or every pass would ask the
    drive for another copy of the same bytes."""
    _journaled: set[str] = field(default_factory=set, init=False)
    """The owed files the journal already holds an intent for."""
    _reconcile_interval: float = field(default=RECONCILE_EVERY, init=False)
    _last_reconcile: float = field(default=0.0, init=False)
    _drain_for: float | None = field(default=None, init=False)
    _map_written: dict[str, KnownNode] | None = field(default=None, init=False)
    """What the node map last held, so a round that changed nothing writes nothing."""
    _map_written_at: float | None = field(default=None, init=False)
    _map_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        self._last_flush = self.clock.monotonic()
        self._last_sent = self._last_flush
        self._last_reconcile = self._last_flush
        self._reconcile_interval = self.reconcile_every
        # Read through the sync, so a clock swapped after construction holds.
        self._settle = SettleWatch(
            wall_ns=lambda: self.wall_ns(), monotonic=lambda: self.clock.monotonic()
        )
        self.root_path = (self.root_path or self.record.org_path).strip().strip("/")
        self.tree = ChatTree.for_chat(self.root, self.chat_id or None)
        self.hasher = self.tree.digest if self.hasher is hash_file else self.hasher
        self.spool = InboundSpool(
            self.journal.path.with_suffix(".inbound") if self.journal else None
        )
        if self.peer is None and isinstance(self.api, RestLiveApi):
            states = PeerStates.beside(self.journal)  # its text peer keeps what it wrote
            self.peer = LivePeer(api=self.api.text_peer(), tree=self.tree, states=states)
        if self.peer is not None:
            # The peer asks where each node is filed and what was agreed.
            nodes, known = self._nodes, self._known
            self.peer.relative_of = lambda node: next(
                (r for r, n in nodes.items() if n == node), None
            )
            self.peer.agreed = lambda r: known[r][0] if r in known else None
            self.peer.agreed_etag = self._agreed_version
            self.peer.descend = self._descended
            self.peer.on_join = lambda node, _relative: self._session_heads.add(node)

    def sent_live(self, step: str, *, running: bool) -> list[str]:
        """What this plane sends itself, from the folder ``step`` above it, for the checkpoint push
        to leave alone: while it runs, what it queued or is sending (all, before its first sweep)
        and its peer holds; once stopped, the peer's settled files. Never a file whose document the
        drive does not hold."""
        if not step:
            return []
        if running and not self._swept:
            return [step]
        peer = self.peer
        held = set() if peer is None else set(peer.owned() if running else peer.settled())
        held -= set() if peer is None else set(peer.unsaved())
        queued = set(self._pending) | self._in_round if running else set()
        return [f"{step}/{relative}" for relative in sorted(held | queued)]

    @property
    def _who(self) -> str:
        """What every line this sync logs names itself by."""
        return (
            f"chat {self.chat_id or '?'} folder {self.root_path or self.root} "
            f"(lease {self.record.node_id})"
        )

    @property
    def _log_ids(self) -> dict[str, str]:
        """The same two ids as fields, for a handler that reads them."""
        return {"chat_id": self.chat_id, "lease_node_id": self.record.node_id}

    @property
    def pending(self) -> Mapping[str, PendingChange]:
        """What the next flush will send, keyed by root-relative path."""
        return dict(self._pending)

    @property
    def metadata(self) -> Mapping[str, TreeEntry]:
        """What the next ``tree`` batch will say, keyed by root-relative path."""
        return dict(self._meta)

    @property
    def fenced(self) -> bool:
        """Whether the self-fence is closed: too long since a beat landed, so nothing leaves here
        until one does. The holder's status, for whoever reports it — a plane that is running but
        fenced is a plane that is deaf, and the difference has to be readable somewhere."""
        return self.fence.expired()

    @property
    def failing(self) -> bool:
        """Whether the last round did not land and its changes are being held
        for another try."""
        return self._retry.failing

    def _fence_open(self) -> bool:
        """Whether a round may leave — said once when the fence closes and
        once when it opens again, so a plane that has gone quiet is on the
        record, and so is its coming back."""
        if self.fence.expired():
            if self._fenced_since is None:
                self._fenced_since = self.clock.monotonic()
                logger.warning(
                    "live sync of %s paused: no heartbeat for %.0fs; %d change(s) wait "
                    "until one lands",
                    self._who,
                    self.fence.silent_for,
                    len(self._pending),
                    extra=self._log_ids,
                )
            return False
        if self._fenced_since is not None:
            logger.info(
                "live sync of %s resumed after %.0fs; sending %d withheld change(s)",
                self._who,
                self.clock.monotonic() - self._fenced_since,
                len(self._pending),
                extra=self._log_ids,
            )
            self._fenced_since = None
        return True

    def adopt(self, agreed: Mapping[str, PulledFile]) -> int:
        """Start out knowing which files already hold the drive's bytes.

        ``agreed`` is what a pull just proved, keyed by root-relative path:
        the node each file is, the etag and hash the drive holds it at, and
        the stamp the file wore when the pull was done. A file that still
        wears that stamp is taken as agreed — its node known, its bytes the
        drive's, its etag the base the next upload fences on — so the sweep
        at the top of the loop drops it instead of sending it back through
        the checkpoint push. One whose stamp moved since is left to the sweep
        like any other file. Called before :meth:`run`; answers how many
        were taken.
        """
        taken = 0
        for relative, pulled in agreed.items():
            if _stamp(self.root / relative) != pulled.stamp:
                continue
            self._known[relative] = (pulled.content_hash, pulled.size)
            self._stamps[relative] = pulled.stamp
            self._nodes[relative] = pulled.node_id
            if pulled.etag:
                self._etags[relative] = pulled.etag
            # Bytes the pull may have written: never named to the text peer
            # as what the agent's next save was made on (see _agreed_version).
            self._etag_digests.pop(relative, None)
            taken += 1
        return taken

    def seed(self) -> int:
        """Start out knowing every file an earlier process knew the node of.

        The node map is what the process before this one wrote down. A file
        still on disk under a path the map names is that node here too, so a
        rename the drive queued for it while the box was down is applied as a
        move of this file rather than a download beside it; one whose bytes
        are still the ones the two agreed on is agreed again, exactly as
        :meth:`adopt` agrees what a pull proved. A path the pull already
        adopted keeps what the pull said. Called before :meth:`run`, after
        :meth:`adopt`; answers how many files it took.
        """
        if self.node_map is None:
            return 0
        taken = restore_agreements(
            self.node_map.load(),
            root=self.root,
            hasher=self.hasher,
            stamp_of=_stamp,
            nodes=self._nodes,
            known=self._known,
            etags=self._etags,
            stamps=self._stamps,
            etag_hashes=self._etag_digests,
        )
        self._map_written = self._node_snapshot()
        return taken

    def _node_snapshot(self) -> dict[str, KnownNode]:
        """Which node each path is, with the bytes and etag last agreed on.

        Over snapshots of the maps: the drain thread adopts nodes while the
        flush thread renames them.
        """
        known = dict(self._known)
        etags = dict(self._etags)
        etag_digests = dict(self._etag_digests)
        snapshot: dict[str, KnownNode] = {}
        for relative, node_id in list(self._nodes.items()):
            agreed = known.get(relative)
            snapshot[relative] = KnownNode(
                node_id=node_id,
                content_hash=agreed[0] if agreed is not None else "",
                size=agreed[1] if agreed is not None else 0,
                etag=etags.get(relative, ""),
                etag_hash=etag_digests.get(relative, ""),
            )
        return snapshot

    def remember_nodes(self, *, now: bool = False) -> None:
        """Write the node map when what this holder knows has changed.

        No more often than the watcher's debounce: a burst of writes is one
        rewrite of the map, not one per file. ``now`` is never skipped: the
        sync's last word, and a checkpoint push about to read the map's bases.
        """
        if self.node_map is None:
            return
        with self._map_lock:
            snapshot = self._node_snapshot()
            if snapshot == self._map_written:
                return
            clock = self.clock.monotonic()
            spacing = self.cadence.debounce_ms / 1000.0
            if (
                not now
                and self._map_written_at is not None
                and clock - self._map_written_at < spacing
            ):
                return
            self.node_map.save(snapshot)
            self._map_written = snapshot
            self._map_written_at = clock

    # -- classification -------------------------------------------------

    def classify(self, change: Change, path: str) -> PendingChange | None:
        """Queue ``path`` for the next flush, or refuse it and answer ``None``.

        Containment is decided on the resolved path, so a path that leaves the
        root through a link is refused however it was reached. What the export
        leaves behind — a gitignored or preset-excluded path, this machine's
        local state, a pointer, a link — is refused here too, so neither queue
        ever carries it: the metadata queue would otherwise list a row for a
        file the checkpoint push will never send.
        """
        raw = Path(path)
        if raw.is_symlink():
            # A link is the host machine's arrangement of its own disk. The drive keeps files, and
            # following one would publish its target under a name that means something else here.
            return None
        base = self.root.resolve()
        candidate = raw.resolve()
        if not (candidate == base or base in candidate.parents):
            return None
        if change is not Change.deleted and candidate.is_dir():
            # A watcher naming a DIRECTORY is naming a burst it could not take
            # apart: macOS coalesces a run of changes into one event for the
            # folder they happened in and never says which file moved. A
            # directory carries no bytes, so dropping the event drops the
            # writes it stands for — the file lands on disk and the drive is
            # never told. Offer what is in it instead.
            if candidate == base or self._offer_directory(candidate):
                self._offer_entries(change, candidate)
            return None
        if candidate == base:
            return None
        relative = candidate.relative_to(base).as_posix()
        if not self._travels(relative, is_dir=False):
            return None
        if change is Change.deleted and candidate.is_file():
            # A removal the disk has already undone: a file moved aside and
            # replaced under its own name (an inbound conflict does exactly
            # that) reaches a watcher as a delete and an add, and a batch hands
            # the delete over last. What is on disk now is the answer.
            change = Change.modified
        if change is Change.deleted:
            pending = PendingChange(relative=relative, kind="delete", path=candidate)
            self._queue_meta(TreeEntry(op="delete", path=relative))
            # Nothing is left to send: the reader who asked for these bytes is
            # told by the delete, not by an upload that can never happen.
            self._first_seen.pop(relative, None)
            self._done_promoting(relative)
            self._intent_done(relative)
            self._landed(relative, landed=False)
        else:
            # A file written and removed again before the flush has nothing
            # left to stat, and a fifo or a socket is nothing a drive keeps.
            entry = self._file_entry(relative, candidate)
            if entry is None:
                return None
            pending = PendingChange(relative=relative, kind="write", path=candidate)
            self._queue_meta(entry)
            self._look_again(relative, (entry.size or 0, entry.mtime_ns or 0))
            if relative not in self._first_seen:
                self._seen_seq += 1
                self._first_seen[relative] = self._seen_seq
            if self._nodes.get(relative) in self._session_heads:
                # People have this file open as a live document, waiting on
                # the agent's edit: it goes ahead of the batch window.
                self.promote(relative)
        self._pending[relative] = pending
        return pending

    def _travels(self, relative: str, *, is_dir: bool) -> bool:
        """Whether ``relative`` is part of what this folder exports at all."""
        if self._refused_names.refused(relative, who=self._who):
            return False
        if not _tree_safe(relative) or relative.endswith(_INBOUND_SUFFIX):
            # Local state and pointers mean nothing on the drive; a name with the
            # inbound suffix is bytes still arriving FROM it, half-downloaded.
            return False
        rules = self._export_rules()
        encoded = relative.encode("utf-8", "surrogateescape")
        return rules.skip_reason(encoded, is_dir=is_dir) is None

    def _export_rules(self) -> ExportRules:
        """The checkpoint push's own rules for this root, read again whenever
        the root's ``.gitignore`` (or its ``.git``) comes or goes or changes."""
        stamp = (_stamp(self.root / ".gitignore"), (self.root / ".git").exists())
        if self._rules is None or stamp != self._rules_stamp:
            self._rules = ExportRules.load(
                self.root,
                respect_gitignore=self.respect_gitignore,
                exclude_presets=self.exclude_presets,
                skip_local_state=True,
            )
            self._rules_stamp = stamp
        return self._rules

    def _offer_directory(self, directory: Path) -> bool:
        """Queue a row for ``directory``; answer whether its contents travel.

        A folder with nothing in it yet is still a folder the reader should
        see. The drive makes a file's parents itself, so this only matters for
        an empty one — and costs nothing for a folder it already listed.
        """
        try:
            relative = directory.resolve().relative_to(self.root.resolve()).as_posix()
        except (OSError, ValueError):
            return False
        if relative in ("", "."):
            return True
        if not self._travels(relative, is_dir=True):
            return False
        self._queue_meta(TreeEntry(op="upsert", path=relative, kind="dir"))
        return True

    def _file_entry(self, relative: str, path: Path) -> TreeEntry | None:
        """The row for the regular file at ``path``, from one ``stat``.

        The digest rides along only when this holder already has it for the
        very bytes now on disk — the stamp it was taken at is the stamp the
        file still wears. The metadata queue never reads a file to get one.
        """
        try:
            info = path.stat()
        except OSError:
            return None
        if not stat_module.S_ISREG(info.st_mode):
            return None
        digest: str | None = None
        known = self._known.get(relative)
        if known is not None and self._stamps.get(relative) == (info.st_size, info.st_mtime_ns):
            digest = f"b3:{known[0]}"
        return TreeEntry(
            op="upsert",
            path=relative,
            kind="file",
            size=info.st_size,
            mtime_ns=info.st_mtime_ns,
            mode=stat_module.S_IMODE(info.st_mode),
            hash=digest,
        )

    def _offer_entries(self, change: Change, directory: Path, *, recursive: bool = False) -> None:
        """Queue the files in ``directory`` whose bytes the drive does not hold.

        A coalesced event says only where something happened, so every file
        beside the one that moved is offered too. The ones the drive already
        holds are taken back off the queue rather than sent: a round that
        reported them would show a reader rows going ``uploading`` for files
        nothing is uploading, and the round after would have nothing to say
        about them.

        ``recursive`` walks the subdirectories too, which is what the sweep at
        the top of the loop needs: a coalesced event names one folder, but the
        files already on disk when the watch arms can be at any depth.
        """
        try:
            entries = sorted(directory.iterdir())
        except OSError:
            return
        for entry in entries:
            if entry.is_dir():
                if entry.is_symlink() or not live_watch_filter(change, str(entry)):
                    continue
                if self._offer_directory(entry) and recursive:
                    self._offer_entries(change, entry, recursive=True)
                continue
            if not live_watch_filter(change, str(entry)):
                continue
            pending = self.classify(change, str(entry))
            if pending is None:
                continue
            known = self._known.get(pending.relative)
            if known is None or self._is_promoted(pending.relative):
                # A promoted file is owed whatever this holder believes the
                # drive holds: the drive, asking for it, says it does not.
                continue
            try:
                if self._already_agreed(pending.relative, pending.path, known):
                    self._pending.pop(pending.relative, None)
                    self._intent_done(pending.relative)
            except OSError:
                continue

    def _already_agreed(self, relative: str, path: Path, known: tuple[str, int]) -> bool:
        """Whether ``path`` still holds the bytes the drive was last given.

        The stat is asked first. A coalesced event names a FOLDER, so every
        file beside the one that moved is offered too — and on a folder of a
        thousand untouched files, hashing to learn what the stat already says
        reads the whole directory to send nothing. A file whose size and
        modification time are the ones its agreed digest was taken at cannot
        hold different bytes, so it is answered without a read.

        Only a file whose stamp moved is hashed, and the stamp is taken again
        afterwards: a file rewritten under the hash leaves no stamp behind, so
        the next offer reads it rather than trusting a stamp that describes
        bytes nobody hashed. A write that leaves both size and modification
        time exactly as they were is the one thing this cannot see, and it is
        the same blind spot every incremental sync on POSIX has.
        """
        before = _stamp(path)
        if before is not None and self._stamps.get(relative) == before:
            return True
        if self.hasher(path) != known:
            return False
        if before is not None and _stamp(path) == before:
            self._stamps[relative] = before
        return True

    def _look_again(self, relative: str, stamp: tuple[int, int]) -> None:
        """Keep an eye on ``relative`` for the watcher's blind second.

        Only a file modified inside the window is kept: one untouched for
        longer is outside the second the watcher cannot see into, and its next
        change is an event like any other — so the sweep of a large tree at
        start-up keeps nothing.
        """
        if self.recheck_window <= 0:
            return
        if self.wall_ns() - stamp[1] >= self.recheck_window * 1e9:
            self._recheck.pop(relative, None)
            return
        self._recheck[relative] = (stamp, self.clock.monotonic() + self.recheck_window)

    def _recheck_recent(self) -> None:
        """Offer again every file kept by :meth:`_look_again` whose size or
        modification time moved since it was seen, and let go of the ones
        whose window has passed.

        A stat each, never a read: the content round decides what the bytes
        are. A file that is gone is left to the watcher, which does see a
        removal, and whose delete carries what the drive must be told.
        """
        if not self._recheck:
            return
        now = self.clock.monotonic()
        for relative, (seen, until) in list(self._recheck.items()):
            path = self.root / relative
            current = _stamp(path)
            if current is not None and current != seen:
                del self._recheck[relative]
                self.classify(Change.modified, str(path))
            elif current is None or now >= until:
                del self._recheck[relative]

    # -- the loop -------------------------------------------------------

    async def run(self) -> None:
        """Consume the watcher and flush on the served cadence.

        There is no sleep here: the watcher is the only await, and a file held
        back to settle wakes it when due (:func:`wake_on`); a test drives it
        with a scripted stream and a clock it owns.

        The first batch sweeps the tree before it is classified. Nothing arms
        the OS watch until the stream is asked for its first batch, so a file
        written between the plane being started and that moment produces no
        event at all — and on a polling watch, which diffs against the snapshot
        it took when it armed, it never will. Sweeping once the watch is up
        closes the window from the other side: what was already on disk is
        offered here, and what arrives from now on is an event.
        """
        try:
            await self._run()
        finally:
            # No round will run again, whether the watch ended or the fence
            # refused: a reply still waiting on one is let go now rather than
            # at its timeout.
            with self._promote_lock:
                for landing in self._landings:
                    landing.done.set()
            if self.peer is not None:
                self.peer.close()

    async def _run(self) -> None:
        self._swept = False
        self._replay_staged_at_start()
        self.recover()  # what the journal says was promised goes before anything new is heard
        stream = await self.watcher.changes()
        async for batch in wake_on(stream, self._settle.due_in):
            if not self._swept:
                self._swept = True
                before = set(self._pending)
                self._offer_entries(Change.added, self.root, recursive=True)
                found = [
                    self._nodes[r] for r in self._pending if r not in before and r in self._nodes
                ]
                self._session_heads.update(found[:PEER_PROBES_AT_START])
            for change, path in sorted(batch, key=lambda item: (item[1], item[0].value)):
                self.classify(change, path)
            self._take_promotions()
            # Every batch, the empty tick included: a rewrite the watcher could
            # not see has no event to arrive with.
            self._recheck_recent()
            self._journal_intents()
            if self._reconcile_due():
                await self.reconcile()
            # The rows first, on their own clock: a burst is listed on the
            # drive before the round that hashes and uploads it has begun.
            if self._metadata_due():
                self._flush_metadata(hold=True)
            if self._flush_due():
                self._flush_contained()
        if self._drain_for is not None:
            self.drain(self._drain_for)
        else:
            self._flush_contained()
        self.remember_nodes(now=True)

    def _flush_contained(self) -> None:
        """One round that the loop survives.

        A round that did not land is not the end of the plane: what it carried
        is still on disk and still queued, and the drive that refused it — a
        502 between two deploys, a reset, a store out of room — is one the next
        round meets again. Said once per streak rather than per round, and
        tried again on a widening wait rather than twice a second. Only the
        fence's refusal ends the loop, because that is the one answer no wait
        changes: the folder is not ours.
        """
        try:
            self.flush()
        except LeaseSupersededError:
            raise
        except Exception as failure:
            if self._retry.failed(failure, self.clock.monotonic()):
                logger.warning(
                    "live sync of %s: a round did not land (%s: %s); %d change(s) kept "
                    "and offered again in %.1fs",
                    self._who,
                    type(failure).__name__,
                    failure,
                    len(self._pending),
                    self._retry.wait,
                    extra=self._log_ids,
                )

    def _flush_due(self) -> bool:
        if not self._retry.due(self.clock.monotonic()):
            return False
        if self._promotion_queued() or self._settle.take_due():
            # A reader is waiting on these bytes, or a file held back to settle
            # is due: the batch window is for coalescing writes nobody awaits.
            return True
        if len(self._pending) >= self.cadence.max_batch_entries:
            return True
        elapsed = (self.clock.monotonic() - self._last_flush) * 1000.0
        return elapsed >= self.cadence.batch_every_ms

    # -- the flush ------------------------------------------------------

    def flush(self) -> None:
        """Send one round: renames, the metadata queue, states, uploads.

        The order is the contract. A row exists on the drive before it is
        named in a batch, and it is named as ``uploading`` before its bytes
        move, so a reader never meets a state that refers to nothing.

        A round that raises leaves the queue as it was, whatever the failure:
        the work is still owed to the drive, and a caller that asks again —
        the loop after a wait, or whoever re-takes the lease — has to find it.
        """
        self._take_promotions()
        if not self._pending and not self._meta:
            return
        if not self._fence_open():
            # Too long since a beat: this holder can no longer prove the folder
            # is its own, so nothing leaves until one lands.
            return
        # Owed before a byte moves, so a restart mid-round still owes it.
        self._journal_intents()
        queued = dict(self._pending)
        self._in_round = frozenset(queued)
        try:
            self._flush_round(queued)
        except Exception:
            self._pending = queued | self._pending
            raise
        finally:
            self._in_round = frozenset()
        self._retry.landed()
        if queued:
            self._last_sent = self.clock.monotonic()
        self.remember_nodes()

    def keepalive(self) -> bool:
        """Tell the drive the plane is alive when nothing else has for a while.

        An empty batch: it lands no row and bumps nothing, and the drive stamps
        the lease as synced on it, which is what keeps a chat whose agent has
        written nothing for a minute from being shown to every reader as a
        holder that stopped syncing. Sent only once :data:`KEEPALIVE_EVERY` has
        passed since the last thing that left here, and never through a closed
        fence — a holder that cannot prove the folder is its own has no
        business saying it is fine. Answers whether one went out. Called from
        the beat, on the beat's thread: it touches nothing a flush owns.
        """
        if self.fence.expired():
            return False
        now = self.clock.monotonic()
        if now - self._last_sent < KEEPALIVE_EVERY:
            return False
        with _fenced():
            self.api.live_batch([])
        self._last_sent = now
        return True

    def _flush_round(self, queued: Mapping[str, PendingChange]) -> None:
        self._pending = {}
        self._last_flush = self.clock.monotonic()
        writes = [item for item in queued.values() if item.kind == "write"]
        deletes = [item for item in queued.values() if item.kind == "delete"]

        # A delete is the metadata queue's to send; the content round only asks
        # whether it and a write of the same bytes are one move.
        renames, writes, _unpaired = self._detect_renames(writes, deletes)
        for removed, added in renames:
            self._queue_rename(removed.relative, added)
            # The bytes are already on the drive, under the node that moves.
            self._done_promoting(added.relative)
            self._landed(added.relative, landed=True)
            self._first_seen.pop(added.relative, None)
            self._intent_done(added.relative)
        # Everything the rows owe goes now, what was held for this round too:
        # the bytes below land on rows that already exist.
        self._flush_metadata(hold=False)

        writes = [item for item in writes if not self._peer_took_write(item)]
        uploads, on_box, deferred = self._partition(writes)
        # A file that is only waiting for the window is still reported now:
        # the reader is told a file is on its way rather than shown nothing.
        self._resolve([item.relative for item in [*uploads, *on_box, *deferred]])
        self._report(uploads, on_box, deferred)
        self._upload(uploads)

    def _detect_renames(
        self, writes: list[PendingChange], deletes: list[PendingChange]
    ) -> tuple[list[tuple[PendingChange, PendingChange]], list[PendingChange], list[PendingChange]]:
        """A delete and a write of identical bytes in one round are a rename.

        Identity is the last content this holder pushed for the deleted path,
        so the pair is believed only when the drive really holds those bytes
        under the old name — a coincidence of size is not enough, and a
        temporary file whose bytes never reached the drive cannot be renamed
        at all.
        """
        pairs: list[tuple[PendingChange, PendingChange]] = []
        moved_to: set[str] = set()
        moved_from: set[str] = set()
        for removed in sorted(deletes, key=lambda item: item.relative):
            before = self._known.get(removed.relative)
            if before is None:
                continue
            for added in sorted(writes, key=lambda item: item.relative):
                if added.relative in moved_to:
                    continue
                if self._size_of(added) != before[1]:
                    continue
                if not self._settled(added):
                    # Still being written: its bytes are not read yet, so it
                    # cannot be proven the same file under a new name.
                    continue
                if self.hasher(added.path)[0] != before[0]:
                    continue
                pairs.append((removed, added))
                moved_to.add(added.relative)
                moved_from.add(removed.relative)
                break
        return (
            pairs,
            [item for item in writes if item.relative not in moved_to],
            [item for item in deletes if item.relative not in moved_from],
        )

    def _peer_took_write(self, item: PendingChange) -> bool:
        """Whether the text peer took ``item`` and the drive will hold it: sent once settled."""
        node = self._nodes.get(item.relative)
        if self.peer is None or node is None:
            return False
        if not self.peer.owns(node) and node not in self._session_heads:
            return False
        if not self._settled(item):
            self._pending.setdefault(item.relative, item)
            return True
        taken = self.peer.take(node, item.relative)
        if taken is Taken.LEFT:
            self._session_heads.discard(node)  # no live session (or text) there now
        if taken is not Taken.LANDED:  # nor one the drive holds: the ordinary upload
            return False
        self._first_seen.pop(item.relative, None)
        self._done_promoting(item.relative)
        self._intent_done(item.relative)
        self._landed(item.relative, landed=True)
        return True

    def _peer_took_inbound(self, node_id: str, relative: str, final: Path) -> bool:
        """Whether the text peer took a write back of ``node_id`` (it holds
        the file, or joins it now); the disk then holds the document."""
        assert self.peer is not None
        if self.peer.owns(node_id):
            taken = self.peer.refresh(node_id)
        else:
            taken = node_id in self._session_heads and self.peer.join(node_id, relative)
        described = self._inbound_hashes.pop(node_id, None)
        if taken and described is not None and self.hasher(final)[0] == described:
            self._adopt(relative, node_id, final)
        return taken

    def _partition(
        self, writes: Sequence[PendingChange]
    ) -> tuple[list[PendingChange], list[PendingChange], list[PendingChange]]:
        """Split the writes into what is uploaded, what stays on the box and
        what waits for the bandwidth window to open again.

        The writes are taken in the content queue's order (:meth:`_priority`),
        and that order is the order the uploads go in. A file still being
        written is none of the three: it stays queued, unread, until it has
        settled. A promoted file that does not fit in the window goes on the
        promotion burst instead, as far as the burst reaches.
        """
        uploads: list[PendingChange] = []
        on_box: list[PendingChange] = []
        deferred: list[PendingChange] = []
        self._bursting = set()
        budget = self._window_left()
        burst = self._burst_left()
        for item in sorted(writes, key=self._priority):
            size = self._size_of(item)
            if size > self.cadence.max_file_bytes:
                on_box.append(item)
                self._first_seen.pop(item.relative, None)
                self._done_promoting(item.relative)
                self._intent_done(item.relative)
                self._landed(item.relative, landed=False)
                continue
            if not self._is_urgent(item.relative) and not self._settled(item):
                # Still being written. Reading it now proves only that the read
                # raced the writer; it waits, and goes once it is still. A file
                # the transcript is about to name is finished — the agent is
                # writing about it — so it is read now, and the stamp check
                # around the hash still refuses bytes that move under it.
                self._pending.setdefault(item.relative, item)
                continue
            if size <= budget:
                budget -= size
                uploads.append(item)
                continue
            if self._is_promoted(item.relative) and size <= burst:
                burst -= size
                self._bursting.add(item.relative)
                uploads.append(item)
                continue
            deferred.append(item)
            # Still owed to the drive, so it goes back on the queue rather
            # than being dropped on the floor.
            self._pending.setdefault(item.relative, item)
            self.on_deferred(item.relative)
        return uploads, on_box, deferred

    def _priority(self, item: PendingChange) -> tuple[int, int, int]:
        """Where ``item`` stands in the content queue: promoted first, in the
        order they were asked for; then small files in the order they were
        first seen; then the rest smallest first; packed history last."""
        seen = self._first_seen.get(item.relative, 0)
        with self._promote_lock:
            asked = self._promoted.get(item.relative)
        if asked is not None:
            return (0, asked, 0)
        size = self._size_of(item)
        if _is_cold(item.relative):
            return (3, size, seen)
        if size <= SMALL_FILE_BYTES:
            return (1, seen, 0)
        return (2, size, seen)

    def _settled(self, item: PendingChange) -> bool:
        """Whether ``item`` may be read (:mod:`.settle`): on the served settle
        time, or the short one when a co-edited document is open on it."""
        live = self._nodes.get(item.relative) in self._session_heads
        settle_ms = min(self.cadence.settle_ms, LIVE_SETTLE_MS) if live else self.cadence.settle_ms
        return self._settle.settled(item.relative, _stamp(item.path), settle_ms=settle_ms)

    # -- promotion ------------------------------------------------------

    def promote(self, relative: str) -> None:
        """Put ``relative`` at the front of the content queue.

        A reader on the drive is waiting on these bytes. Safe to call from any
        thread: the path is only noted here, and taken onto the queue by the
        flush, on the sync's own thread — where it is classified like every
        other change, so a path that is not inside the root, not exported, or
        no longer a file is dropped by the same refusals a watcher event
        meets. Nothing is read or hashed on the caller's thread.
        """
        with self._promote_lock:
            if relative in self._promoted or relative in self._promote_inbox:
                return
            self._promote_inbox.append(relative)

    @property
    def promotions(self) -> frozenset[str]:
        """The paths promoted and not yet uploaded (or dropped)."""
        with self._promote_lock:
            return frozenset(self._promote_inbox) | frozenset(self._promoted)

    def _take_promotions(self) -> None:
        """Take what :meth:`promote` noted onto the queue, on this thread."""
        with self._promote_lock:
            taken, self._promote_inbox = self._promote_inbox, []
        for relative in taken:
            if self._is_urgent(relative) and self._on_drive(relative):
                # Named by the transcript and already the drive's bytes: there
                # is nothing to send, and the reply goes now.
                with self._promote_lock:
                    self._urgent.discard(relative)
                self._landed(relative, landed=True)
                continue
            pending = self.classify(Change.modified, str(self.root / relative))
            with self._promote_lock:
                refused = pending is None or pending.kind != "write"
                if refused:
                    self._urgent.discard(relative)
                else:
                    self._promote_seq += 1
                    self._promoted[relative] = self._promote_seq
            if refused:
                # Outside the root, not exported, gone, or not a file: no bytes
                # will ever land for it, and its landing is told so now.
                self._landed(relative, landed=False)

    def _promotion_queued(self) -> bool:
        with self._promote_lock:
            if self._promote_inbox:
                return True
            promoted = list(self._promoted)
        return any(relative in self._pending for relative in promoted)

    def _is_promoted(self, relative: str) -> bool:
        with self._promote_lock:
            return relative in self._promoted

    def _done_promoting(self, relative: str) -> None:
        with self._promote_lock:
            self._promoted.pop(relative, None)
            self._urgent.discard(relative)

    # -- landing --------------------------------------------------------

    def land(self, relatives: Iterable[str], *, timeout: float) -> frozenset[str]:
        """Put these files' bytes on the drive before answering, within ``timeout``.

        What the transcript calls before it publishes a reply that names files
        in this folder: a reader shown the reference must be able to open it.
        Each path is promoted and read without waiting out the settle time —
        the agent has finished a file it is writing about — and this blocks
        until every one of them has landed, been refused (outside the root,
        not exported, gone, larger than the drive takes), or ``timeout``
        passes. A file already holding the drive's bytes answers at once and
        is not sent again. A file larger than :data:`LAND_WAIT_BYTES` is
        promoted but not waited for.

        Answers the paths whose bytes are on the drive. Safe to call from any
        thread but the sync's own, which is the one that does the sending.
        """
        already: set[str] = set()
        asked: dict[str, int] = {}
        for relative in relatives:
            clean = relative.strip("/")
            if not clean or clean in asked or clean in already:
                continue
            raw = self.root / clean
            base = self.root.resolve()
            if raw.is_symlink() or base not in raw.resolve().parents:
                # Not a file of this folder's, whatever the reply says: the
                # round would refuse it, so nothing waits for the round to.
                continue
            try:
                facts = raw.stat()
            except OSError:
                # Nothing on disk by that name: a word in code that only looked
                # like a path, or a file the agent named and never wrote. There
                # are no bytes to wait for.
                continue
            if not stat_module.S_ISREG(facts.st_mode):
                continue
            if self._on_drive(clean):
                # Sent a moment ago, or long since and untouched: the reply
                # need not wait for the sync's next round to be told so.
                already.add(clean)
                continue
            asked[clean] = facts.st_size
        landing = _Landing(
            waiting={path for path, size in asked.items() if size <= LAND_WAIT_BYTES}
        )

        with self._promote_lock:
            # Registered before the paths reach the inbox, so a round that
            # lands one the instant it is asked for cannot land it unheard.
            if landing.waiting:
                self._landings.append(landing)
            for clean in asked:
                self._urgent.add(clean)
                if clean not in self._promoted and clean not in self._promote_inbox:
                    self._promote_inbox.append(clean)
        if landing.waiting:
            try:
                landing.done.wait(timeout)
            finally:
                with self._promote_lock:
                    if landing in self._landings:
                        self._landings.remove(landing)
        with self._promote_lock:
            return frozenset(already | landing.landed)

    def _landed(self, relative: str, *, landed: bool) -> None:
        """Tell every landing waiting on ``relative`` how it came out."""
        with self._promote_lock:
            for landing in self._landings:
                if relative not in landing.waiting:
                    continue
                landing.waiting.discard(relative)
                if landed:
                    landing.landed.add(relative)
                if not landing.waiting:
                    landing.done.set()

    def _is_urgent(self, relative: str) -> bool:
        with self._promote_lock:
            return relative in self._urgent

    def _on_drive(self, relative: str) -> bool:
        """Whether the drive holds the bytes on disk at ``relative`` now: this
        holder sent (or pulled) them, and the file still wears the stamp it had
        then, with nothing queued for it since."""
        if relative in self._pending or relative not in self._known:
            return False
        agreed = self._stamps.get(relative)
        return agreed is not None and agreed == _stamp(self.root / relative)

    def _size_of(self, item: PendingChange) -> int:
        try:
            return item.path.stat().st_size
        except OSError:
            return 0

    def _window_left(self) -> int:
        now = self.clock.monotonic()
        self._spent = [entry for entry in self._spent if now - entry[0] < 60.0]
        return max(self.cadence.bandwidth_bytes_per_minute - sum(e[1] for e in self._spent), 0)

    def _burst_left(self) -> int:
        """What promotions may still move past the window this minute."""
        now = self.clock.monotonic()
        self._burst_spent = [entry for entry in self._burst_spent if now - entry[0] < 60.0]
        spent = sum(entry[1] for entry in self._burst_spent)
        return max(self.cadence.live_promote_burst_bytes - spent, 0)

    def _resolve(self, paths: Sequence[str]) -> None:
        """Learn the node id filed at each path, once per path.

        Nothing is made here: the rows exist because the metadata queue said
        so. A path the drive does not list yet — its row refused, or still
        waiting on a throttled batch — simply stays unknown until it does.
        """
        wanted = sorted({path for path in paths if path not in self._nodes})
        if not wanted:
            return
        with _fenced():
            answer = self.api.resolve(wanted)
        asked = set(wanted)
        for path, node_id in answer.items():
            if path in asked and node_id:
                self._nodes[path] = node_id

    def _report(
        self,
        uploads: Sequence[PendingChange],
        on_box: Sequence[PendingChange],
        deferred: Sequence[PendingChange],
    ) -> None:
        groups: tuple[tuple[Sequence[PendingChange], LiveState], ...] = (
            (uploads, "uploading"),
            (on_box, "on_box"),
            (deferred, "deferred"),
        )
        entries: list[LiveEntry] = []
        for group, state in groups:
            for item in group:
                entry = self._entry(item, state)
                if entry.node_id:
                    # A path the drive does not list yet cannot be reported:
                    # the server would have nothing to hang the state on.
                    entries.append(entry)
        self._batch(entries)

    def _batch(self, entries: Sequence[LiveEntry]) -> list[LiveEntry]:
        """Tell the server about these nodes, and answer what it took.

        Chunked at the ceiling the grant named, never above it: one request
        past the cap is refused whole, and for the inbound direction those rows
        are cleared by nothing else — a drain larger than the cap would be
        re-offered, re-downloaded and never settled for the life of the lease.

        A throttled server is waited out rather than raised on. The ceiling
        that answers 429 is shared with every box behind this address, so
        letting one refusal out of here would end an unrelated chat's live
        plane for good; what is not accepted is simply still owed.
        """
        cap = max(self.cadence.max_batch_entries, 1)
        taken: list[LiveEntry] = []
        for start in range(0, len(entries), cap):
            chunk = list(entries[start : start + cap])
            if not self._post(chunk):
                break
            taken.extend(chunk)
        return taken

    def _post(self, chunk: Sequence[LiveEntry]) -> bool:
        """One batch, offered again while the server is only saying "not now"."""
        for attempt in range(THROTTLE_ATTEMPTS):
            try:
                with _fenced():
                    self.api.live_batch(chunk)
            except LeaseSupersededError:
                raise
            except Exception as refusal:
                if conflict_code(refusal) == TOO_MANY_CODE:
                    # The drive will not hold another row for this lease right
                    # now. The rows are deferred, not the bytes: every upload
                    # in this round still lands, and each one that does clears
                    # a row on the drive, so the next round has room. Ending
                    # the plane here would be the one outcome that never drains.
                    logger.warning(
                        "live sync of %s: a live batch of %d entries was refused: the lease "
                        "has too many nodes in flight; the states wait, the bytes still go",
                        self._who,
                        len(chunk),
                        extra=self._log_ids,
                    )
                    return False
                wait = throttle_wait(refusal, attempt)
                if wait is None:
                    raise
                if attempt == THROTTLE_ATTEMPTS - 1:
                    # The last try is spent. Waiting now would only be time the
                    # box spends not watching its folder.
                    break
                logger.info(
                    "live sync of %s: a live batch of %d was throttled; offering it again in %.1fs",
                    self._who,
                    len(chunk),
                    wait,
                    extra=self._log_ids,
                )
                self.sleep(wait)
                continue
            self._last_sent = self.clock.monotonic()
            return True
        logger.warning(
            "live sync of %s: a live batch of %d entries is still being throttled; the "
            "server keeps them",
            self._who,
            len(chunk),
            extra=self._log_ids,
        )
        return False

    def _entry(self, item: PendingChange, state: LiveState) -> LiveEntry:
        node_id = self._nodes.get(item.relative, "")
        try:
            stat = item.path.stat()
        except OSError:
            return LiveEntry(node_id=node_id, state=state)
        return LiveEntry(
            node_id=node_id,
            state=state,
            box_size=stat.st_size,
            box_mtime=datetime.fromtimestamp(stat.st_mtime).astimezone(),
        )

    def _upload(self, uploads: Sequence[PendingChange]) -> None:
        # In the order the partition decided: the queue's order is the point.
        for item in uploads:
            promoted = self._is_promoted(item.relative)
            # The node the tree batch made addressable. A path the drive does
            # not list yet is still sent: the upload files it by path, and a
            # row the metadata queue could not land is no reason to hold bytes.
            node_id = self._nodes.get(item.relative, "")
            before = _stamp(item.path)
            digest, size = self.hasher(item.path)
            if before is None or _stamp(item.path) != before:
                # The file moved under the hash. Sending these bytes would
                # publish a half-written file as a finished version, so the
                # path goes back on the queue and a later flush sends whatever
                # the writer ended up with — still promoted, if it was.
                self._pending.setdefault(item.relative, item)
                continue
            if not promoted and self._known.get(item.relative) == (digest, size):
                self._first_seen.pop(item.relative, None)
                self._intent_done(item.relative)
                continue
            with _fenced():
                filed = self.api.upload(
                    item.relative, node_id, size, base=self._agreed_base(item.relative)
                )
            self._intent_done(item.relative)
            self._known[item.relative] = (digest, size)
            if filed is not None:
                self._confirm_etag(item.relative, filed)
            # Unconfirmed, the older etag stays: while the head holds these
            # bytes the next upload fences on the head anyway, and once it holds
            # anything else the older etag is still behind it.
            # The stamp the hash above was taken at, proven unmoved across it.
            self._stamps[item.relative] = before
            spent = self._burst_spent if item.relative in self._bursting else self._spent
            spent.append((self.clock.monotonic(), size))
            self._first_seen.pop(item.relative, None)
            self._done_promoting(item.relative)
            self._landed(item.relative, landed=True)
            # The row learns the digest these bytes were proven to have, so the
            # drive compares content rather than a size and a timestamp.
            proven = self._file_entry(item.relative, item.path)
            if proven is not None and proven.hash is not None:
                self._queue_meta(proven)

    def _confirm_etag(self, relative: str, etag: str) -> None:
        """The drive filed the bytes this holder uploaded (:attr:`_known`'s)
        for ``relative`` at ``etag``."""
        self._etags[relative] = etag
        known = self._known.get(relative)
        if known is None:
            self._etag_digests.pop(relative, None)
        else:
            self._etag_digests[relative] = known[0]

    def _agreed_version(self, relative: str) -> int | None:
        """The drive etag of the version ``relative``'s disk was made on, for
        the text peer to name: the bytes this holder read off its disk and
        uploaded, which the drive filed. Every later save the agent makes is
        made on them until something else writes the file.

        ``None`` when the drive never confirmed those bytes (an upload it did
        not answer for moved them past the etag), when the text peer wrote or
        sent the file since, when bytes were written over it (a write back
        taken inbound, a take's pull: an agent that read the file before and
        saved after made its save on the bytes before, and named as made on
        the new ones, every line they brought would read as removed), or
        when the etag is not a version number. The text peer then sends the
        save naming no version, and nothing is removed."""
        etag = self._etags.get(relative)
        known = self._known.get(relative)
        if etag is None or known is None or self._etag_digests.get(relative) != known[0]:
            return None
        return int(etag) if etag.isdigit() else None

    def _descended(self, relative: str) -> None:
        """The text peer is about to write or send ``relative``: its disk will descend from a
        document state, which the agreed drive version does not hold. Written down before it does,
        so a restart cannot name that version for the file either: merged from it, every line the
        peer had sent since would land a second time."""
        if self._etag_digests.pop(relative, None) is not None:
            self.remember_nodes(now=True)

    def _agreed_base(self, relative: str) -> AgreedBase | None:
        """The base the next upload of ``relative`` is fenced on, if known.

        The agreed bytes and the etag the drive held them at, both. Missing
        either, the upload fences on whatever it reads -- a file this holder
        never agreed on has no older base to name.
        """
        known = self._known.get(relative)
        etag = self._etags.get(relative)
        if known is None or etag is None:
            return None
        return AgreedBase(etag=etag, content_hash=known[0])

    # -- the metadata queue ---------------------------------------------

    def _queue_meta(self, entry: TreeEntry, *, force: bool = False) -> None:
        """Coalesce ``entry`` into the metadata queue under its path.

        The latest word on a path wins: a delete after an upsert is a delete,
        an upsert after a delete is an upsert, and a file changed three times
        is one entry carrying the last stat. A queued move stays a move — a
        fresh stat of its destination updates it — until either end changes
        under it: its source naming a new file turns it back into a plain
        upsert of the destination, and its destination vanishing deletes both.

        An upsert saying exactly what the drive last accepted for a path that
        has nothing queued is dropped here, so a coalesced folder event that
        re-offers a thousand untouched files sends nothing — unless ``force``:
        the walk queues a directory whose digest the drive disagrees with,
        and what this holder believes it last sent is exactly what is in doubt.
        """
        path = entry.path
        destination = self._renames_from.pop(path, None)
        if destination is not None:
            moving = self._meta.get(destination)
            if moving is not None and moving.op == "rename":
                self._meta[destination] = replace(moving, op="upsert", from_=None)
        current = self._meta.get(path)
        if current is not None and current.op == "rename" and current.from_ is not None:
            if entry.op == "delete":
                self._renames_from.pop(current.from_, None)
                self._meta.setdefault(current.from_, TreeEntry(op="delete", path=current.from_))
            else:
                entry = replace(entry, op="rename", from_=current.from_)
        elif (
            not force
            and current is None
            and entry.op != "delete"
            and self._sent_meta.get(path) == entry.fingerprint()
        ):
            return
        if not self._meta:
            self._meta_since = self.clock.monotonic()
        self._meta[path] = entry

    def _queue_rename(self, source: str, added: PendingChange) -> None:
        """Queue ``source`` → ``added`` as one move, replacing its delete."""
        moved = self._file_entry(added.relative, added.path)
        if moved is None:
            # Gone again before the move could be said; the watcher's delete
            # for it is on its way and the source's delete still stands.
            return
        known = self._known.get(source)
        self._meta.pop(source, None)
        if not self._meta:
            self._meta_since = self.clock.monotonic()
        self._meta[added.relative] = replace(
            moved,
            op="rename",
            from_=source,
            hash=f"b3:{known[0]}" if known is not None else moved.hash,
        )
        self._renames_from[source] = added.relative

    def _metadata_due(self) -> bool:
        """Whether the metadata queue goes now: full, or its window has passed."""
        if not self._meta:
            return False
        now = self.clock.monotonic()
        if now < self._meta_retry_at:
            return False
        if len(self._meta) >= max(self.cadence.metadata_max_entries, 1):
            return True
        since = self._meta_since if self._meta_since is not None else now
        return (now - since) * 1000.0 >= self.cadence.metadata_every_ms

    def _rename_candidates(self) -> set[str]:
        """The queued entries the content round must see before they are sent.

        A delete of a file whose bytes the drive holds, queued beside a new
        file of the same size, may be a move — and only the content round can
        tell, because only it reads bytes. Sent as they stand, the pair would
        trash the node that carries the file's history and list a fresh row
        under the new name; held until that round, they go as one rename.
        Nothing is held that the content round is not about to look at.
        """
        sizes: dict[int, list[str]] = {}
        for path, entry in self._meta.items():
            known = self._known.get(path)
            waiting = self._pending.get(path)
            if entry.op != "delete" or known is None or waiting is None:
                continue
            if waiting.kind == "delete":
                sizes.setdefault(known[1], []).append(path)
        if not sizes:
            return set()
        held: set[str] = set()
        for path, entry in self._meta.items():
            if entry.op != "upsert" or entry.kind != "file" or entry.size not in sizes:
                continue
            waiting = self._pending.get(path)
            if waiting is None or waiting.kind != "write":
                continue
            held.add(path)
            held.update(sizes[entry.size])
        return held

    def _flush_metadata(self, *, hold: bool) -> None:
        """Send the metadata queue as ``tree`` batches; raise only when fenced.

        Everything short of the fence is this queue's own business and never
        the content round's: a throttle is waited out on the clock while the
        queue keeps coalescing, a lease with too many rows pauses the rows and
        not the bytes, and any other refusal backs off and offers the same
        rows again. Nothing that was queued is lost on the way — least of all
        a delete, which no later refusal can turn into a file left standing.

        ``hold`` keeps back the pairs only the content round can tell apart
        from a move (see :meth:`_rename_candidates`).
        """
        if not self._meta:
            return
        now = self.clock.monotonic()
        if now < self._meta_retry_at:
            return
        if not self._fence_open():
            return
        held = self._rename_candidates() if hold else set()
        ready = [entry for path, entry in self._meta.items() if path not in held]
        if not ready:
            return
        for entry in ready:
            del self._meta[entry.path]
        self._meta_since = now if self._meta else None
        settled: set[str] = set()
        cap = max(self.cadence.metadata_max_entries, 1)
        try:
            for start in range(0, len(ready), cap):
                self._send_tree(ready[start : start + cap], settled)
        except BaseException as refusal:
            self._requeue([entry for entry in ready if entry.path not in settled])
            if isinstance(refusal, LeaseSupersededError) or not isinstance(refusal, Exception):
                raise
            self._meta_backoff(refusal)
            return
        self._meta_failing = False
        self._meta_wait = 0.0
        self._meta_attempts = 0
        self._meta_retry_at = 0.0

    def _requeue(self, entries: Sequence[TreeEntry]) -> None:
        """Put back what did not land. Anything queued since is newer and wins."""
        for entry in entries:
            self._meta.setdefault(entry.path, entry)
        if self._meta and self._meta_since is None:
            self._meta_since = self.clock.monotonic()

    def _meta_backoff(self, refusal: Exception) -> None:
        """Hold the metadata queue back after a refusal that is not the fence.

        A throttle names its own wait, obeyed up to :data:`THROTTLE_MAX_WAIT`;
        anything else — the lease's row ceiling, a drive between deploys —
        waits on a doubling clock. The rows are what wait: the loop goes on
        classifying into the queue, and the bytes go on landing.
        """
        throttled = throttle_wait(refusal, self._meta_attempts)
        self._meta_attempts += 1
        if throttled is not None:
            wait = throttled
        else:
            wait = doubled(self._meta_wait, floor=RETRY_FIRST_WAIT, cap=RETRY_MAX_WAIT)
        self._meta_wait = wait
        self._meta_retry_at = self.clock.monotonic() + wait
        if self._meta_failing:
            return
        self._meta_failing = True
        if conflict_code(refusal) == TOO_MANY_CODE:
            reason = "the lease lists as many rows as the drive will hold; the bytes still go"
        elif throttled is not None:
            reason = "the drive asked for a pause"
        else:
            reason = f"{type(refusal).__name__}: {refusal}"
        logger.warning(
            "live sync of %s: the folder's rows did not land (%s); %d kept and offered "
            "again in %.1fs",
            self._who,
            reason,
            len(self._meta),
            wait,
            extra=self._log_ids,
        )

    def _post_tree(self, chunk: Sequence[TreeEntry]) -> TreeAnswer:
        """One ``tree`` call under a fresh batch id, journaled before it leaves.

        A refusal forgets the row: what it carried is either queued again, to
        leave under a batch id of its own, or left off the drive on purpose.
        Anything that is not a refusal — the process dying under the call —
        leaves the row unacked, and the next start resends it as it was.
        """
        batch_id = str(uuid.uuid4())
        if self.journal is not None:
            self.journal.record_batch(batch_id, [entry.to_journal() for entry in chunk])
        try:
            with _fenced():
                answer = self.api.tree(batch_id, chunk, gzip_above=self.cadence.metadata_gzip_bytes)
        except Exception:
            if self.journal is not None:
                self.journal.forget_batch(batch_id)
            raise
        if self.journal is not None:
            self.journal.ack_batch(batch_id, answer.live_seq)
        return answer

    def _send_tree(self, chunk: Sequence[TreeEntry], settled: set[str]) -> None:
        """Land ``chunk``, splitting it where the route asks for less
        (:class:`.live_tree_send.TreeSend`)."""
        TreeSend(
            post=self._post_tree,
            landed=self._landed_tree,
            who=self._who,
            log_ids=self._log_ids,
            logger=logger,
        ).send(chunk, settled)

    def _landed_tree(
        self, chunk: Sequence[TreeEntry], answer: TreeAnswer, settled: set[str]
    ) -> None:
        """Agree with the drive on what it now lists."""
        self._last_sent = self.clock.monotonic()
        if chunk:
            # Something moved: the walk goes back to looking every minute.
            self._reconcile_interval = self.reconcile_every
        for entry in chunk:
            settled.add(entry.path)
            if entry.op == "delete":
                self._forget(entry.path)
                # The checkpoint push must not put back what the box deleted.
                self.tombstones.add(entry.path)
                continue
            if entry.op == "rename" and entry.from_ is not None:
                self._renames_from.pop(entry.from_, None)
                self._move_known(entry.from_, entry.path)
            self._sent_meta[entry.path] = entry.fingerprint()
            self._revive(entry.path)

    def _forget(self, relative: str) -> None:
        """Drop what this holder knew about a path the drive no longer lists —
        everything under it too, when it was a folder the drive listed."""
        was_folder = self._sent_meta.get(relative, (None,))[0] == "dir"
        maps: list[dict[str, Any]] = [
            self._nodes,
            self._known,
            self._etags,
            self._stamps,
            self._sent_meta,
        ]
        prefix = f"{relative}/"
        for known in maps:
            known.pop(relative, None)
            if was_folder:
                # Over a snapshot: the drain thread adopts nodes into these
                # maps while the flush thread walks them here.
                for inside in [path for path in list(known) if path.startswith(prefix)]:
                    known.pop(inside, None)

    def _move_known(self, source: str, destination: str) -> None:
        """A rename moves the name, never the bytes or their mtime."""
        node_id = self._nodes.pop(source, None)
        if node_id is not None:
            self._nodes[destination] = node_id
        known = self._known.pop(source, None)
        if known is not None:
            self._known[destination] = known
        etag = self._etags.pop(source, None)
        if etag is not None:
            self._etags[destination] = etag
        digest = self._etag_digests.pop(source, None)
        if digest is not None:
            self._etag_digests[destination] = digest
        stamp = self._stamps.pop(source, None)
        if stamp is not None:
            self._stamps[destination] = stamp
        self._sent_meta.pop(source, None)

    def _forget_exact(self, relative: str) -> None:
        """Drop what this holder knew about exactly ``relative``."""
        for known in (self._nodes, self._known, self._etags, self._stamps, self._sent_meta):
            known.pop(relative, None)

    def _revive(self, relative: str) -> None:
        """A path the drive lists again is no longer one the box deleted — nor
        is any folder above it."""
        parts = relative.split("/")
        for depth in range(1, len(parts) + 1):
            self.tombstones.discard("/".join(parts[:depth]))

    # -- the journal ----------------------------------------------------

    def recover(self) -> None:
        """Keep what the journal says was promised, before anything new.

        Unacked batches are resent first, each under its own ``batch_id`` —
        the route applies a batch id once, so a batch that did land before
        the restart lands nothing twice. Then every file still owed goes back
        on the content queue, classified like any watcher event, so a file
        that has since gone or left the export is simply let go. Settled rows
        a day old are pruned here and nowhere else.

        A batch the drive refuses now is not resent again: its rows go back
        on the metadata queue, to leave under a new id on the normal cadence.
        Through a closed fence nothing is sent at all, which is the third
        refusal: the rows wait in memory for a beat.
        """
        journal = self.journal
        if journal is None:
            return
        journal.prune()
        for batch in journal.unacked():
            entries = [TreeEntry.from_journal(row) for row in batch.entries]
            if not self._fence_open():
                journal.forget_batch(batch.batch_id)
                self._requeue(entries)
                continue
            try:
                with _fenced():
                    answer = self.api.tree(
                        batch.batch_id, entries, gzip_above=self.cadence.metadata_gzip_bytes
                    )
            except LeaseSupersededError:
                raise
            except Exception as refusal:
                logger.warning(
                    "live sync of %s: journaled batch %s was refused on resend (%s: %s); its "
                    "%d row(s) go back on the queue",
                    self._who,
                    batch.batch_id,
                    type(refusal).__name__,
                    refusal,
                    len(entries),
                    extra=self._log_ids,
                )
                journal.forget_batch(batch.batch_id)
                self._requeue(entries)
                continue
            journal.ack_batch(batch.batch_id, answer.live_seq)
            self._landed_tree(entries, answer, set())
        for intent in journal.queued_intents():
            pending = self.classify(Change.modified, str(self.root / intent.relative))
            if pending is None or pending.kind != "write":
                journal.done_intent(intent.relative)
                continue
            self._journaled.add(intent.relative)

    def _journal_intents(self) -> None:
        """Note every queued write the journal does not hold yet, in one go."""
        journal = self.journal
        if journal is None:
            return
        fresh: list[tuple[str, int, int, str | None]] = []
        for relative, item in list(self._pending.items()):
            if item.kind != "write" or relative in self._journaled:
                continue
            stamp = _stamp(item.path)
            if stamp is None:
                continue
            fresh.append((relative, stamp[0], stamp[1], self._nodes.get(relative)))
        if journal.queue_intents(fresh):
            self._journaled.update(relative for relative, *_ in fresh)

    def _intent_done(self, relative: str) -> None:
        if self.journal is None or relative not in self._journaled:
            return
        self._journaled.discard(relative)
        self.journal.done_intent(relative)

    # -- the walk -------------------------------------------------------

    def note_overflow(self) -> None:
        """The watcher said it dropped events: look again on the short interval."""
        self._reconcile_interval = self.reconcile_every

    @property
    def reconcile_interval(self) -> float:
        """How long after the last walk the next one is due."""
        return self._reconcile_interval

    def _reconcile_due(self) -> bool:
        if self.reconciling:
            return False
        return self.clock.monotonic() - self._last_reconcile >= self._reconcile_interval

    async def reconcile(self) -> bool:
        """Walk the folder, compare digests with the drive, queue what differs.

        Answers whether any directory differed. One walk at a time; a walk
        that fails short of the fence is a line in the log and the same
        interval again, and the fence's refusal ends the loop.
        """
        if self.reconciling:
            return False
        self.reconciling = True
        differed = False
        try:
            differed = await self._reconcile()
        except LeaseSupersededError:
            raise
        except Exception as failure:
            logger.warning(
                "live sync of %s: the folder walk did not finish (%s: %s); tried again in %.0fs",
                self._who,
                type(failure).__name__,
                failure,
                self._reconcile_interval,
                extra=self._log_ids,
            )
            return False
        finally:
            self.reconciling = False
            self._last_reconcile = self.clock.monotonic()
        wait = doubled(self._reconcile_interval, floor=self.reconcile_every, cap=self.reconcile_cap)
        self._reconcile_interval = self.reconcile_every if differed else wait
        return differed

    async def _reconcile(self) -> bool:
        if not self._fence_open():
            return False
        local, children, excluded = await _walk_digests(
            self.root,
            respect_gitignore=self.respect_gitignore,
            exclude_presets=self.exclude_presets,
            known_of=self._known,
            stamps=self._stamps,
        )
        # The directories the rules leave on the machine are asked about too:
        # rows the drive still holds for one — listed before the rules said
        # so — must not linger there, and a digest is how the walk learns of
        # them without listing the drive.
        paths = [*local, *excluded]
        drive: dict[str, DirDigest] = {}
        for start in range(0, len(paths), DIGEST_BATCH):
            if not self._fence_open():
                return False
            with _fenced():
                answer = self.api.digests(paths[start : start + DIGEST_BATCH])
            drive.update(answer.digests)
            await asyncio.sleep(0)
        stale = [path for path in excluded if path in drive]
        for path in stale:
            self._queue_meta(TreeEntry(op="delete", path=path))
        differing = [path for path in local if drive.get(path, EMPTY) != local[path]]
        if not differing:
            if stale:
                self._journal_intents()
            return bool(stale)
        # What the drive files in each differing directory is the truth: a row
        # deleted while the box was down is named here and nowhere else, and a
        # file holding its agreed bytes that the drive no longer files there was
        # trashed or moved by somebody else, so it is never offered back.
        listed: dict[str, Sequence[str]] = {}
        for start in range(0, len(differing), DIGEST_NAMES_BATCH):
            if not self._fence_open():
                return False
            chunk = differing[start : start + DIGEST_NAMES_BATCH]
            with _fenced():
                answer = self.api.digests(chunk, names=chunk)
            listed.update(answer.children)
            await asyncio.sleep(0)
        skip = set(excluded)
        for directory in differing:
            on_drive = set(listed.get(directory, ()))
            present = {entry.path for entry in children[directory]}
            for entry in children[directory]:
                if entry.hash is not None and entry.path.rpartition("/")[2] not in on_drive:
                    continue
                self._queue_meta(entry, force=True)
                if entry.kind == "file" and self._stamps.get(entry.path) != (
                    entry.size,
                    entry.mtime_ns,
                ):
                    # Bytes the drive may not hold: owed like a watcher write.
                    self._pending.setdefault(
                        entry.path,
                        PendingChange(
                            relative=entry.path, kind="write", path=self.root / entry.path
                        ),
                    )
                    if entry.path not in self._first_seen:
                        self._seen_seq += 1
                        self._first_seen[entry.path] = self._seen_seq
            for name in on_drive:
                gone = f"{directory}/{name}" if directory else name
                if gone in present or gone in skip or not self._lost(gone):
                    continue
                self._queue_meta(TreeEntry(op="delete", path=gone))
        self._journal_intents()
        logger.info(
            "live sync of %s: the folder walk found %d director(ies) the drive disagrees with",
            self._who,
            len(differing),
            extra=self._log_ids,
        )
        return True

    def _lost(self, relative: str) -> bool:
        """Whether a child the drive files at ``relative`` is gone from the
        disk in a way a delete can say.

        Something still on the disk under that name was not deleted; a name
        the tree route would refuse cannot ride in a batch without costing it.
        """
        if not _tree_safe(relative) or relative.endswith(_INBOUND_SUFFIX):
            return False
        try:
            relative.encode("utf-8")
        except UnicodeEncodeError:
            return False
        return not os.path.lexists(self.root / relative)

    # -- the release drain ----------------------------------------------

    def request_drain(self, seconds: float) -> None:
        """Have the loop drain for up to ``seconds`` once its watcher ends."""
        self._drain_for = max(seconds, 0.0)

    def unsynced(self) -> list[str]:
        """The files the content queue still owes the drive, root-relative."""
        return sorted(
            relative for relative, item in dict(self._pending).items() if item.kind == "write"
        )

    def drain(self, seconds: float) -> list[str]:
        """Send what the queues hold for up to ``seconds``; answer what is left.

        Bounded by the clock and by a round count, so a drain that meets a
        closed fence or a drive refusing every round still ends on time. The
        fence's refusal ends it at once: the folder is not ours to send to.
        What is left is kept in :attr:`remainder` for whoever releases.
        """
        started = self.clock.monotonic()
        rounds = int(seconds / _DRAIN_STEP) + 1
        try:
            for _ in range(rounds):
                self._flush_contained()
                if not self.unsynced():
                    break
                spent = self.clock.monotonic() - started
                if spent >= seconds:
                    break
                self.sleep(min(_DRAIN_STEP, seconds - spent))
        except LeaseSupersededError:
            logger.info(
                "live sync of %s: the drain met the fence; the rest stays on the machine",
                self._who,
                extra=self._log_ids,
            )
        self.remember_nodes(now=True)
        self.remainder = self.unsynced()
        if self.remainder:
            logger.warning(
                "live sync of %s: %d file(s) did not land within the %.0fs drain",
                self._who,
                len(self.remainder),
                seconds,
                extra=self._log_ids,
            )
        return self.remainder

    # -- inbound --------------------------------------------------------

    def pull_inbound(self) -> list[LiveEntry]:
        """Take what the drive wrote while the box held the folder.

        Three refusals shape this direction, and they mirror the push's:

        * **containment is decided on the drive's own path, segment by
          segment.** ``a/bc`` is not inside ``a/b``, so an item the server
          names beside the leased folder writes nothing here — and is not
          answered for either, because a holder that has no business touching
          a node has no standing to report on it;
        * **neither side's bytes are overwritten.** A file whose content is no
          longer the one this holder and the drive last agreed on is work done
          after the version being sent. It is moved aside under a staging name,
          the drive's bytes (or its delete) are applied, and the moved bytes
          are submitted as a conflicted copy the drive names; the entry is
          answered ``applied`` with ``displaced`` naming where they went;
        * **the file appears whole or not at all.** Every download lands under
          a temporary name in the destination's own directory and is renamed
          into place, so the agent reading the folder mid-turn meets the old
          file or the new one.

        One pass runs at a time. A caller that asks while a pass is still
        fetching gets an empty answer rather than a second pass: the entries
        are the same entries, and two passes would fetch one node into one
        folder at once. Whoever asked is better served watching what the
        running pass is landing than racing it.

        The answer is what the drive actually took, which is what a caller may
        count as settled: the verdicts go back in batches the server's ceiling
        accepts, and a row it would not take yet is still a row it is holding.
        """
        if not self._fence_open():
            return []
        if not self._inbound_pass.acquire(blocking=False):
            logger.debug(
                "live sync of %s: a drain is already running", self._who, extra=self._log_ids
            )
            return []
        try:
            # A conflicted copy an earlier pass (or a process that died) left
            # staged goes to the drive before anything new is taken.
            self._resubmit_staged()
            with _fenced():
                queued = self.api.inbound()
            reported: list[LiveEntry] = []
            for entry in sorted(queued, key=lambda item: (item.seq, item.node_id)):
                try:
                    answer = self._apply_inbound(entry)
                except LeaseSupersededError:
                    raise
                except Exception as failure:
                    # One entry the box could not take is one file missing, not
                    # a drain that stops: everything else the drive is holding
                    # still lands, and this node stays owed for the next pass.
                    logger.warning(
                        "live sync of %s: the drive's entry for node %s was not taken "
                        "(%s: %s); left owed",
                        self._who,
                        entry.node_id,
                        type(failure).__name__,
                        failure,
                        extra=self._log_ids,
                    )
                    continue
                if answer is not None:
                    reported.append(answer)
            answered = self._batch(reported)
            self.remember_nodes()
            return answered
        finally:
            self._inbound_pass.release()

    def _apply_inbound(self, entry: InboundEntry) -> LiveEntry | None:
        if entry.state == "inbound_delete":
            return self._apply_delete(entry)
        if entry.state == "inbound_rename":
            return self._apply_rename(entry)
        return self._apply_write(entry, self._inbound_relative(entry.node_id))

    def _apply_write(self, entry: InboundEntry, relative: str | None) -> LiveEntry | None:
        """Land the drive's bytes for ``relative``; the box's own, when they
        differ from what the two last agreed on, become a conflicted copy."""
        if relative is None:
            return None
        target = MaterializationTarget(self.root)
        try:
            final = target.resolve(relative.encode("utf-8", "surrogateescape"))
        except ContainmentError:
            return None
        if self.peer is not None and self._peer_took_inbound(entry.node_id, relative, final):
            return LiveEntry(node_id=entry.node_id, state="applied")
        if entry.node_id in self._session_heads and self._diverged(relative, final):
            # A live session's write back, while this box holds newer bytes of
            # its own (an agent's edit not sent yet): they go up first, the
            # session merges them, and the write back after carries both.
            self.promote(relative)
            return None
        # The drive's bytes are fetched before the box's file is touched, and
        # the box's are then kept aside under a second name while the first
        # still holds them. The name is never empty: a watcher that looked in
        # between would report the file deleted, and a holder that passes that
        # on trashes the node the person just wrote a version onto — the file
        # comes back as a new node with none of its history.
        fetched = _fetch_inbound(
            self.api,
            self.spool,
            entry.node_id,
            relative,
            deadline=inbound_deadline(self._inbound_sizes.get(entry.node_id)),
            who=self._who,
            ids=self._log_ids,
            backoff=self._inbound_backoff,
            seq=entry.seq,
            now=self.clock.monotonic(),
        )
        if fetched is None:
            # The bytes took too long to be believed, or were refused. Nothing
            # touched the box's file, and the entry is still owed: the next
            # pass meets the same divergence.
            return None
        try:
            staged = self._keep_aside(final) if self._diverged(relative, final) else None
            # A process that dies from here on leaves the staged bytes for the
            # next start to submit; the owed entry is fetched again.
            if not self._put_in_place(fetched, final):
                if staged is not None:
                    self._unkeep(staged, final)
                return None
        finally:
            with contextlib.suppress(OSError):
                fetched.unlink()
        fresh = relative not in self._known
        self._adopt(relative, entry.node_id, final)
        if staged is None:
            if fresh:
                self._note_copy_of_ours(entry.node_id, relative)
            return LiveEntry(node_id=entry.node_id, state="applied")
        displaced = self._submit_staged(staged, relative, entry.node_id, deleted=False)
        return LiveEntry(node_id=entry.node_id, state="applied", displaced=displaced)

    def _apply_delete(self, entry: InboundEntry) -> LiveEntry | None:
        """Remove what the drive trashed — the box's own later bytes survive
        it as a conflicted copy, and the drive's trash keeps the record."""
        relative = self._relative_of(entry.node_id)
        if relative is None:
            relative = self._inbound_relative(entry.node_id)
            if relative is not None and self._nodes.get(relative, entry.node_id) != entry.node_id:
                # The path the trashed node was filed at now holds another
                # node's file on this box — one renamed onto the freed name.
                # That file is not this node's to remove.
                return LiveEntry(node_id=entry.node_id, state="applied")
        if relative is None:
            return None
        target = MaterializationTarget(self.root)
        try:
            local = target.resolve(relative.encode("utf-8", "surrogateescape"))
        except ContainmentError:
            return None
        staged = self._stage(local) if self._diverged(relative, local) else None
        if staged is None:
            with contextlib.suppress(OSError):
                self.tree.unlink(local)
        self._nodes.pop(relative, None)
        self._known.pop(relative, None)
        self._etags.pop(relative, None)
        self._etag_digests.pop(relative, None)
        self._stamps.pop(relative, None)
        self._sent_meta.pop(relative, None)
        if staged is None:
            return LiveEntry(node_id=entry.node_id, state="applied")
        # No displaced node is named: the drive trashed it, and it files a copy
        # only beside a live one. The bytes land as a new file under the
        # conflicted-copy name, and the trash is the record of the delete.
        displaced = self._submit_staged(staged, relative, None, deleted=True)
        return LiveEntry(node_id=entry.node_id, state="applied", displaced=displaced)

    def _apply_rename(self, entry: InboundEntry) -> LiveEntry | None:
        """Move the node's file to the name the drive now files it under.

        A file the box has at the destination that the drive never had is not
        overwritten by the move: it is staged first and submitted as a
        conflicted copy of the node that now holds the name.
        """
        destination = self._inbound_relative(entry.node_id)
        if destination is None:
            return None
        source = self._relative_of(entry.node_id)
        target = MaterializationTarget(self.root)
        try:
            moved_to = target.resolve(destination.encode("utf-8", "surrogateescape"))
            moved_from = (
                None
                if source is None
                else target.resolve(source.encode("utf-8", "surrogateescape"))
            )
        except ContainmentError:
            return None
        if source is None or moved_from is None or not self.tree.is_file(moved_from):
            # Nothing here carries that node, so the new name is filled from
            # the drive rather than left as a gap in the folder.
            return self._apply_write(entry, destination)
        if source == destination:
            return LiveEntry(node_id=entry.node_id, state="applied")
        staged: Path | None = None
        if self.tree.is_file(moved_to) and self._diverged(destination, moved_to):
            staged = self._stage(moved_to)
        self.tree.rename(moved_from, moved_to)
        self._forget_exact(destination)
        self._move_known(source, destination)
        self._nodes[destination] = entry.node_id
        listed = self._file_entry(destination, moved_to)
        if listed is not None:
            # The drive already lists the moved row under its new name.
            self._sent_meta[destination] = listed.fingerprint()
        self.tombstones.discard(destination)
        if staged is None:
            return LiveEntry(node_id=entry.node_id, state="applied")
        displaced = self._submit_staged(staged, destination, entry.node_id, deleted=False)
        return LiveEntry(node_id=entry.node_id, state="applied", displaced=displaced)

    # -- conflicted copies ----------------------------------------------

    def _stage(self, local: Path) -> Path:
        """Move ``local`` aside under a staging name of its own, in its own
        directory so the move cannot cross a device."""
        token = uuid.uuid4().hex[:8]
        staged = local.parent / conflict_staging_name(local.name, token)
        self.tree.rename(local, staged)
        self._staged.add(staged)
        return staged

    def _keep_aside(self, local: Path) -> Path:
        """Keep ``local``'s bytes under a staging name as well as its own.

        A second link to the same file, so the name keeps its bytes until the
        drive's replace them. A filesystem that cannot link falls back to the
        move, which leaves the name empty only between two renames.
        """
        token = uuid.uuid4().hex[:8]
        staged = local.parent / conflict_staging_name(local.name, token)
        try:
            self.tree.link(local, staged)
        except OSError:
            self.tree.rename(local, staged)
        self._staged.add(staged)
        return staged

    def _still_the_original(self, staged: Path, local: Path) -> bool:
        """Whether ``staged`` is a second name for the file ``local`` still is."""
        try:
            return self.tree.samefile(staged, local)
        except OSError:
            return False

    def _unkeep(self, staged: Path, local: Path) -> None:
        """Undo :meth:`_keep_aside` when the drive's bytes could not be placed."""
        if not self._still_the_original(staged, local):
            self._unstage(staged, local)
            return
        with contextlib.suppress(OSError):
            self.tree.unlink(staged)
            self._staged.discard(staged)

    def _unstage(self, staged: Path, local: Path) -> None:
        """Put staged bytes back under their own name, when nothing took it."""
        if self.tree.stat(local) is not None:
            # Something wrote the name meanwhile; the staged bytes stay where
            # they are and the next pass submits them as a copy.
            return
        with contextlib.suppress(OSError):
            self.tree.rename(staged, local)
            self._staged.discard(staged)

    def _submit_staged(
        self, staged: Path, relative: str, conflict_of: str | None, *, deleted: bool
    ) -> str | None:
        """Send staged bytes to the drive as a conflicted copy and put them
        under the name it answers with. Answers that name, under the root.

        ``None`` when the copy is not made yet: the staging file stays, and the
        next start or reconciliation pass submits it again. Only the fence's
        refusal leaves here, because that is the one answer no retry changes.
        """
        parsed = _staged_original(staged.name)
        token = parsed[1] if parsed is not None else uuid.uuid4().hex[:8]
        try:
            with _fenced():
                try:
                    answer = self.api.submit_conflict(
                        staged, relative, conflict_of=conflict_of, token=token
                    )
                except ConflictTargetGoneError:
                    if conflict_of is None:
                        raise
                    # The node was trashed under an open session, which every replay of this token
                    # meets again. Filed instead as a new file beside the name, under a token
                    # derived from this one: a new key for the drive, yet the same key on every
                    # later replay, so a crash past this point never makes a second copy.
                    answer = self.api.submit_conflict(
                        staged, relative, conflict_of=None, token=f"{token}-unbound"
                    )
        except LeaseSupersededError:
            raise
        except Exception as failure:
            logger.warning(
                "live sync of %s: the box's copy of %s was not sent as a conflicted copy "
                "(%s: %s); it waits as %s and is offered again",
                self._who,
                relative,
                type(failure).__name__,
                failure,
                staged.name,
                extra=self._log_ids,
            )
            return None
        return self._place(staged, relative, answer, deleted=deleted)

    def _place(
        self, staged: Path, relative: str, answer: ConflictAnswer, *, deleted: bool
    ) -> str | None:
        """Rename staged bytes to the name the drive chose for them."""
        name = answer.name
        parent = relative.rpartition("/")[0]
        copy = f"{parent}/{name}" if parent else name
        if not name or "/" in name or "\\" in name or name in (".", "..") or not _tree_safe(copy):
            self._unplaced.add(staged)
            logger.warning(
                "live sync of %s: the drive named %s's conflicted copy %r, which this "
                "box will not write; the bytes stay as %s",
                self._who,
                relative,
                name,
                staged.name,
                extra=self._log_ids,
            )
            return None
        final = staged.parent / name
        # Past the drive's ceiling the bytes are a new version of the newest
        # copy, answered under that copy's name. When the box still holds that
        # copy as the two last agreed, its older bytes are a version on the
        # drive and the newer ones take its place here too.
        newest_copy = self._nodes.get(copy) == answer.node_id and not self._diverged(copy, final)
        if self.tree.stat(final) is not None and not newest_copy:
            # A file the box wrote under that very name that the drive has not
            # been told of yet. Replacing it would lose it; the copy is on the
            # drive, so the staged bytes wait here for someone to look.
            self._unplaced.add(staged)
            logger.warning(
                "live sync of %s: %s already exists on the box; the conflicted copy of %s "
                "stays as %s",
                self._who,
                copy,
                relative,
                staged.name,
                extra=self._log_ids,
            )
            return None
        self.tree.rename(staged, final)
        self._staged.discard(staged)
        self._adopt(copy, answer.node_id, final)
        with self._conflict_lock:
            self._conflicts.append(ConflictNotice(path=relative, copy=copy, deleted=deleted))
        return copy

    def _resubmit_staged(self) -> int:
        """Submit every conflicted copy a staging file still holds.

        The staging name says which file the bytes displaced and which attempt
        they belong to; the node they displace is the one this holder knows at
        that path, or the drive's answer for it, or — when neither knows the
        path any more — no node, and the drive files the bytes as a new file.
        Answers how many were placed.
        """
        now = self.clock.monotonic()
        if self._scanned_at is None or now - self._scanned_at >= STAGING_SCAN_EVERY:
            self._scanned_at = now
            self._staged.update(_staging_files(self.root))
        placed = 0
        for staged in sorted(self._staged):
            if staged in self._unplaced or not self.tree.is_file(staged):
                self._staged.discard(staged)
                continue
            parsed = _staged_original(staged.name)
            if parsed is None:
                continue
            if self._still_the_original(staged, staged.parent / parsed[0]):
                # Kept aside by a pass that died before the drive's bytes
                # replaced the original: nothing was displaced, and the entry
                # is still owed, so the next pass meets the divergence again.
                with contextlib.suppress(OSError):
                    self.tree.unlink(staged)
                self._staged.discard(staged)
                continue
            try:
                inside = staged.parent.resolve().relative_to(self.root.resolve()).as_posix()
            except (OSError, ValueError):
                continue
            relative = f"{inside}/{parsed[0]}" if inside not in ("", ".") else parsed[0]
            try:
                node_id = self._nodes.get(relative)
                if node_id is None:
                    with _fenced():
                        node_id = self.api.resolve([relative]).get(relative)
            except LeaseSupersededError:
                raise
            except Exception as failure:
                logger.info(
                    "live sync of %s: the node %s's staged copy displaced was not looked up "
                    "(%s); it is offered again",
                    self._who,
                    relative,
                    failure,
                    extra=self._log_ids,
                )
                continue
            if self._submit_staged(staged, relative, node_id or None, deleted=False):
                placed += 1
        return placed

    def _replay_staged_at_start(self) -> None:
        """What a previous run left staged goes to the drive before this one
        watches anything. Never the end of the start: a copy that cannot be
        sent now is sent by the next pass."""
        if not self._fence_open():
            return
        if not self._inbound_pass.acquire(blocking=False):
            return
        try:
            self._resubmit_staged()
        except LeaseSupersededError:
            raise
        except Exception as failure:
            logger.warning(
                "live sync of %s: staged conflicted copies were not replayed at start (%s: %s)",
                self._who,
                type(failure).__name__,
                failure,
                extra=self._log_ids,
            )
        finally:
            self._inbound_pass.release()

    def take_conflict_notices(self, mentioned: Iterable[str]) -> list[str]:
        """The sentences owed to an agent whose tool call names these strings.

        A conflict is said once: on the first tool result whose arguments name
        its file or its copy, by the path under the root or the absolute one.
        """
        texts = [text for text in mentioned if text]
        if not texts:
            return []
        base = str(self.root)
        said: list[str] = []
        with self._conflict_lock:
            kept: deque[ConflictNotice] = deque(maxlen=CONFLICT_NOTICES_KEPT)
            for notice in self._conflicts:
                named = any(
                    _mentions(text, notice.path, base) or _mentions(text, notice.copy, base)
                    for text in texts
                )
                if named:
                    said.append(notice.sentence())
                else:
                    kept.append(notice)
            self._conflicts = kept
        return said

    @property
    def conflicts(self) -> tuple[ConflictNotice, ...]:
        """The conflicts not yet told to the agent, oldest first."""
        with self._conflict_lock:
            return tuple(self._conflicts)

    def _put_in_place(self, fetched: Path, final: Path) -> bool:
        """Put fetched bytes over ``final`` through the tree; False if refused."""
        try:
            self.tree.install(final, fetched)
        except OSError as failure:
            # The bytes arrived but could not be put under the name they were
            # fetched for. The node stays owed rather than settled, or the
            # drive would record as taken a file the box does not have.
            logger.warning(
                "live sync of %s: inbound bytes for %s could not be put in place (%s: %s); "
                "left owed",
                self._who,
                final.name,
                type(failure).__name__,
                failure,
                extra=self._log_ids,
            )
            return False
        return True

    def _adopt(self, relative: str, node_id: str, final: Path) -> None:
        """Agree with the drive on what this path now holds.

        Without this the watcher's echo of the write would push the very bytes
        that just arrived back up as a new version, and the two planes would
        chase each other for as long as the folder is held.
        """
        self._nodes[relative] = node_id
        # The etag the drive described this node at before its bytes came
        # down; a node it never described (a copy the drive just named) has no
        # agreed etag until this holder's own upload of it is confirmed.
        described = self._inbound_etags.pop(node_id, None)
        if described is not None:
            self._etags[relative] = described
        else:
            self._etags.pop(relative, None)
        # Bytes written over the file: never named to the text peer as what
        # the agent's next save was made on (see _agreed_version).
        self._etag_digests.pop(relative, None)
        with contextlib.suppress(OSError):
            self._known[relative] = self.hasher(final)
            stamp = _stamp(final)
            if stamp is not None:
                self._stamps[relative] = stamp
        # The drive lists this row already — it sent it — so the watcher's
        # echo of the write has nothing to tell the metadata queue either.
        listed = self._file_entry(relative, final)
        if listed is not None:
            self._sent_meta[relative] = listed.fingerprint()
        self.tombstones.discard(relative)

    def _note_copy_of_ours(self, node_id: str, copy: str) -> None:
        """Tell the agent when a file the drive just sent down is the web's
        version this holder's own upload displaced: the drive kept the box's
        bytes under the name and filed the web's beside it as ``copy``."""
        of = self._inbound_copy_of.pop(node_id, None)
        path = None if of is None else self._relative_of(of)
        if path is None or path == copy:
            return
        with self._conflict_lock:
            self._conflicts.append(ConflictNotice(path=path, copy=copy, kept_ours=True))

    def _diverged(self, relative: str, local: Path) -> bool:
        """Whether the box's copy is newer than what the drive is sending.

        The comparison is against the content this holder and the drive last
        agreed on. A file the drive has never had bytes for counts as diverged:
        overwriting it would drop work nobody ever saw.
        """
        if not local.is_file():
            return False
        agreed = self._known.get(relative)
        if agreed is None:
            return True
        try:
            return self.hasher(local) != agreed
        except OSError:
            return True

    def _inbound_relative(self, node_id: str) -> str | None:
        """The path under ``root`` the drive files ``node_id`` at, or None.

        None is the refusal: a path outside the leased folder, the folder
        itself, or an item with no path at all.
        """
        with _fenced():
            item = self.api.item(node_id)
        self._session_heads.discard(node_id)
        if _head_source(item) == "document_snapshot":
            self._session_heads.add(node_id)
        if described := facet_content_hash(item.get("file") or {}):
            self._inbound_hashes[node_id] = described
        copy_of = item.get("conflictOf") or item.get("conflict_of")
        if isinstance(copy_of, str) and copy_of:
            self._inbound_copy_of[node_id] = copy_of
        etag = item.get("etag")
        if isinstance(etag, (str, int)) and not isinstance(etag, bool) and str(etag):
            # Read BEFORE the download, so it can only be as old as the bytes
            # that land or older -- never newer. Older is safe: an upload
            # fences on the head itself while the head holds the agreed bytes.
            self._inbound_etags[node_id] = str(etag)
        size = item.get("size")
        if isinstance(size, int):
            # What the download of this node is budgeted on: the ceiling grows
            # with the file, so a large drop on a slow link is not cut short.
            self._inbound_sizes[node_id] = size
        raw = item.get("pathBytes") or item.get("path_bytes") or item.get("path")
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "surrogateescape")
        if not isinstance(raw, str):
            return None
        segments = [part for part in raw.strip().strip("/").split("/") if part]
        base = [part for part in self.root_path.split("/") if part]
        if segments[: len(base)] != base:
            return None
        inside = segments[len(base) :]
        if not inside:
            return None
        return "/".join(inside)

    def _relative_of(self, node_id: str) -> str | None:
        """Where this holder last knew ``node_id`` to live under ``root``.

        Over a snapshot, because the flush thread is learning node ids for the
        files the agent is writing while the drain thread walks them here. A
        walk of the live map ends in ``dictionary changed size during
        iteration`` the moment the two meet — mid-pass, so everything the drive
        was holding after that entry is abandoned with it.
        """
        for relative, known in list(self._nodes.items()):
            if known == node_id:
                return relative
        return None


#: Names a chat folder keeps beside its working directory and never streams:
#: the manifest, the logs, the trace digest and the runtime directory are the
#: box's own records of the folder rather than the chat's work. The watcher
#: never meets them because the working directory is the watched root and they
#: sit above it; the filter is the belt for a caller that points one at the
#: folder. The set is the one the drive marks a record by.
RECORD_NAMES: Final[frozenset[str]] = CHAT_RECORD_NAMES


async def _walk_digests(
    root: Path,
    *,
    respect_gitignore: bool,
    exclude_presets: tuple[str, ...],
    known_of: Mapping[str, tuple[str, int]],
    stamps: Mapping[str, tuple[int, int]],
) -> tuple[dict[str, DirDigest], dict[str, list[TreeEntry]], list[str]]:
    """Every exported directory's digest, the rows its children make, and
    the directories the rules exclude directly under an exported one.

    The walk is the checkpoint push's, with this folder's rules; what the
    live plane refuses (links, special files, local state, bytes still
    arriving inbound) counts for nothing on either side. A directory the
    plane refuses is not descended into, so a directory under an excluded
    one is covered by its excluded ancestor.
    """
    local: dict[str, DirDigest] = {"": EMPTY}
    children: dict[str, list[TreeEntry]] = {"": []}
    excluded: list[str] = []
    visited = 0
    for entry in walk(
        root,
        respect_gitignore=respect_gitignore,
        exclude_presets=exclude_presets,
        skip_local_state=True,
    ):
        visited += 1
        if visited % WALK_YIELD_EVERY == 0:
            await asyncio.sleep(0)
        if entry.kind not in (EntryKind.FILE, EntryKind.DIRECTORY):
            continue
        relative = entry.relative.decode("utf-8", "surrogateescape")
        parent, _, _ = relative.rpartition("/")
        if parent not in local:
            continue
        refused = (
            entry.skipped is not None
            or not _tree_safe(relative)
            or relative.endswith(_INBOUND_SUFFIX)
            or not live_watch_filter(Change.added, str(root / relative))
        )
        if refused:
            if entry.kind is EntryKind.DIRECTORY and _tree_safe(relative):
                excluded.append(relative)
            continue
        name = entry.relative.rpartition(b"/")[2]
        if entry.kind is EntryKind.DIRECTORY:
            local[relative] = EMPTY
            children[relative] = []
            local[parent] = local[parent].with_child(child_value("dir", name))
            children[parent].append(TreeEntry(op="upsert", path=relative, kind="dir"))
            continue
        local[parent] = local[parent].with_child(
            child_value("file", name, entry.size, entry.mtime_ns)
        )
        known = known_of.get(relative)
        agreed = known is not None and stamps.get(relative) == (
            entry.size,
            entry.mtime_ns,
        )
        children[parent].append(
            TreeEntry(
                op="upsert",
                path=relative,
                kind="file",
                size=entry.size,
                mtime_ns=entry.mtime_ns,
                mode=entry.mode,
                hash=f"b3:{known[0]}" if agreed and known is not None else None,
            )
        )
    return local, children, excluded


@dataclass
class RestLiveApi:
    """The live plane over a held folder's own fenced client.

    Every call rides the client the mount minted for this lease, so a box
    holding several chats signs each chat's writes with that chat's epoch and
    never with another's. The rows go through the lease's ``tree`` route, one
    batch per call; the bytes reuse the checkpoint push (:func:`push_paths`)
    rather than a second uploader, so the resume state, the hash comparison,
    the pointer rule and the walker's classification are one implementation,
    and a session a checkpoint left open is finished by a live flush instead
    of abandoned. The push files bytes by path, onto the row the ``tree``
    batch made — or, where no row exists yet, onto one it makes itself.
    """

    files: FilesApi
    http: httpx.Client
    root: Path
    drive_id: str
    lease_node_id: str
    dest: str
    #: The step from the leased folder down to the watched directory. The lease
    #: is on the chat's folder and the stream is of the working directory
    #: inside it, so the node this holder was handed sits one level ABOVE the
    #: bytes: every call that addresses the drive by the lease node — the tree,
    #: and the push's anchor — takes this step before it names a path, or the
    #: file the agent just wrote is filed in the chat folder beside the working
    #: directory rather than in it. Empty when the two are the same directory.
    inside: str = ""
    home: Path | None = None
    push: Callable[..., Any] = push_paths
    base: str = "/api/v1/files"
    #: The clock a download's ceiling is measured on. Injected so a test can
    #: stall a stream without spending the minutes the ceiling is made of.
    monotonic: Callable[[], float] = time.monotonic

    def tree(self, batch_id: str, entries: Sequence[TreeEntry], *, gzip_above: int) -> TreeAnswer:
        """List (or unlist) these paths on the drive, in one batch applied whole.

        The paths are the watched directory's, so each takes the step down
        from the leased folder first. A body larger than ``gzip_above`` bytes
        travels compressed: a burst of two thousand rows is a quarter of a
        megabyte of repetitive JSON, and it leaves once a third of a second.
        """
        step = self.inside.strip("/")
        body = json.dumps(
            {"batch_id": batch_id, "entries": [entry.to_wire(step) for entry in entries]},
            separators=(",", ":"),
        ).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if len(body) > gzip_above:
            body = gzip.compress(body, mtime=0)
            headers["Content-Encoding"] = "gzip"
        response = self.http.post(self._lease_url("tree"), content=body, headers=headers)
        response.raise_for_status()
        answer: Any = response.json()
        if not isinstance(answer, Mapping):
            return TreeAnswer()
        return TreeAnswer(
            live_seq=_count(answer, "live_seq", "liveSeq"),
            applied=_count(answer, "applied"),
            landing_count=_count(answer, "landing_count", "landingCount"),
        )

    def digests(self, paths: Sequence[str], *, names: Sequence[str] = ()) -> DriveDigests:
        """The drive's digest of each named directory, keyed as asked, and
        the children of each directory in ``names``.

        The paths are the watched directory's, so each takes the step down
        from the leased folder on the way out and gives it back on the way in.
        """
        step = self.inside.strip("/")

        def under(relative: str) -> str:
            if not step:
                return relative
            return f"{step}/{relative}" if relative else step

        wire = {under(path): path for path in paths}
        asked: dict[str, list[str]] = {"paths": list(wire)}
        if names:
            asked["names"] = [under(path) for path in names]
        body = json.dumps(asked, separators=(",", ":")).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if len(body) > _DIGEST_GZIP_ABOVE:
            body = gzip.compress(body, mtime=0)
            headers["Content-Encoding"] = "gzip"
        response = self.http.post(self._lease_url("tree/digests"), content=body, headers=headers)
        response.raise_for_status()
        answer: Any = response.json()
        rows = answer.get("digests") if isinstance(answer, Mapping) else None
        found: dict[str, DirDigest] = {}
        for key, row in (rows if isinstance(rows, Mapping) else {}).items():
            path = wire.get(key)
            if path is None or not isinstance(row, Mapping):
                continue
            try:
                found[path] = from_hex(int(row.get("count", 0)), str(row.get("xor", "0")))
            except (TypeError, ValueError):
                continue
        lists = answer.get("children") if isinstance(answer, Mapping) else None
        listed = set(names)
        children: dict[str, list[str]] = {}
        for key, row in (lists if isinstance(lists, Mapping) else {}).items():
            path = wire.get(key)
            if path is None or path not in listed or not isinstance(row, list):
                continue
            children[path] = [
                name for name in row if isinstance(name, str) and name and "/" not in name
            ]
        return DriveDigests(digests=found, children=children)

    def resolve(self, paths: Sequence[str]) -> dict[str, str]:
        """The node id filed at each path the drive lists; nothing is made."""
        found: dict[str, str] = {}
        for relative in paths:
            node_id = self._node_at(relative)
            if node_id is not None:
                found[relative] = node_id
        return found

    def upload(
        self, rel_path: str, node_id: str, size: int, *, base: AgreedBase | None = None
    ) -> str | None:
        """Send one file's current bytes onto the row filed at its path.

        Unchanged bytes move nothing. ``node_id`` is the row the tree batch
        made; the push reaches the same row by path, which is also how it
        lands a file whose row the drive has not listed yet. ``base`` fences
        the write on what this holder last agreed (:class:`AgreedBase`).
        """
        del node_id, size
        summary = self._push_paths([rel_path], bases={rel_path: base} if base else None)
        agreed = getattr(summary, "agreed", None)
        filed = agreed.get(rel_path) if isinstance(agreed, Mapping) else None
        return filed if isinstance(filed, str) else None

    def live_batch(self, entries: Sequence[LiveEntry]) -> LiveBatchAnswer:
        """Tell the server what this holder is doing to each node right now."""
        response = self.http.post(
            self._live_url(), json={"entries": [entry.to_wire() for entry in entries]}
        )
        response.raise_for_status()
        body: Any = response.json()
        if not isinstance(body, Mapping):
            return LiveBatchAnswer()
        return LiveBatchAnswer(
            live_seq=int(body.get("liveSeq", 0) or 0), pending=int(body.get("pending", 0) or 0)
        )

    def inbound(self) -> list[InboundEntry]:
        """What the server is asking this holder to take onto the box.

        A row naming a state this build has never heard of is dropped rather
        than carried into an apply: the holder acts on what it understands and
        leaves the rest for the server to keep offering.
        """
        response = self.http.get(self._live_url(), params={"inbound": "true"})
        response.raise_for_status()
        body: Any = response.json()
        rows = body.get("entries") if isinstance(body, Mapping) else None
        if not isinstance(rows, list):
            return []
        found: list[InboundEntry] = []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            node_id = row.get("nodeId")
            state = row.get("state")
            if not isinstance(node_id, str) or not node_id or state not in _INBOUND_STATES:
                continue
            found.append(
                InboundEntry(node_id=node_id, state=state, seq=int(row.get("seq", 0) or 0))
            )
        return found

    def item(self, node_id: str) -> Mapping[str, Any]:
        return self.files.item(self.drive_id, node_id)

    def download(
        self, node_id: str, into: Path, *, deadline: float = INBOUND_DOWNLOAD_FLOOR
    ) -> None:
        """Stream a node's head version onto the box.

        Streamed rather than read whole because an inbound file is whatever a
        person dropped into the chat's folder on the web, and holding it in
        memory first is a limit the drive itself does not have. ``deadline``
        is the whole transfer's ceiling, checked between chunks: a stream that
        is still arriving when it passes is cut and left owed, while one that
        finishes under it lands however slow the link was.
        """
        into.parent.mkdir(parents=True, exist_ok=True)
        started = self.monotonic()
        with self.http.stream(
            "GET",
            f"{self.base}/drives/{self.drive_id}/items/{node_id}/content",
            # The route answers a redirect to a signed URL on the content
            # origin — that is where bytes live, and a client that stops at the
            # redirect downloads the empty body of a 302 instead of the file.
            # The credential does not travel with it: the hop crosses origins,
            # so the signed URL is the whole authority, which is the point of
            # minting one.
            follow_redirects=True,
        ) as response:
            # A refusal's body has to be in hand before it is raised on: the
            # caller reads the Files error code off it to tell "the lease moved
            # on" from "these bytes are not there yet", and a streamed response
            # nobody read answers neither.
            if response.is_error:
                response.read()
            response.raise_for_status()
            chunks = response.iter_bytes()
            stream_into(into, chunks, self.monotonic, since=started, ceiling=deadline, what=node_id)

    def submit_conflict(
        self, staged: Path, relative: str, *, conflict_of: str | None, token: str
    ) -> ConflictAnswer:
        """Send staged bytes as a conflicted copy through one fenced session.

        The session is opened with ``conflictOf`` so the drive files the bytes
        as a new node beside that one, under a name it chooses; the answer
        names the copy. Every call carries an idempotency key made of the lease
        and the staging token, so a submission replayed after a crash is the
        same submission and the drive answers with the copy it already made.
        """
        parent, _, name = relative.rpartition("/")
        parent_id = self._node_at(parent)
        if parent_id is None:
            raise FileNotFoundError(f"the drive lists no folder at {parent or '.'!r}")
        size = staged.stat().st_size
        key = f"alkera-conflict-{self.lease_node_id}-{token}"
        body: dict[str, Any] = {"parentId": parent_id, "name": name, "declaredSize": size}
        if conflict_of:
            body["conflictOf"] = conflict_of
        opened = self._post(f"{self.base}/uploads", body, key=f"{key}-open")
        session_id = str(opened["uploadId"])
        part_size = int(opened.get("partSize") or 0)
        if part_size <= 0:
            raise UploadSessionError(
                f"{relative}: the conflict session offers a part size of {part_size}"
            )
        parts: list[dict[str, Any]] = []
        with staged.open("rb") as handle:
            sent = 0
            while sent < size or not parts:
                chunk = handle.read(min(part_size, size - sent))
                if not chunk and size:
                    raise UploadSessionError(
                        f"{relative}: the staged copy held {sent} bytes, not {size}"
                    )
                sent += len(chunk)
                number = len(parts) + 1
                digest = _blake3_hex(chunk)
                response = self.http.put(
                    f"{self.base}/uploads/{session_id}/parts/{number}",
                    content=chunk,
                    headers={
                        "Idempotency-Key": f"{key}-part-{number}",
                        "X-Part-Checksum": digest,
                        "Content-Type": "application/octet-stream",
                    },
                )
                response.raise_for_status()
                parts.append({"partNo": number, "size": len(chunk), "checksum": digest})
        try:
            completed = self._post(
                f"{self.base}/uploads/{session_id}/complete",
                {"parts": parts, "conflictBehavior": "rename"},
                key=f"{key}-complete",
            )
        except httpx.HTTPStatusError as refused:
            if conflict_of and refused.response.status_code == 404:
                raise ConflictTargetGoneError(
                    f"{relative}: node {conflict_of} was trashed while its copy was uploading"
                ) from refused
            raise
        answer: dict[str, Any] = dict(completed)
        operation = completed.get("id")
        if _answer_node(answer) is None and isinstance(operation, str) and operation:
            answer.update(
                self.files.await_operation(self.drive_id, operation, timeout=60.0, interval=0.2)
            )
        node_id = _answer_node(answer)
        if node_id is None:
            raise UploadSessionError(f"{relative}: the drive did not name the conflicted copy")
        named = answer.get("name")
        if not isinstance(named, str) or not named:
            named = self.files.item(self.drive_id, node_id).get("name")
        if not isinstance(named, str) or not named:
            raise UploadSessionError(f"{relative}: the conflicted copy {node_id} has no name")
        conflict = answer.get("conflictId") or answer.get("conflict_id")
        return ConflictAnswer(
            node_id=node_id, name=named, conflict_id=str(conflict) if conflict else None
        )

    # -- internals -------------------------------------------------------

    def _post(self, url: str, body: Mapping[str, Any], *, key: str) -> Mapping[str, Any]:
        response = self.http.post(url, json=dict(body), headers={"Idempotency-Key": key})
        response.raise_for_status()
        parsed: Any = response.json()
        return parsed if isinstance(parsed, Mapping) else {}

    def _push_paths(
        self, relatives: Sequence[str], *, bases: Mapping[str, AgreedBase] | None = None
    ) -> Any:
        extra: dict[str, Any] = {} if bases is None else {"bases": bases}
        return self.push(
            **extra,
            files=self.files,
            http=self.http,
            root=self.root,
            dest=self.dest,
            node_id=self.lease_node_id,
            # The lease names the drive: a box on its machine credential has
            # no drive of its own for the push to ask for, and asked, it was
            # answered not-found on every round — the file the agent had just
            # written never left the box.
            drive_id=self.drive_id,
            inside=self.inside,
            paths=[self.root / Path(relative) for relative in relatives],
            skip_local_state=True,
            home=self.home,
        )

    def text_peer(self) -> RestPeerApi:
        """The live text routes over this folder's own fenced client."""
        return RestPeerApi(http=self.http, drive_id=self.drive_id, base=self.base)

    def _live_url(self) -> str:
        return self._lease_url("live")

    def _lease_url(self, route: str) -> str:
        return f"{self.base}/drives/{self.drive_id}/items/{self.lease_node_id}/lease/{route}"

    def _node_at(self, relative: str) -> str | None:
        """The id of the node filed at ``relative``, or ``None`` when none is.

        Asked from the leased node, by the step down to it: the holder is told
        the folder's path only from the deepest ancestor it may read — a box
        on a chat's lease is told the bare name — so an absolute path built
        from ``dest`` names nothing for it. The lease node it always has.

        A 404 is the only absence: any other refusal is a real failure and is
        left to the caller, because reading a 403 as "not there" would mint a
        second node beside one this holder simply may not see.
        """
        inside = self.inside.strip("/")
        step = "/".join(part for part in (inside, relative.strip("/")) if part)
        try:
            item = self.files.item_under(self.drive_id, self.lease_node_id, step)
        except Exception as exc:
            if _status_of(exc) == 404:
                return None
            raise
        node_id = item.get("id")
        return str(node_id) if node_id else None


#: The states a server may ask a holder to take onto the box.
_INBOUND_STATES: Final = frozenset({"inbound", "inbound_delete", "inbound_rename"})
