"""The folder-lease family: take a folder, hold it, push into it, hand it back.

Every route here is the same four steps in the same order — resolve the
principal, authorize through :func:`alkera_core.files.authz.authorize`, call
:class:`alkera_core.files.leases.LeaseService`, render the library's answer —
so the "not yours" contract holds by construction: the policy runs before any
branch, and a lease refusal can only ever be reached by a caller the policy
already let through.

The one deliberate asymmetry is the heartbeat. It carries no ``Idempotency-Key``
because it is state-based: replaying a beat is the point of a beat, and a client
that had to mint a key per second would be minting one per second for nothing.
``backend.api.deps.files.idempotency`` exempts it by path suffix, so the exemption is a property
of the route's URL rather than something this module can forget to declare.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import uuid
import zlib
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Annotated, Any, Literal, TypeVar

from alkera_core.authz.decision import MAX_AUDITED_STRING
from alkera_core.authz.enums import Action, ResourceType
from alkera_core.authz.resource import Resource
from alkera_core.config import get_settings
from alkera_core.files import leases as leases_lib
from alkera_core.files.authz.actions import FilesAction
from alkera_core.files.authz.authorize import Authorized, Denied, authorize
from alkera_core.files.authz.decider import CHAT_SUBTYPE, WORKSPACE_SUBTYPE, AccessFacts
from alkera_core.files.clock import SystemClock
from alkera_core.files.db_retry import with_db_retries
from alkera_core.files.errors import InvalidRequest, NotFound, TooLarge
from alkera_core.files.ids import DriveId, NodeId
from alkera_core.files.lease_live import LiveEntriesService, LiveReport
from alkera_core.files.lease_snapshots import DEFAULT_SYNC_INTERVAL
from alkera_core.files.lease_tree import LeaseTreeService, TreeChange
from alkera_core.files.repo import FilesRepo
from alkera_core.objects import chat_end
from alkera_core.project.local_state import is_local_state
from alkera_core.schemas.files.lease_tree import (
    TREE_MAX_BODY_BYTES,
    DigestAnswer,
    DigestRequest,
    FolderDigest,
    TreeBatch,
    TreeBatchAnswer,
    TreeEntry,
)
from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text

from backend.api.deps.files import (
    FILES_RETRY_BUDGET,
    Idempotency,
    IfMatch,
    Lease,
    as_platform,
    caller_drive,
    files_enforcer,
    idempotent_route,
    platform_wrap,
    ratelimited,
)
from backend.api.deps.files_context import FilesCtx
from backend.api.deps.files_facts import facts_for
from backend.api.params import PathId
from backend.services.files import beat_extends
from backend.services.files.context import FilesContext

T = TypeVar("T")

router = APIRouter(tags=["files"])

#: How often a holder is told to beat: several beats inside one TTL, so a single
#: lost beat never costs the lease.
HEARTBEATS_PER_TTL = 4


@dataclass(frozen=True, slots=True)
class _Final:
    """The final snapshot as the hook needs it: the changes and who is pushing."""

    instance_id: str
    changes: list[dict[str, Any]]


# ---------------------------------------------------------------------------
# The one authorize call every route in this module makes
# ---------------------------------------------------------------------------


async def _authorized(
    request: Request,
    ctx: FilesContext,
    node_id: uuid.UUID,
    action: FilesAction,
    facts: AccessFacts,
) -> Authorized[Any]:
    """Authorize ``action`` on ``node_id`` — the same statements whatever the
    answer, so a nonexistent node, another org's node and an unreadable node
    cost the same work and answer the same 404.

    ``facts`` is resolved by the caller through :func:`_facts`, inside the Files
    transaction and as the platform: the session runs as the restricted Files
    role, which cannot read the membership tables the resolver walks, and the
    decision row steps out of that role the same way.
    """
    return await authorize(
        ctx.ctx,
        ctx.repo,
        NodeId(node_id),
        action,
        facts=facts,
        enforce=files_enforcer(request, ctx.repo.session, wrap=platform_wrap(ctx.repo.session)),
    )


def _kind_of(node: Any) -> str:
    """What a folder is to the lease-kind policy: a native workspace's, a
    chat's, or anything else. The object behind it decides, never its name."""
    if node.target_object_id is not None and node.subtype == WORKSPACE_SUBTYPE:
        return "workspace"
    if node.target_object_id is not None and node.subtype == CHAT_SUBTYPE:
        return "chat"
    return "plain"


async def _decide_lease_kind(
    request: Request,
    ctx: FilesContext,
    handle: Authorized[Any],
    facts: AccessFacts,
    action: Action,
    *,
    purpose: str,
    readable: bool,
    holder_purpose: str = "",
    reason: str = "",
) -> None:
    """Decide the kind of lease (``files.lease_kind``) once the ladder has
    admitted the caller to the verb: a workspace lease is its bound box's, a
    person's lease in a workspace takes Full access, and a box's lease is
    forced off only by the owner or an org admin with a reason. On record
    either way; a refusal raises."""
    node = handle.node
    kinds = [_kind_of(ancestor) for ancestor in reversed(handle.chain)]
    inside = next((kind for kind in kinds if kind != "plain"), "plain")
    kind = _kind_of(node)
    if kind == "workspace":
        runs_here = node.id in facts.workspaces_run_here
    elif ctx.ctx.is_machine:
        # A box on its own credential runs only the chats bound to it: it
        # holds no rung a person granted, so a chat no box is bound to is
        # not its to take.
        runs_here = node.id in facts.chats_run_here
    else:
        # A box on its operator's session takes a chat unless it is bound to
        # another machine: the operator's own rung admitted it to the folder.
        runs_here = node.id not in facts.chats_bound_elsewhere
    allowed = handle.access.allowed_actions
    enforce = files_enforcer(request, ctx.repo.session, wrap=platform_wrap(ctx.repo.session))
    outcome = enforce(
        ctx.ctx,
        action,
        Resource(ResourceType.FILE_LEASE, id=str(node.id), org_id=node.org_team_id),
        {
            "purpose": purpose,
            "node_kind": kind,
            "inside_kind": inside,
            "readable": readable,
            "machine_verified": facts.agent_machine_id is not None,
            "runs_here": facts.agent_machine_id is not None and runs_here,
            "full_access": FilesAction.LEASE_FORCE.value in allowed,
            "owner_rung": FilesAction.HOLD.value in allowed,
            "org_admin": facts.org_admin,
            "holder_purpose": holder_purpose,
            "reason_given": bool(reason.strip()),
            "reason": reason.strip()[:FORCE_REASON_RECORDED],
        },
    )
    if inspect.isawaitable(outcome):
        await outcome  # the enforcer raises the refusal; an allow is on record


