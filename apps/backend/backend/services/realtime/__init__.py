"""The realtime layer behind the event stream and the socket gateway.

``runtime`` is what the lifespan starts (the per-application hub and the outbox
listener); ``filters`` decides who may see which event; ``limits`` caps live
connections; ``sse`` encodes the event-stream text. The route modules under
``backend.api.routes`` are thin over these.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Mapping, Sequence
from types import ModuleType
from typing import TYPE_CHECKING, Any, Final, cast

from alkera_core.files.lease_snapshots import HeldLease
from alkera_core.files.promotion import PromoteOutcome
from alkera_core.logging import get_logger

if TYPE_CHECKING:
    from backend.services.crdt import CrdtDocs
    from backend.services.realtime.channels import Channel as Channel
    from backend.services.realtime.channels import ChannelError as ChannelError
    from backend.services.realtime.channels import ChannelGrant as ChannelGrant
    from backend.services.realtime.channels import team_of_scope as team_of_scope
    from backend.services.realtime.doc_kinds import (
        REALTIME_DOC_KINDS as REALTIME_DOC_KINDS,
    )
    from backend.services.realtime.doc_kinds import RealtimeDocKind as RealtimeDocKind
    from backend.services.realtime.docsync import (
        STATE_SCHEMA_VERSION_KEY as STATE_SCHEMA_VERSION_KEY,
    )
    from backend.services.realtime.docsync import DocOpRejectedError as DocOpRejectedError
    from backend.services.realtime.docsync import DocScope as DocScope
    from backend.services.realtime.docsync import MachineSubject as MachineSubject
    from backend.services.realtime.docsync import Subject as Subject
    from backend.services.realtime.docsync import check_size as check_size
    from backend.services.realtime.filters import EntitlementSnapshot as EntitlementSnapshot
    from backend.services.realtime.presence import EPHEMERAL_ENTITY as EPHEMERAL_ENTITY
    from backend.services.realtime.presence import (
        EphemeralTooLargeError as EphemeralTooLargeError,
    )

log = get_logger(__name__)

#: Where an application keeps its realtime runtime (``application.state``).
STATE_ATTR: Final = "realtime"

#: How long a trash waits for the live documents under it to be written back.
FLUSH_BEFORE_TRASH_SECONDS: Final = 10.0

#: How long a trash waits for the machines holding folders under it to push
#: what they hold. Every machine is asked at once, so this bounds the trash.
FLUSH_HOLDERS_BEFORE_TRASH_SECONDS: Final = 30.0


def crdt_lane(application: Any) -> CrdtDocs | None:
    """The Loro lane an application's realtime runtime runs, or ``None``
    before its lifespan started it (or in an app without one)."""
    runtime = getattr(application.state, STATE_ATTR, None)
    lane = getattr(runtime, "crdt", None)
    return cast("CrdtDocs | None", lane)


async def _flush(
    crdt: CrdtDocs, org_id: uuid.UUID, node_id: uuid.UUID
) -> dict[str, tuple[int, int]]:
    """The live documents at or under ``node_id`` written back, and those
    that could not be reported. A session left holding edits is not lost
    with the trash: it is parked until a restore lets it write back."""
    flushed = await crdt.sessions.flush_report(org_id, node_id)
    if flushed.unsaved:
        log.warning(
            "realtime.flush_before_trash_left_unsaved",
            node=str(node_id),
            docs=flushed.unsaved,
        )
    return flushed.moved


async def flush_before_trash(
    application: Any, org_id: uuid.UUID, node_id: uuid.UUID, if_match: int
) -> int:
    """Write back the live documents at or under ``node_id`` before it is
    trashed: edits people typed that are not on the drive yet would go with
    the session. The write is the session's own, made on the version the
    caller named, so a trash fenced on that version is fenced on the one the
    write produced (answered here). Bounded; never raises; an application
    without the live lane changes nothing. A document it could not write
    back is reported, and its session kept until a restore saves it."""
    crdt = crdt_lane(application)
    if crdt is None:
        return if_match
    try:
        async with asyncio.timeout(FLUSH_BEFORE_TRASH_SECONDS):
            moved = await _flush(crdt, org_id, node_id)
    except Exception as exc:
        log.warning("realtime.flush_before_trash_failed", error=str(exc))
        return if_match
    before_after = moved.get(str(node_id))
    if before_after is not None and before_after[0] == if_match:
        return int(before_after[1])
    return if_match


async def flush_many_before_trash(
    application: Any, org_id: uuid.UUID, if_match: Mapping[uuid.UUID, int]
) -> dict[uuid.UUID, int]:
    """:func:`flush_before_trash` for a batch: the live documents at or under
    every node in ``if_match`` are written back, within one bound for the
    batch, and each node is answered the etag its trash is fenced on (a node
    whose file another node's write back moved, a file in a trashed folder,
    included). The caller runs it before its own transaction opens: the write
    backs take file rows in transactions of their own, and a batch holding its
    locks across them could wait on them while they waited on it."""
    crdt = crdt_lane(application)
    fenced = dict(if_match)
    if crdt is None or not fenced:
        return fenced
    moved: dict[str, tuple[int, int]] = {}
    try:
        async with asyncio.timeout(FLUSH_BEFORE_TRASH_SECONDS):
            for node_id in if_match:
                moved.update(await _flush(crdt, org_id, node_id))
    except Exception as exc:
        log.warning("realtime.flush_before_trash_failed", error=str(exc))
    for node_id, etag in if_match.items():
        before_after = moved.get(str(node_id))
        if before_after is not None and before_after[0] == etag:
            fenced[node_id] = int(before_after[1])
    return fenced


async def flush_holders(application: Any, holders: Sequence[HeldLease]) -> int:
    """Ask each machine in ``holders`` to push its folder before a trash takes
    it, and wait (bounded) for each to say it has. Answers how many did.

    A trash ends the leases inside what it trashes and fences whatever their
    machines send afterwards, so work a box had not pushed yet would go into
    the trash missing from it. Pushed first, it is in the trashed tree and a
    restore brings it back. The trash is never refused for a machine: one that
    does not answer costs the ack window, and an application without the live
    lane asks nobody. Never raises; the caller holds no connection meanwhile."""
    runtime = getattr(application.state, STATE_ATTR, None)
    promoter = getattr(runtime, "promoter", None)
    if not holders or promoter is None:
        return 0
    try:
        outcomes = await asyncio.gather(
            *(
                promoter.flush(holder, deadline=FLUSH_HOLDERS_BEFORE_TRASH_SECONDS)
                for holder in holders
            )
        )
    except Exception as exc:
        log.warning("realtime.flush_holders_failed", error=str(exc))
        return 0
    flushed = sum(1 for outcome in outcomes if outcome is PromoteOutcome.LANDED)
    if flushed != len(holders):
        log.warning(
            "realtime.flush_holders_incomplete",
            holders=len(holders),
            flushed=flushed,
            outcomes=[str(outcome) for outcome in outcomes],
        )
    return flushed


#: Names other domains read from this package, and the submodule that owns
#: each. A name resolves on first use, so importing the package never imports
#: those submodules (they import the realtime runtime's collaborators).
_OWNERS: dict[str, str] = {
    "Channel": "backend.services.realtime.channels",
    "ChannelError": "backend.services.realtime.channels",
    "ChannelGrant": "backend.services.realtime.channels",
    "DocOpRejectedError": "backend.services.realtime.docsync",
    "DocScope": "backend.services.realtime.docsync",
    "EPHEMERAL_ENTITY": "backend.services.realtime.presence",
    "EntitlementSnapshot": "backend.services.realtime.filters",
    "EphemeralTooLargeError": "backend.services.realtime.presence",
    "MachineSubject": "backend.services.realtime.docsync",
    "REALTIME_DOC_KINDS": "backend.services.realtime.doc_kinds",
    "RealtimeDocKind": "backend.services.realtime.doc_kinds",
    "STATE_SCHEMA_VERSION_KEY": "backend.services.realtime.docsync",
    "Subject": "backend.services.realtime.docsync",
    "check_size": "backend.services.realtime.docsync",
    "team_of_scope": "backend.services.realtime.channels",
}


def __getattr__(name: str) -> Any:
    owner = _OWNERS.get(name)
    if owner is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(_owner_module(owner), name)


def _owner_module(owner: str) -> ModuleType:
    """Import ``owner`` through a literal import (the compiled CLI build
    follows only literal imports)."""
    if owner == "backend.services.realtime.filters":
        from backend.services.realtime import filters

        return filters
    if owner == "backend.services.realtime.presence":
        from backend.services.realtime import presence

        return presence
    if owner == "backend.services.realtime.doc_kinds":
        from backend.services.realtime import doc_kinds

        return doc_kinds
    if owner == "backend.services.realtime.channels":
        from backend.services.realtime import channels

        return channels
    if owner == "backend.services.realtime.docsync":
        from backend.services.realtime import docsync

        return docsync
    raise AssertionError(f"no import for {owner}")


__all__ = [
    "EPHEMERAL_ENTITY",
    "FLUSH_BEFORE_TRASH_SECONDS",
    "FLUSH_HOLDERS_BEFORE_TRASH_SECONDS",
    "REALTIME_DOC_KINDS",
    "STATE_ATTR",
    "STATE_SCHEMA_VERSION_KEY",
    "Channel",
    "ChannelError",
    "ChannelGrant",
    "DocOpRejectedError",
    "DocScope",
    "EntitlementSnapshot",
    "EphemeralTooLargeError",
    "MachineSubject",
    "RealtimeDocKind",
    "Subject",
    "check_size",
    "crdt_lane",
    "flush_before_trash",
    "flush_holders",
    "flush_many_before_trash",
    "team_of_scope",
]
