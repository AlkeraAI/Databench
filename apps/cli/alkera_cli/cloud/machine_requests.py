"""What the drive asks of this machine over its channel, and the answers.

A reader on the web opening a file whose bytes are still only on this box asks
the drive for them, and the drive asks the machine holding the folder: a
``machine.request`` frame on the socket's ``machine:<id>`` channel naming the
lease, the node and the file's path under the lease. This module decides the
answer and, when it is yes, puts the file at the front of that folder's live
content queue (:meth:`alkera_cli.files.live_sync.LiveSync.promote`). The bytes
then land the only way bytes ever reach the drive — the fenced upload — so
nothing here reads a file, hashes one, or puts a byte on the socket.

The answer is one of five:

* ``not_holder`` — this box holds no folder under that lease, holds it at an
  epoch the drive no longer knows it by, or its own fence has closed: whatever
  the drive is waiting for, it will not come from here;
* ``missing`` — the path does not name a regular file inside the folder's live
  root. A path that would leave the root — through ``..``, an absolute name or
  a link planted in the tree — is ``missing`` too, and is never opened;
* ``changed`` — the file is there but is not the one the drive listed, with
  what this box sees instead: the rows are about to say so themselves;
* ``busy`` — this machine already has as many promotions in flight as it
  takes, or this lease has used its promotions for the minute;
* ``accepted`` — the file is queued ahead of everything else.

A ``flush`` asks for the whole folder instead: a person is about to trash a
folder this box holds (or one above it), and the trash waits for what the box
has not pushed yet, so that it goes into the trash with the rest and a restore
brings it back. It is acknowledged ``accepted`` at once (a push outlasts the
drive's ack window), the folder's checkpoint push runs off the socket's loop,
and a second ack settles it: ``flushed`` when everything went, ``busy`` when
the push did not land whole. A lease this box does not hold at the epoch named
is ``not_holder``, as for a promote.

A request of a kind this build does not know is not answered at all: the
drive's own deadline covers it, and a guess would be a lie about a verb this
box never implemented.

A ``live_text`` notice is not a request for an answer either: it says a file
under one of this box's leases has a live document that moved, and names its
version token. It is handed to that folder's text peer
(:class:`alkera_cli.files.live_peer.LivePeer`), which reads the document over
REST under the lease's fence and writes it to disk, on a thread of its own.
"""

from __future__ import annotations

import asyncio
import logging
import os
import stat as stat_module
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal, Protocol

from alkera_cli.cloud.folder import HeldFolder
from alkera_cli.cloud.transport import LaterAnswer
from alkera_cli.files.live_sync import LiveSync
from alkera_cli.files.target import ContainmentError, MaterializationTarget

logger = logging.getLogger(__name__)

__all__ = [
    "LIVE_TEXT",
    "MACHINE_ACK",
    "MACHINE_REQUEST",
    "NOTEBOOK",
    "PROMOTE_INFLIGHT",
    "PROMOTE_PER_MINUTE",
    "FolderLookup",
    "MachineRequests",
    "Outcome",
    "PromoteRequest",
    "parse_request",
]

#: The frame the drive sends, and the frame this box answers with.
MACHINE_REQUEST: Final = "machine.request"
MACHINE_ACK: Final = "machine.ack"
#: The kind of request that says a file's live document moved.
LIVE_TEXT: Final = "live_text"
#: The kind of request a notebook's engine on this box answers (its answer
#: travels as an event on the notebook's events route, never as an ack here).
NOTEBOOK: Final = "notebook"

#: How many promotions this machine carries at once, across every folder it
#: holds. A reader clicking through a folder is served; a scripted walk of a
#: thousand files is told this box is busy rather than queued behind itself.
PROMOTE_INFLIGHT: Final = 4

#: How many promotions one lease may ask for in a minute — the drive's own
#: ceiling, mirrored here so a drive that miscounts cannot turn the burst
#: into a second bandwidth window.
PROMOTE_PER_MINUTE: Final = 600

Outcome = Literal["accepted", "not_holder", "missing", "changed", "busy"]
FlushOutcome = Literal["flushed", "busy"]


@dataclass(frozen=True, slots=True)
class PromoteRequest:
    """A ``machine.request`` of kind ``promote``, as far as this box reads one."""

    request_id: str
    lease_node_id: str
    node_id: str
    path: str
    expected_size: int
    expected_mtime_ns: int
    #: The lease epoch the drive holds this folder at, when the frame names
    #: one. A box still at an older epoch is not the holder the drive means.
    epoch: int | None = None


class FolderLookup(Protocol):
    """Where a lease's folder is found, and how it is pushed —
    :class:`ChatFolders` in production."""

    def held_by_lease_node(self, lease_node_id: str) -> HeldFolder | None: ...

    def push(self, chat_id: str) -> Any: ...


def _text(frame: Mapping[str, Any], key: str) -> str | None:
    value = frame.get(key)
    return value if isinstance(value, str) and value else None