async def _facts(request: Request, ctx: FilesContext) -> AccessFacts:
    """The caller's facts, read as the platform from inside the transaction."""
    async with as_platform(ctx.repo.session):
        return await facts_for(request, ctx.repo.session, ctx.ctx, ctx.drive, now=ctx.clock.now())


async def _may_read(
    request: Request, ctx: FilesContext, node_id: uuid.UUID, facts: AccessFacts
) -> bool:
    """Whether this caller may read ``node_id`` — the fact that decides whether
    a 409 names its holder.

    It is its own authorize rather than a reuse of the caller's LEASE decision
    because the two are different questions about the same node: answering "may
    they know who holds it?" with "they asked to lease it" would name a holder
    to someone who may not read the folder at all.
    """
    try:
        await _authorized(request, ctx, node_id, FilesAction.READ, facts)
    except (NotFound, Denied):
        return False
    return True


# ---------------------------------------------------------------------------
# The snapshot seam
# ---------------------------------------------------------------------------


async def _apply_final(
    repo: FilesRepo, node_id: NodeId, *, lease: leases_lib.LeaseContext, changes: Any
) -> None:
    """The exporter's batch, fenced at the start and again at the commit.

    What a change *means* to the tree belongs to the snapshot service; what this
    hook owns is the part the lease family owns and a client can observe: the
    batch runs under the holder's epoch, and ``last_sync_at`` advances only when
    it does, so "last synced 12 s ago" can never be later than the last batch
    that actually landed.

    The whole batch runs under ``lease`` — the context whoever called this built,
    and the fence it is checked against. Only :meth:`LeaseService.release` builds
    one carrying ``final``, so only what a release applies is spared the drive's
    ceilings; the exporter's mid-lease batch below is bounded like any other
    write, and no request can reach either decision.
    """
    if lease.epoch is None or not lease.instance_id:
        raise InvalidRequest(
            "files.bad_lease_headers",
            "A snapshot is fenced: send the lease epoch and instance",
        )
    epoch, instance = lease.epoch, lease.instance_id
    holder = lease.holder
    # At update strength: the stamp below updates the row this fence locks.
    await leases_lib.assert_lease_epoch(
        repo, node_id, epoch, instance, holder=holder, lock="no key update"
    )
    await repo.session.execute(
        text(
            "UPDATE file_leases SET last_sync_at = now() "
            "WHERE node_id = :node AND org_team_id = :org AND epoch = :epoch "
            "AND holder_instance_id = :instance "
            "AND released_at IS NULL AND expires_at > now()"
        ),
        {
            "node": node_id,
            "org": repo.scope.org_team_id,
            "epoch": epoch,
            "instance": instance,
        },
    )
    # Fenced again after the writes: a reap that committed while the batch ran
    # must lose the whole batch, not silently keep half of it. Same strength as
    # the first fence — the row is already held, so this re-takes it, never
    # weakens it.
    await leases_lib.assert_lease_epoch(
        repo, node_id, epoch, instance, holder=holder, lock="no key update"
    )


def _drive(ctx: FilesContext, drive_id: uuid.UUID) -> DriveId:
    """The drive the caller named, or the opaque 404.

    A drive id that is not this org's own is indistinguishable, from outside,
    from one that does not exist, so every drive-addressed route here resolves
    it before it looks at anything else it was sent.
    """
    if ctx.drive.id != drive_id:
        raise NotFound()
    return DriveId(ctx.drive.id)


def _context(ctx: FilesContext, epoch: int, instance: str) -> leases_lib.LeaseContext:
    """The fencing context for a batch this route applies on the caller's word.

    Carries who is asking as well as which lease they name: the epoch and the
    instance are derivable from what a chat serves, so on their own they would
    let anybody who may write the folder push a snapshot as its holder. Who is
    asking is the identity the server derived — never the agent id the request
    asserted, which is a header and a public one at that.
    """
    return leases_lib.LeaseContext(epoch=epoch, instance_id=instance, holder=ctx.holder)


def _service(ctx: FilesContext) -> leases_lib.LeaseService:
    """The lease library, serving the deployment's own fencing numbers.

    Both halves of the configuration come from one place: the TTL a grant lives
    by and the delay a lapsed folder stays off the next asker. A deployment that
    gives a slow holder a minute to come back must not have the route hand its
    folder away in the library's default five seconds.
    """
    return leases_lib.LeaseService(
        ctx.repo,
        ctx.ctx,
        ctx.clock,
        grant_delay=timedelta(seconds=ctx.settings.files_lease_grant_delay_seconds),
        apply_final=_apply_final,
        verified_machine_id=ctx.agent_machine_id,
    )


def _live_entries(ctx: FilesContext) -> LiveEntriesService:
    """The plane, fenced on the same identity every other statement uses."""
    return LiveEntriesService(ctx.repo, ctx.ctx, verified_machine_id=ctx.agent_machine_id)


async def _replayed(op: Callable[[], Coroutine[Any, Any, T]]) -> T:
    """Run one live-plane transaction, replaying it on ``40001`` / ``40P01``.

    The live routes take no idempotency key (a report is idempotent by
    construction, the drain is a read), so they are outside the ladder
    ``idempotent_route`` runs every keyed Files write under. Both fence on the
    lease row, which the holder's heartbeats, reports and tree batches all
    lock, so a drain can be the waiter Postgres picks as a deadlock's victim
    even though it holds nothing else: the transaction was rolled back whole,
    and the answer is to run it again, not a 500.
    """
    return await with_db_retries(
        op, budget=FILES_RETRY_BUDGET, clock=SystemClock(), sleep=asyncio.sleep
    )


# ---------------------------------------------------------------------------
# Wire shapes (in-flight only — nothing here is persisted)
# ---------------------------------------------------------------------------


#: How much of a force-release reason goes on the decision row: as much as
#: the record keeps of any string.
FORCE_REASON_RECORDED = MAX_AUDITED_STRING
#: The widest holder instance or machine id a lease row stores.
HOLDER_ID_MAX = 128
#: The lease purposes a box holds for the chats it serves.
_BOX_LEASES = frozenset({"chat", "workspace"})


class ForceBody(BaseModel):
    """Why a lease is being forced off. Required for a box's lease (a chat's or
    a workspace's): the owner or an org admin cuts off a turn in flight, and
    the reason goes on the decision row."""

    reason: str = Field(default="", max_length=500)


class AcquireBody(BaseModel):
    """What a holder asks for when it takes a folder.

    ``inbound`` is "take what other people write here and keep it for me to
    apply" and ``live`` is "I will run the live plane over this folder": either
    one turns the folder's live plane on, because a folder that keeps drops
    nobody drains and a holder that drains a folder keeping none are the same
    mistake told from opposite ends. A holder that asks for neither gets what
    its purpose means — a chat's or a workspace's lease takes drops, because that is why the box
    holds the folder at all; a mount does not, because the machine owns that
    tree outright.
    """

    instance_id: str = Field(min_length=1, max_length=HOLDER_ID_MAX, alias="instanceId")
    machine_id: str = Field(min_length=1, max_length=HOLDER_ID_MAX, alias="machineId")
    purpose: leases_lib.LeasePurpose = "mount"
    ttl: int | None = Field(default=None, ge=1, le=3600)
    inbound: bool = False
    live: bool = False
    #: The holder is re-taking a lease it believes is still its own, after a
    #: fenced beat: granted after a lapse or a reap, refused as
    #: ``files.lease_ended`` when the server ended it.
    retake: bool = False

    model_config = {"populate_by_name": True}


class HeartbeatBody(BaseModel):
    epoch: int = Field(ge=0)
    instance_id: str = Field(min_length=1, max_length=HOLDER_ID_MAX, alias="instanceId")

    model_config = {"populate_by_name": True}


#: How many leases one batched beat may name. A box holds one lease per chat,
#: so this is the number of chats a single machine can keep in one call; past
#: it the box splits the batch.
HEARTBEAT_BATCH_MAX = 1000


class HeartbeatBatchEntry(BaseModel):
    node_id: uuid.UUID = Field(alias="nodeId")
    epoch: int = Field(ge=0)
    instance_id: str = Field(min_length=1, max_length=HOLDER_ID_MAX, alias="instanceId")
    #: The holder's live plane is running: a renewed lease is stamped synced,
    #: as an empty live batch would have stamped it.
    synced: bool = False

    model_config = {"populate_by_name": True}


class HeartbeatBatchBody(BaseModel):
    leases: list[HeartbeatBatchEntry] = Field(max_length=HEARTBEAT_BATCH_MAX)


#: How many of the paths still queued at a release the holder may name.
RELEASE_MAX_UNSYNCED_PATHS = 200


class ReleaseBody(BaseModel):
    """The hand-back. ``unsyncedCount`` is how many files the holder still had
    queued when its drain ran out, and ``unsyncedPaths`` names up to
    :data:`RELEASE_MAX_UNSYNCED_PATHS` of them; the count is recorded on the
    lease, the paths are the holder's report and are not stored."""

    epoch: int = Field(ge=0)
    instance_id: str = Field(min_length=1, max_length=HOLDER_ID_MAX, alias="instanceId")
    final: list[dict[str, Any]] | None = None
    unsynced_count: int | None = Field(default=None, ge=0, alias="unsyncedCount")
    unsynced_paths: list[str] = Field(
        default_factory=list, max_length=RELEASE_MAX_UNSYNCED_PATHS, alias="unsyncedPaths"
    )
    #: Set when the hand-back ENDS the chat the folder belongs to (the box put
    #: it to sleep): the release runs the chat-end transition in the same
    #: transaction, so the chat reads asleep the instant the lease is gone.
    ending: Literal["idle", "evicted", "drained"] | None = None

    model_config = {"populate_by_name": True}


class LiveInboundEntry(BaseModel):
    """One write the drive took into the holder's folder on its behalf."""

    node_id: str = Field(alias="nodeId")
    state: str
    seq: int

    model_config = {"populate_by_name": True}


class LiveInboundPage(BaseModel):
    entries: list[LiveInboundEntry]


class LiveReportBody(BaseModel):
    """One node as the holder reports it: a stored state, or ``applied`` /
    ``superseded`` to say the difference the drive recorded is gone."""

    node_id: str = Field(alias="nodeId")
    state: str = Field(min_length=1)
    box_size: int | None = Field(default=None, alias="boxSize", ge=0)
    box_mtime: datetime | None = Field(default=None, alias="boxMtime")
    #: On an ``applied`` report of an inbound write the holder's disk had
    #: diverged from: the name the drive chose for the conflicted copy its own
    #: displaced bytes were filed under.
    displaced: str | None = Field(default=None, min_length=1, max_length=1024)

    model_config = {"populate_by_name": True}


class LiveBatchBody(BaseModel):
    """One live batch, at most ``files_live_max_batch_entries`` entries long.

    The same number the grant hands the holder as ``maxBatchEntries``, read
    here rather than compiled in so a deployment that lowers it lowers what it
    will accept in the same breath. A batch is one statement per entry inside
    one transaction holding the lease row, so an unbounded list is an unbounded
    transaction — and the holder is told it sent too many rather than having
    them applied.
    """

    entries: list[LiveReportBody]

    @field_validator("entries")
    @classmethod
    def _within_the_batch_ceiling(cls, entries: list[LiveReportBody]) -> list[LiveReportBody]:
        ceiling = get_settings().files_live_max_batch_entries
        if len(entries) > ceiling:
            raise ValueError(f"a live batch names at most {ceiling} entries")
        return entries


class LiveBatchAnswer(BaseModel):
    live_seq: int = Field(alias="liveSeq")
    #: What the holder still has to apply after this batch.
    pending: int

    model_config = {"populate_by_name": True}


class SnapshotBody(BaseModel):
    epoch: int | None = None
    instance_id: str | None = Field(default=None, alias="instanceId")
    changes: list[dict[str, Any]] = Field(default_factory=list)

    model_config = {"populate_by_name": True}


class LiveCadenceWire(BaseModel):
    """How the holder is told to run the live plane: how long to wait for a file
    to stop changing, how often to report, and the three ceilings past which it
    must skeleton or defer rather than upload.

    Served rather than compiled into the client for the same reason the two
    lease cadences are: a deployment that has to slow the plane down — a busy
    org, a store under pressure — changes a setting instead of waiting for
    every box in the field to update. ``inbound`` is what this folder does with
    a write from someone who is not the holder, so a client reads whether to
    drain at all off the same block it reads its cadence from.
    """

    debounce_ms: int = Field(serialization_alias="debounceMs")
    batch_every_ms: int = Field(serialization_alias="batchEveryMs")
    max_batch_entries: int = Field(serialization_alias="maxBatchEntries")
    max_file_bytes: int = Field(serialization_alias="maxFileBytes")
    bandwidth_bytes_per_minute: int = Field(serialization_alias="bandwidthBytesPerMinute")
    max_pending_entries: int = Field(serialization_alias="maxPendingEntries")
    inbound: bool
    #: The tree report's cadence: how long a change waits for the ones after
    #: it, how many entries one report may name, and the body size past which
    #: the holder gzips it.
    metadata_every_ms: int = Field(serialization_alias="metadataEveryMs")
    metadata_max_entries: int = Field(serialization_alias="metadataMaxEntries")
    metadata_gzip_bytes: int = Field(serialization_alias="metadataGzipBytes")
    #: How long a holder handing the folder back keeps landing queued bytes
    #: before it releases and reports what is left.
    release_drain_ms: int = Field(serialization_alias="releaseDrainMs")

    model_config = {"populate_by_name": True, "serialize_by_alias": True}