def _count(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def parse_request(frame: Mapping[str, Any]) -> PromoteRequest | None:
    """The promote ``frame`` asks for, or ``None`` for anything that is not one.

    ``None`` is also the answer to a frame too malformed to answer: without a
    request id there is nobody to address a reply to, and without a path there
    is nothing to decide about.
    """
    if frame.get("t") != MACHINE_REQUEST:
        return None
    request_id = _text(frame, "request_id")
    kind = frame.get("kind")
    if kind != "promote":
        logger.warning(
            "machine channel: request %s is of a kind this box does not serve (%r); "
            "left unanswered",
            request_id,
            kind,
        )
        return None
    lease_node_id = _text(frame, "lease_node_id")
    node_id = _text(frame, "node_id")
    path = _text(frame, "path")
    expected = frame.get("expected")
    size = _count(expected.get("size")) if isinstance(expected, Mapping) else None
    mtime_ns = _count(expected.get("mtime_ns")) if isinstance(expected, Mapping) else None
    if (
        request_id is None
        or lease_node_id is None
        or node_id is None
        or path is None
        or size is None
        or mtime_ns is None
    ):
        logger.warning(
            "machine channel: a promote request %s is malformed; left unanswered", request_id
        )
        return None
    epoch = frame.get("epoch")
    return PromoteRequest(
        request_id=request_id,
        lease_node_id=lease_node_id,
        node_id=node_id,
        path=path,
        expected_size=size,
        expected_mtime_ns=mtime_ns,
        epoch=epoch if isinstance(epoch, int) and not isinstance(epoch, bool) else None,
    )


class MachineRequests:
    """This machine's side of the drive's requests.

    ``handle`` takes a decoded frame and returns the ack frame to send, or
    ``None`` when nothing is to be sent. Synchronous and quick: a stat and a
    containment walk, never a read of a file's bytes, so it is safe to run on
    the socket's loop.
    """

    def __init__(
        self,
        folders: FolderLookup,
        *,
        notebooks: Callable[[Mapping[str, Any]], None] | None = None,
        inflight: int = PROMOTE_INFLIGHT,
        per_minute: int = PROMOTE_PER_MINUTE,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._folders = folders
        self._notebooks = notebooks
        self._inflight = inflight
        self._per_minute = per_minute
        self._clock = clock
        #: The promotions this machine accepted and has not seen leave the
        #: queue, keyed by the sync and the path it was handed, with the lease
        #: the sync was holding the folder under.
        self._accepted: dict[tuple[int, str], tuple[str, LiveSync]] = {}
        #: When each lease's promotions were accepted, over the last minute.
        self._recent: dict[str, deque[float]] = {}

    def handle(self, frame: Mapping[str, Any]) -> dict[str, Any] | LaterAnswer | None:
        if frame.get("t") == MACHINE_REQUEST and frame.get("kind") == LIVE_TEXT:
            self.live_text(frame)
            return None
        if frame.get("t") == MACHINE_REQUEST and frame.get("kind") == NOTEBOOK:
            if self._notebooks is None:
                logger.warning("machine channel: a notebook request on a box that serves none")
            else:
                self._notebooks(frame)
            return None
        if frame.get("t") == MACHINE_REQUEST and frame.get("kind") == "flush":
            return self.flush(frame)
        request = parse_request(frame)
        if request is None:
            return None
        outcome, observed = self.promote(request)
        ack: dict[str, Any] = {
            "t": MACHINE_ACK,
            "request_id": request.request_id,
            "outcome": outcome,
        }
        if observed is not None:
            ack["observed"] = observed
        logger.info(
            "machine channel: promote %s of %s under lease %s: %s",
            request.request_id,
            request.path,
            request.lease_node_id,
            outcome,
        )
        return ack

    def flush(self, frame: Mapping[str, Any]) -> dict[str, Any] | LaterAnswer | None:
        """Answer a flush: ``not_holder`` at once, or ``accepted`` now and the
        push's own outcome once it has run."""
        request_id = _text(frame, "request_id")
        lease_node_id = _text(frame, "lease_node_id")
        if request_id is None or lease_node_id is None:
            logger.warning("machine channel: a flush request %s is malformed", request_id)
            return None
        epoch = frame.get("epoch")
        held = self._folders.held_by_lease_node(lease_node_id)
        sync = None if held is None else held.live
        if (
            held is None
            or (isinstance(epoch, int) and epoch != held.record.epoch)
            or (sync is not None and (sync.fenced or sync.record.epoch != held.record.epoch))
        ):
            logger.info(
                "machine channel: flush %s of lease %s: not_holder", request_id, lease_node_id
            )
            return {"t": MACHINE_ACK, "request_id": request_id, "outcome": "not_holder"}

        async def pushed() -> dict[str, Any]:
            outcome = await asyncio.to_thread(self._push, held)
            logger.info(
                "machine channel: flush %s of lease %s: %s", request_id, lease_node_id, outcome
            )
            return {"t": MACHINE_ACK, "request_id": request_id, "outcome": outcome}

        return LaterAnswer(
            now={"t": MACHINE_ACK, "request_id": request_id, "outcome": "accepted"},
            then=pushed(),
        )

    def _push(self, held: HeldFolder) -> FlushOutcome:
        """The folder's checkpoint push, under the lease the box holds it by."""
        try:
            summary = self._folders.push(held.chat_id)
        except Exception:
            logger.exception("machine channel: the flush push of %s failed", held.chat_id)
            return "busy"
        if summary is None or getattr(summary, "failed", None):
            return "busy"
        return "flushed"

    def promote(self, request: PromoteRequest) -> tuple[Outcome, dict[str, int] | None]:
        """Decide ``request`` and, when it is accepted, queue the file."""
        held = self._folders.held_by_lease_node(request.lease_node_id)
        if held is None:
            return "not_holder", None
        sync = held.live
        if sync is None or not self._current(held, sync, request):
            return "not_holder", None
        relative = self._inside_live_root(held, sync, request.path)
        if relative is None:
            return "missing", None
        target = self._resolve(sync, relative)
        if target is None:
            return "missing", None
        try:
            info = os.lstat(target)
        except OSError:
            return "missing", None
        if not stat_module.S_ISREG(info.st_mode):
            return "missing", None
        if (info.st_size, info.st_mtime_ns) != (request.expected_size, request.expected_mtime_ns):
            return "changed", {"size": info.st_size, "mtime_ns": info.st_mtime_ns}
        key = (id(sync), relative)
        if relative in sync.promotions:
            # Already on its way: the second reader joins the first, and the
            # machine's allowance is not spent twice on one file.
            self._accepted[key] = (request.lease_node_id, sync)
            return "accepted", None
        if not self._room(request.lease_node_id):
            return "busy", None
        sync.promote(relative)
        self._accepted[key] = (request.lease_node_id, sync)
        self._recent.setdefault(request.lease_node_id, deque()).append(self._clock())
        return "accepted", None

    def live_text(self, frame: Mapping[str, Any]) -> bool:
        """Hand a ``live_text`` notice to the text peer of the folder it names;
        ``False`` when no folder this box holds at that epoch has one."""
        lease_node_id, node_id = _text(frame, "lease_node_id"), _text(frame, "node_id")
        token, epoch = _text(frame, "token"), frame.get("epoch")
        held = None if lease_node_id is None else self._folders.held_by_lease_node(lease_node_id)
        sync = None if held is None else held.live
        if held is None or sync is None or sync.peer is None or node_id is None or token is None:
            return False
        if epoch != held.record.epoch or sync.record.epoch != held.record.epoch or sync.fenced:
            return False
        sync.peer.notify(node_id, token)
        return True

    @staticmethod
    def _current(held: HeldFolder, sync: LiveSync, request: PromoteRequest) -> bool:
        """Whether this box is the holder the drive is asking.

        Not when the drive names an epoch this box does not hold the folder
        at, not when the sync still writes under an epoch the folder has moved
        past, and not when the sync's own fence has closed — a holder that
        cannot prove the folder is its own writes nothing, promoted or not.
        """
        if request.epoch is not None and request.epoch != held.record.epoch:
            return False
        if sync.record.epoch != held.record.epoch:
            return False
        return not sync.fenced

    @staticmethod
    def _inside_live_root(held: HeldFolder, sync: LiveSync, path: str) -> str | None:
        """``path`` — spelled from the lease root, as the tree batch spelled it
        — as a path under the live root, or ``None`` when it is not under it.

        The live root is the working directory inside the leased folder, so
        the drive's path carries the step down to it first. A path that does
        not take that step names something the live sync does not watch and
        cannot send.
        """
        try:
            step = sync.root.relative_to(held.root).as_posix()
        except ValueError:
            return None
        step = "" if step == "." else step.strip("/")
        if not step:
            return path
        prefix = f"{step}/"
        if not path.startswith(prefix):
            return None
        return path[len(prefix) :]

    @staticmethod
    def _resolve(sync: LiveSync, relative: str) -> Path | None:
        """Where ``relative`` is under the live root, proven to stay there.

        Refused lexically before any I/O (absolute, ``..``, empty segments,
        NUL) and then walked with every component that exists proven a real
        directory, never a link — so a link planted in the tree names nothing.
        """
        try:
            return MaterializationTarget(sync.root).resolve(
                relative.encode("utf-8", "surrogateescape")
            )
        except (ContainmentError, OSError):
            return None

    def _still_sending(self, lease_node_id: str, sync: LiveSync) -> bool:
        """Whether ``sync`` can still upload what it was promoted: it is the
        live sync of a folder this box holds, and its fence is open. A sync the
        box let go of, or one that lost the folder, never sends its queue, so
        what it carried is not in flight."""
        held = self._folders.held_by_lease_node(lease_node_id)
        return held is not None and held.live is sync and not sync.fenced

    def _room(self, lease_node_id: str) -> bool:
        """Whether one more promotion fits in this machine's bounds."""
        for key, (lease_node_id_of, sync) in list(self._accepted.items()):
            if key[1] not in sync.promotions or not self._still_sending(lease_node_id_of, sync):
                del self._accepted[key]
        if len(self._accepted) >= self._inflight:
            return False
        recent = self._recent.setdefault(lease_node_id, deque())
        now = self._clock()
        while recent and now - recent[0] >= 60.0:
            recent.popleft()
        return len(recent) < self._per_minute