class LeaseGrant(BaseModel):
    """What a holder is handed: the epoch it writes under and the cadences it
    must keep. The cadences are served rather than compiled into the client
    so a deployment can slow them down without shipping a new one.

    ``inboundPending`` rides every grant — the acquire AND every beat — so a
    holder learns that a person dropped a file into the chat without asking a
    second question at its own cadence: the beat it already sends is the poll.
    """

    epoch: int
    expires_at: datetime = Field(serialization_alias="expiresAt")
    heartbeat_every: float = Field(serialization_alias="heartbeatEvery")
    sync_interval: float = Field(serialization_alias="syncInterval")
    forced: bool = False
    live: LiveCadenceWire
    inbound_pending: int = Field(default=0, serialization_alias="inboundPending")

    model_config = {"populate_by_name": True, "serialize_by_alias": True}


class LeaseRow(BaseModel):
    """One of my leases, as ``alkera files mounts`` lists them."""

    node_id: str = Field(serialization_alias="nodeId")
    epoch: int
    machine: str
    purpose: str
    since: datetime
    expires_at: datetime = Field(serialization_alias="expiresAt")
    last_sync_at: datetime | None = Field(default=None, serialization_alias="lastSyncAt")

    model_config = {"populate_by_name": True, "serialize_by_alias": True}


def _live_cadence(ctx: FilesContext, lease: leases_lib.Lease) -> LiveCadenceWire:
    return LiveCadenceWire(
        debounce_ms=ctx.settings.files_live_debounce_ms,
        batch_every_ms=ctx.settings.files_live_batch_ms,
        max_batch_entries=ctx.settings.files_live_max_batch_entries,
        max_file_bytes=ctx.settings.files_live_max_file_bytes,
        bandwidth_bytes_per_minute=ctx.settings.files_live_bandwidth_bytes_per_minute,
        max_pending_entries=ctx.settings.files_live_max_pending_entries,
        inbound=lease.accepts_inbound,
        metadata_every_ms=ctx.settings.files_live_metadata_every_ms,
        metadata_max_entries=ctx.settings.files_live_metadata_max_entries,
        metadata_gzip_bytes=ctx.settings.files_live_metadata_gzip_bytes,
        release_drain_ms=ctx.settings.files_release_drain_seconds * 1000,
    )


def _heartbeat_every(ctx: FilesContext, ttl: timedelta) -> float:
    """How often a holder of a grant that lives ``ttl`` must beat to keep it.

    The deployment names a cadence, but it may only make the beat faster: the
    TTL-derived value is a ceiling, so a holder always gets
    :data:`HEARTBEATS_PER_TTL` chances inside one window however the knob is
    set, and a fleet that wants tighter detection than that spells it.
    """
    return min(
        float(ctx.settings.files_lease_heartbeat_seconds),
        ttl.total_seconds() / HEARTBEATS_PER_TTL,
    )


def _grant(
    ctx: FilesContext,
    lease: leases_lib.Lease,
    ttl: timedelta,
    *,
    live: LiveCadenceWire | None = None,
    inbound_pending: int = 0,
) -> LeaseGrant:
    return LeaseGrant(
        epoch=lease.epoch,
        expires_at=lease.expires_at,
        heartbeat_every=_heartbeat_every(ctx, ttl),
        sync_interval=DEFAULT_SYNC_INTERVAL.total_seconds(),
        forced=lease.forced,
        live=live if live is not None else _live_cadence(ctx, lease),
        inbound_pending=inbound_pending,
    )


async def _stamp_cadence(
    ctx: FilesContext, lease: leases_lib.Lease, cadence: LiveCadenceWire
) -> None:
    """Record on the lease row the cadence this grant served, or clear it.

    A reader asks the lease row whether the folder is being streamed, and the
    presence of the cadence block IS that answer — a lease cannot be live and
    have no numbers to run at. It is written here rather than by the library
    because the numbers are the deployment's, read off the settings the route
    already holds; cleared on a grant that runs no plane, so a folder re-taken
    as a plain mount stops claiming a stream the holder is not running.
    """
    await ctx.repo.session.execute(
        text(
            "UPDATE file_leases SET live_cadence = CAST(:block AS jsonb) "
            "WHERE org_team_id = :org AND node_id = :node AND epoch = :epoch"
        ),
        {
            "block": cadence.model_dump_json() if lease.accepts_inbound else "{}",
            "org": ctx.repo.scope.org_team_id,
            "node": lease.node_id,
            "epoch": lease.epoch,
        },
    )


async def _inbound_pending(ctx: FilesContext, lease: leases_lib.Lease) -> int:
    """How much the holder still owes on this folder.

    Counted for a lease that takes drops at all, so a mount pays nothing for a
    plane it does not run. The read is fenced by the lease's own epoch and
    instance — the ones the row just handed back — so it can only ever count
    what this holder is the one to apply.
    """
    if not lease.accepts_inbound:
        return 0
    owed = await _live_entries(ctx).inbound_for_holder(
        lease.node_id, epoch=lease.epoch, instance_id=lease.holder_instance_id
    )
    return len(owed)


def _row(lease: leases_lib.Lease) -> LeaseRow:
    return LeaseRow(
        node_id=str(lease.node_id),
        epoch=lease.epoch,
        machine=lease.machine_id,
        purpose=lease.purpose,
        since=lease.acquired_at,
        expires_at=lease.expires_at,
        last_sync_at=lease.last_sync_at,
    )


def _reported(entries: list[LiveReportBody]) -> list[LiveReport]:
    """The batch as the service takes it: one entry per node, last word wins.

    A holder that named the same file twice in one batch said two things about
    it and means the later one — writing both would be the same row written
    twice, and counting both would let a batch of one file repeated spend the
    ceiling on entries in flight, which is a ceiling on NODES. Order is kept
    otherwise, so the sequence the holder reported in is the sequence applied.
    """
    landing: dict[NodeId, LiveReport] = {}
    for entry in entries:
        node = NodeId(uuid.UUID(entry.node_id))
        landing[node] = LiveReport(
            node_id=node,
            state=entry.state,
            box_size=entry.box_size,
            box_mtime=entry.box_mtime,
            displaced=entry.displaced,
        )
    return list(landing.values())


def _ttl(ctx: FilesContext, seconds: int | None = None) -> timedelta:
    """The TTL this grant lives by: what the caller asked for, else the
    deployment's ``files_lease_ttl_seconds``.

    One number decides both halves of the answer — how long the row lives and
    how often the holder is told to beat — so a deployment that lengthens the
    lease lengthens the cadence with it instead of serving a client a beat rate
    the statement disagrees with.
    """
    if seconds is not None:
        return timedelta(seconds=seconds)
    return timedelta(seconds=ctx.settings.files_lease_ttl_seconds)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

_ITEM = "/drives/{drive_id}/items/{item_id}"


@router.post(_ITEM + "/lease", dependencies=[Depends(ratelimited("leases"))])
@idempotent_route("files.leases.acquire_lease")
async def acquire_lease(
    request: Request,
    drive_id: uuid.UUID,
    item_id: uuid.UUID,
    body: AcquireBody,
    ctx: FilesCtx,
    key: Idempotency,
    etag: IfMatch,
) -> LeaseGrant:
    """Take the folder. 409 ``files.leased`` when anything above or below it is
    already held — naming the holder only to a caller who may read that node."""
    ttl = _ttl(ctx, body.ttl)
    async with ctx.repo.transaction():
        facts = await _facts(request, ctx)
        handle = await _authorized(request, ctx, item_id, FilesAction.LEASE, facts)
        readable = await _may_read(request, ctx, item_id, facts)
        await _decide_lease_kind(
            request, ctx, handle, facts, Action.LEASE, purpose=body.purpose, readable=readable
        )
        lease = await _service(ctx).acquire(
            NodeId(handle.node.id),
            instance_id=body.instance_id,
            machine_id=body.machine_id,
            purpose=body.purpose,
            ttl=ttl,
            can_read_holder=readable,
            accepts_inbound=True if body.inbound or body.live else None,
            retake=body.retake,
        )
        cadence = _live_cadence(ctx, lease)
        await _stamp_cadence(ctx, lease, cadence)
        pending = await _inbound_pending(ctx, lease)
    return _grant(ctx, lease, ttl, live=cadence, inbound_pending=pending)


@router.post(
    _ITEM + "/lease/heartbeat",
    dependencies=[Depends(ratelimited("leases.heartbeat", limit=1200))],
)
async def heartbeat_lease(
    request: Request,
    drive_id: uuid.UUID,
    item_id: uuid.UUID,
    body: HeartbeatBody,
    ctx: FilesCtx,
) -> LeaseGrant:
    """Keep the lease. Zero rows updated is 409 ``files.lease_fenced``: the
    holder was superseded and must stop writing."""
    ttl = _ttl(ctx)
    async with ctx.repo.transaction():
        facts = await _facts(request, ctx)
        handle = await _authorized(request, ctx, item_id, FilesAction.LEASE, facts)
        lease = await _service(ctx).heartbeat(
            NodeId(handle.node.id),
            epoch=body.epoch,
            instance_id=body.instance_id,
            ttl=ttl,
            extend=await _beat_extends(ctx, facts, handle),
        )
        pending = await _inbound_pending(ctx, lease)
    return _grant(ctx, lease, ttl, inbound_pending=pending)


async def _beat_extends(ctx: FilesContext, facts: AccessFacts, handle: Authorized[Any]) -> bool:
    """Whether this beat keeps the lease or forces it: a box the chat or the
    workspace moved off may finish, not stay. Read as the platform."""
    async with as_platform(ctx.repo.session):
        return await beat_extends(ctx.repo.session, ctx.ctx, facts, handle.node)


#: What one lease in a batched beat came to, in the per-lease route's terms:
#: ``renewed`` is its 200, ``superseded`` its 409 ``files.lease_fenced``, and
#: ``gone`` its 404 or 403 — a folder that no longer exists, or one this caller may
#: not lease.
BeatVerdict = Literal["renewed", "superseded", "gone"]


class HeartbeatBatchVerdict(BaseModel):
    node_id: str = Field(serialization_alias="nodeId")
    verdict: BeatVerdict
    grant: LeaseGrant | None = None

    model_config = {"populate_by_name": True, "serialize_by_alias": True}


class HeartbeatBatchAnswer(BaseModel):
    leases: list[HeartbeatBatchVerdict]


@router.post(
    "/drives/{drive_id}/leases/heartbeat",
    dependencies=[Depends(caller_drive), Depends(ratelimited("leases.heartbeat"))],
)
async def heartbeat_leases(
    request: Request,
    drive_id: uuid.UUID,
    body: HeartbeatBatchBody,
    ctx: FilesCtx,
) -> HeartbeatBatchAnswer:
    """Keep every lease a holder names in one call.

    A box holds one lease per chat, and one beat per lease every few seconds
    made the beats alone most of an idle box's traffic, growing with every chat
    it ran. Each lease is decided exactly as the per-lease route decides it —
    the same authorize, the same fenced statement, the same grant — and each in
    a transaction of its own, so a lease that is gone or superseded rolls back
    its own work and never costs the others their beat. Answered in the order
    asked.
    """
    ttl = _ttl(ctx)
    verdicts: list[HeartbeatBatchVerdict] = []
    async with ctx.repo.transaction():
        facts = await _facts(request, ctx)
    service = _service(ctx)
    for entry in body.leases:
        node = str(entry.node_id)
        try:
            async with ctx.repo.transaction():
                handle = await _authorized(request, ctx, entry.node_id, FilesAction.LEASE, facts)
                # The synced stamp rides the beat's own statement: a beat that
                # stamped the row in a second UPDATE took the leased folder's
                # key lock after the lease row, and deadlocked against the
                # holder's own push holding that folder.
                lease = await service.heartbeat(
                    NodeId(handle.node.id),
                    epoch=entry.epoch,
                    instance_id=entry.instance_id,
                    ttl=ttl,
                    synced=entry.synced,
                    extend=await _beat_extends(ctx, facts, handle),
                )
                pending = await _inbound_pending(ctx, lease)
        except leases_lib.LeaseConflict:
            verdicts.append(HeartbeatBatchVerdict(node_id=node, verdict="superseded"))
            continue
        except (NotFound, Denied):
            # A folder the caller may still read but no longer lease (a share
            # moved down, a write taken away) is the per-lease route's 403. It
            # belongs to this entry alone: raised out of the loop it answered
            # the whole batch 403, and every lease after it went unbeaten
            # until it lapsed.
            verdicts.append(HeartbeatBatchVerdict(node_id=node, verdict="gone"))
            continue
        verdicts.append(
            HeartbeatBatchVerdict(
                node_id=node,
                verdict="renewed",
                grant=_grant(ctx, lease, ttl, inbound_pending=pending),
            )
        )
    return HeartbeatBatchAnswer(leases=verdicts)


@router.get(_ITEM + "/lease/live", dependencies=[Depends(ratelimited("leases"))])
async def live_inbound(
    request: Request,
    drive_id: uuid.UUID,
    item_id: uuid.UUID,
    ctx: FilesCtx,
    lease: Lease,
    inbound: bool = Query(True),
) -> LiveInboundPage:
    """What the drive took into the holder's folder on its behalf — the files
    a person dropped into the chat while the box ran — oldest first, for the
    box to apply on its next drain.

    Read under the holder's fence the way a write is: a caller with no epoch,
    a superseded epoch or another instance is a stranger to the plane and is
    told so, never shown what is waiting. The inbound page is the only page of
    the plane this route serves; ``inbound`` is the flag the holder spells.
    """
    _ = inbound
    _drive(ctx, drive_id)
    epoch, instance = lease.epoch, lease.instance
    if epoch is None or instance is None:
        raise leases_lib.LeaseConflict("files.lease_fenced", "a newer epoch owns this subtree")

    async def drain() -> list[Any]:
        async with ctx.repo.transaction():
            facts = await _facts(request, ctx)
            handle = await _authorized(request, ctx, item_id, FilesAction.LEASE, facts)
            return await _live_entries(ctx).inbound_for_holder(
                NodeId(handle.node.id), epoch=epoch, instance_id=instance
            )

    entries = await _replayed(drain)
    return LiveInboundPage(
        entries=[
            LiveInboundEntry(nodeId=str(entry.node_id), state=entry.state, seq=entry.seq)
            for entry in entries
        ]
    )


@router.post(_ITEM + "/lease/live", dependencies=[Depends(ratelimited("leases.live"))])
async def live_batch(
    request: Request,
    drive_id: uuid.UUID,
    item_id: uuid.UUID,
    body: LiveBatchBody,
    ctx: FilesCtx,
    lease: Lease,
) -> LiveBatchAnswer:
    """The holder's word on what it is doing to each node — and, for what the
    drive took on its behalf, that it has applied it.

    ``applied`` and ``superseded`` clear the node's row, so the next drain does
    not offer it again: a re-offered drop would be downloaded a second time
    onto a file the agent may since have edited. Fenced like every write, and
    idempotent by construction — a batch the holder resends clears nothing
    twice. Not under an idempotency key because a heartbeat-cadence report
    that is refused for a missing key is worse than one applied twice.
    """
    _drive(ctx, drive_id)
    epoch, instance = lease.epoch, lease.instance
    if epoch is None or instance is None:
        raise leases_lib.LeaseConflict("files.lease_fenced", "a newer epoch owns this subtree")

    async def report() -> LiveBatchAnswer:
        async with ctx.repo.transaction():
            facts = await _facts(request, ctx)
            handle = await _authorized(request, ctx, item_id, FilesAction.SNAPSHOT, facts)
            service = _live_entries(ctx)
            node_id = NodeId(handle.node.id)
            try:
                seq = await service.upsert(
                    node_id, epoch=epoch, instance_id=instance, entries=_reported(body.entries)
                )
            except ValueError as exc:
                raise InvalidRequest("files.live_unknown_state", str(exc)) from exc
            owed = await service.inbound_for_holder(node_id, epoch=epoch, instance_id=instance)
        return LiveBatchAnswer(liveSeq=seq, pending=len(owed))

    return await _replayed(report)


# ---------------------------------------------------------------------------
# The tree report: a body that may arrive gzipped
# ---------------------------------------------------------------------------


class BadEncoding(InvalidRequest):
    """A body whose ``Content-Encoding`` does not decode: the request is
    malformed, not merely unprocessable, so it is a 400 rather than the 422
    the rest of the invalid-request family answers."""

    status = 400


def _batch_too_large() -> TooLarge:
    return TooLarge("files.batch_too_large", "split the report and send it again")


def _gunzip(raw: bytes, limit: int) -> bytes:
    """``raw`` decoded as ONE gzip member, never past ``limit`` bytes.

    The decoder is asked for at most one byte more than the limit, so a small
    body that inflates to gigabytes costs the limit and not the gigabytes, and
    a body that decodes to more than the limit is refused as too large rather
    than as malformed. A truncated stream, trailing bytes and a second member
    are all malformed: a holder sends one member, whole.
    """
    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        decoded = decoder.decompress(raw, limit + 1)
    except zlib.error:
        raise BadEncoding("files.bad_encoding", "the body is not valid gzip") from None
    if len(decoded) > limit:
        raise _batch_too_large()
    if not decoder.eof or decoder.unused_data:
        raise BadEncoding("files.bad_encoding", "the body is not one whole gzip member")
    return decoded


def decoded_tree_body(raw: bytes, content_encoding: str | None) -> bytes:
    """The tree report's body as JSON bytes: decoded, then held to the cap.

    The cap is on what the body DECODES to, whatever it arrived as, so the
    ceiling a holder is told about is the one that applies either way.
    """
    coding = (content_encoding or "").strip().lower()
    if coding in ("", "identity"):
        body = raw
    elif coding == "gzip":
        body = _gunzip(raw, TREE_MAX_BODY_BYTES)
    else:
        raise BadEncoding("files.bad_encoding", f"unsupported content encoding {coding!r}")
    if len(body) > TREE_MAX_BODY_BYTES:
        raise _batch_too_large()
    return body


class _DecodedRequest(Request):
    """A request whose body is read through :func:`decoded_tree_body`."""

    async def body(self) -> bytes:
        if not hasattr(self, "_decoded"):
            raw = await super().body()
            self._decoded = decoded_tree_body(raw, self.headers.get("content-encoding"))
            self._body = self._decoded
        return self._decoded


def _entries_too_long(error: Any) -> bool:
    """A report with more entries, or a digest request with more folders, than
    one request may carry: the holder's cue to split."""
    return error.get("type") == "too_long" and tuple(error.get("loc", ())) in (
        ("body", "entries"),
        ("body", "paths"),
        ("body", "names"),
    )


class _TreeReportRoute(APIRoute):
    """The one route whose body may be compressed.

    FastAPI parses a JSON body before any dependency runs, so the decoding has
    to happen beneath it: the route hands the handler a request whose body is
    the decoded one. And a report longer than the ceiling is the holder's cue
    to split, which is a 413 it acts on rather than the 422 a schema failure
    would otherwise be.
    """

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def decoded(request: Request) -> Response:
            wrapped = _DecodedRequest(request.scope, request.receive)
            # Decoded here, before FastAPI reads the body: its own read turns
            # any failure into a bare "error parsing the body", and the holder
            # acts on which failure it was.
            await wrapped.body()
            try:
                return await handler(wrapped)
            except RequestValidationError as exc:
                if any(_entries_too_long(error) for error in exc.errors()):
                    raise _batch_too_large() from None
                raise

        return decoded


_tree_router = APIRouter(route_class=_TreeReportRoute)


def _tree_change(entry: TreeEntry) -> TreeChange:
    """One wire entry in the library's terms: paths as the bytes they name,
    the hash as its digest."""
    return TreeChange(
        op=entry.op,
        path=entry.path.encode("utf-8", "surrogateescape"),
        kind=entry.kind,
        from_path=(
            entry.from_.encode("utf-8", "surrogateescape") if entry.from_ is not None else None
        ),
        size=entry.size,
        mtime_ns=entry.mtime_ns,
        mode=entry.mode,
        hash=bytes.fromhex(entry.hash[3:]) if entry.hash is not None else None,
    )


@_tree_router.post(
    _ITEM + "/lease/tree",
    dependencies=[Depends(ratelimited("leases.tree")), Depends(caller_drive)],
)
async def lease_tree(
    request: Request,
    drive_id: PathId,
    item_id: uuid.UUID,
    body: TreeBatch,
    ctx: FilesCtx,
    lease: Lease,
) -> TreeBatchAnswer:
    """The holder's report of its tree: every name, kind, size and modified
    time under the folder it holds, ahead of the bytes.

    Holder only and fenced like every write the holder makes; one transaction;
    all or nothing. Rows the drive did not have are minted with no bytes and
    carry the report until the bytes land. ``batch_id`` makes a resend free: it
    answers the first answer and changes nothing. The body may arrive gzipped.
    """
    if lease.epoch is None or lease.instance is None:
        raise leases_lib.LeaseConflict("files.lease_fenced", "a newer epoch owns this subtree")
    changes = [_tree_change(entry) for entry in body.entries]
    async with ctx.repo.transaction():
        facts = await _facts(request, ctx)
        handle = await _authorized(request, ctx, item_id, FilesAction.SNAPSHOT, facts)
        answer = await LeaseTreeService(
            ctx.repo,
            ctx.ctx,
            ctx.clock,
            ctx.store,
            verified_machine_id=ctx.agent_machine_id,
            ceilings=ctx.ceilings,
            local_state=is_local_state,
        ).apply(
            NodeId(handle.node.id),
            drive_id=DriveId(ctx.drive.id),
            epoch=lease.epoch,
            instance_id=lease.instance,
            batch_id=body.batch_id,
            changes=changes,
        )
    return TreeBatchAnswer(
        live_seq=answer.live_seq,
        applied=answer.applied,
        landing_count=answer.landing_count,
    )


@_tree_router.post(
    _ITEM + "/lease/tree/digests",
    dependencies=[Depends(ratelimited("leases.tree")), Depends(caller_drive)],
    response_model=DigestAnswer,
)
async def lease_tree_digests(
    request: Request,
    drive_id: PathId,
    item_id: uuid.UUID,
    body: DigestRequest,
    ctx: FilesCtx,
    lease: Lease,
) -> Response:
    """The drive's digest of each folder the holder's walk names, so the walk
    sends only the folders that differ, and the names of the children of the
    folders it asks to list, so the walk learns what its disk no longer has.

    Holder only and fenced like the tree report; a read, in one statement
    whatever the number of folders, and one more whatever the number listed.
    The body may arrive gzipped. The answer is rendered with every non-ASCII
    character escaped: a name that is not UTF-8 travels as its surrogate
    escapes, which no UTF-8 encoder would write.
    """
    if lease.epoch is None or lease.instance is None:
        raise leases_lib.LeaseConflict("files.lease_fenced", "a newer epoch owns this subtree")
    paths = [path.encode("utf-8", "surrogateescape") for path in body.paths]
    listed = [path.encode("utf-8", "surrogateescape") for path in body.names]
    async with ctx.repo.transaction():
        facts = await _facts(request, ctx)
        handle = await _authorized(request, ctx, item_id, FilesAction.SNAPSHOT, facts)
        reading = await LeaseTreeService(
            ctx.repo,
            ctx.ctx,
            ctx.clock,
            ctx.store,
            verified_machine_id=ctx.agent_machine_id,
            local_state=is_local_state,
        ).digests(
            NodeId(handle.node.id),
            drive_id=DriveId(ctx.drive.id),
            epoch=lease.epoch,
            instance_id=lease.instance,
            paths=paths,
            list_children=listed,
        )
    answer = DigestAnswer(
        digests={
            path: FolderDigest(count=digest.count, xor=digest.hex)
            for path, digest in zip(body.paths, (reading.digests[p] for p in paths), strict=True)
        },
        children={
            path: [name.decode("utf-8", "surrogateescape") for name in reading.children[raw]]
            for path, raw in zip(body.names, listed, strict=True)
        },
    )
    return Response(
        content=json.dumps(answer.model_dump(mode="python"), separators=(",", ":")),
        media_type="application/json",
    )


router.include_router(_tree_router)


@router.post(_ITEM + "/lease/release", dependencies=[Depends(ratelimited("leases"))])
@idempotent_route("files.leases.release_lease")
async def release_lease(
    request: Request,
    drive_id: uuid.UUID,
    item_id: uuid.UUID,
    body: ReleaseBody,
    ctx: FilesCtx,
    key: Idempotency,
    etag: IfMatch,
) -> None:
    """Hand the folder back, with the final batch in the same transaction: a
    reader who sees the lease gone sees every change it carried."""
    final = (
        _Final(instance_id=body.instance_id, changes=body.final) if body.final is not None else None
    )
    async with ctx.repo.transaction():
        facts = await _facts(request, ctx)
        handle = await _authorized(request, ctx, item_id, FilesAction.LEASE, facts)
        chat_id = _chat_of(handle.node) if body.ending is not None else None
        if chat_id is not None:
            # The chat row before the lease row, the order the chat-end
            # transition takes them in, so a hand-back and a server-side ending
            # of the same chat queue rather than deadlock.
            async with as_platform(ctx.repo.session):
                await chat_end.lock_chat(ctx.repo.session, chat_id)
        await _service(ctx).release(
            NodeId(handle.node.id),
            epoch=body.epoch,
            instance_id=body.instance_id,
            final=final,
            unsynced_count=body.unsynced_count,
        )
        if chat_id is not None and body.ending is not None:
            # The box's sleep IS the chat-end transition, not a second path
            # beside it: the lease this holder just handed back is the one the
            # transition would have ended, and the chat reads asleep with it.
            async with as_platform(ctx.repo.session):
                await chat_end.end_chat(
                    ctx.repo.session, chat_id, body.ending, actor=ctx.ctx.audit_dict()
                )


def _chat_of(node: Any) -> uuid.UUID | None:
    """The chat whose own folder ``node`` is, or ``None``."""
    if getattr(node, "subtype", None) != CHAT_SUBTYPE or node.target_object_id is None:
        return None
    return uuid.UUID(str(node.target_object_id))


@router.post(_ITEM + "/lease/request-release", dependencies=[Depends(ratelimited("leases"))])
@idempotent_route("files.leases.request_release")
async def request_release(
    request: Request,
    drive_id: uuid.UUID,
    item_id: uuid.UUID,
    ctx: FilesCtx,
    key: Idempotency,
    etag: IfMatch,
) -> LeaseRow:
    """Ask the holder for the folder back. A reader may do this, and the answer
    names the holder — a reader who may be told "Ana has it" is a reader who may
    already read the folder."""
    async with ctx.repo.transaction():
        facts = await _facts(request, ctx)
        handle = await _authorized(request, ctx, item_id, FilesAction.LEASE_REQUEST, facts)
        lease = await _service(ctx).request_release(NodeId(handle.node.id))
    return _row(lease)


@router.post(_ITEM + "/lease/force-release", dependencies=[Depends(ratelimited("leases"))])
@idempotent_route("files.leases.force_release")
async def force_release(
    request: Request,
    drive_id: uuid.UUID,
    item_id: uuid.UUID,
    ctx: FilesCtx,
    key: Idempotency,
    etag: IfMatch,
    body: ForceBody | None = None,
) -> LeaseGrant:
    """A manager takes it back. The holder is not cut off mid-write: it has one
    TTL of grace, and learns about it from ``forced`` on its next heartbeat.

    The grace is the deployment's TTL, the same one acquire and heartbeat hand
    out. A grace cut from the build's constant instead would be shorter than
    the beat cadence the holder was told to keep on any deployment that widened
    the window, and the holder would be fenced before it ever saw ``forced``.
    """
    ttl = _ttl(ctx)
    async with ctx.repo.transaction():
        facts = await _facts(request, ctx)
        handle = await _authorized(request, ctx, item_id, FilesAction.LEASE_FORCE, facts)
        holder_purpose = await leases_lib.live_lease_purpose(ctx.repo, NodeId(handle.node.id))
        reason = body.reason.strip() if body is not None else ""
        if holder_purpose in _BOX_LEASES and not reason:
            # A request without what the force needs, said as such: the box is
            # cutting off a turn in flight, and whoever forces it says why.
            raise InvalidRequest(
                "files.force_reason_required",
                "Say why you are taking this folder back from its box.",
            )
        await _decide_lease_kind(
            request,
            ctx,
            handle,
            facts,
            Action.LEASE_FORCE,
            purpose="",
            readable=FilesAction.READ.value in handle.access.allowed_actions,
            holder_purpose=holder_purpose,
            reason=reason,
        )
        lease = await _service(ctx).force_release(NodeId(handle.node.id), ttl=ttl)
    return _grant(ctx, lease, ttl)


@router.post(
    _ITEM + "/snapshots",
    dependencies=[Depends(ratelimited("leases.snapshots", limit=1200))],
)
@idempotent_route("files.leases.push_snapshot")
async def push_snapshot(
    request: Request,
    drive_id: uuid.UUID,
    item_id: uuid.UUID,
    body: SnapshotBody,
    ctx: FilesCtx,
    key: Idempotency,
    etag: IfMatch,
    lease: Lease,
) -> None:
    """The exporter's batch. Fenced by the epoch headers at the start and again
    at the commit, so a batch that began under a lease the reaper took away
    never lands half-applied."""
    epoch = lease.epoch if lease.epoch is not None else body.epoch
    instance = lease.instance if lease.instance is not None else body.instance_id
    if epoch is None or not instance:
        raise InvalidRequest(
            "files.bad_lease_headers",
            "A snapshot is fenced: send the lease epoch and instance",
        )
    async with ctx.repo.transaction():
        facts = await _facts(request, ctx)
        handle = await _authorized(request, ctx, item_id, FilesAction.SNAPSHOT, facts)
        await _apply_final(
            ctx.repo,
            NodeId(handle.node.id),
            # A checkpoint mid-lease, so the context carries no exemption: the
            # holder is still there and whatever it could not land it can send
            # again. Only the release's own batch is spared the ceilings.
            lease=_context(ctx, epoch, instance),
            changes=_Final(instance_id=instance, changes=body.changes),
        )


@router.get("/drives/{drive_id}/leases", dependencies=[Depends(ratelimited("leases"))])
async def my_leases(
    drive_id: uuid.UUID,
    ctx: FilesCtx,
    mine: Annotated[bool, Query()] = False,
) -> list[LeaseRow]:
    """My mounts across the drive. ``mine=true`` is required on day one: a
    drive-wide listing has to be filtered by readability per row, and answering
    the unfiltered question badly would list folders the caller cannot see.

    The drive is resolved first, before the flag and before the caller's own id
    is looked at, so a stranger's drive answers the opaque 404 rather than
    telling them which argument they got wrong.
    """
    drive = _drive(ctx, drive_id)
    if not mine:
        raise InvalidRequest("files.mine_required", "mine=true is required on this listing")
    # "Mine" is the identity the fence uses, kind and all: a caller who can hold
    # nothing lists nothing, and a machine's mounts are never a person's.
    holder = ctx.holder
    if holder is None:
        return []
    rows = (
        await ctx.repo.session.execute(
            text(
                "SELECT l.* FROM file_leases l JOIN file_nodes n ON n.id = l.node_id "
                "WHERE l.org_team_id = :org AND n.drive_id = :drive "
                "AND l.holder_principal_id = :holder AND l.holder_kind = :holder_kind "
                "AND l.released_at IS NULL AND l.reaped_at IS NULL AND l.expires_at > now() "
                "ORDER BY l.acquired_at"
            ),
            {
                "org": ctx.repo.scope.org_team_id,
                "drive": drive,
                "holder": holder.id,
                "holder_kind": holder.kind,
            },
        )
    ).all()
    return [
        LeaseRow(
            node_id=str(row.node_id),
            epoch=row.epoch,
            machine=row.machine_id,
            purpose=row.purpose,
            since=row.acquired_at,
            expires_at=row.expires_at,
            last_sync_at=row.last_sync_at,
        )
        for row in rows
    ]


__all__ = ["router"]
