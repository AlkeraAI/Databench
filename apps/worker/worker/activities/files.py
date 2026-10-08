"""The Files plane's activities: the upload commit, the janitor, the ACL rewrite
and the batched subtree move.

Thin ``@activity.defn`` wrappers around the cores in ``worker.tasks.files``.
Each heartbeats, so a worker that dies moving a multi-gigabyte object or half
way through a fleet-wide sweep is noticed in minutes rather than at the
deadline, and each translates the library's refusals into a Temporal
``ApplicationError`` whose type the workflow's ``non_retryable_error_types``
names — a bad hash, an exhausted quota, an authorization the caller has since
lost or a malformed request is a fact about the upload, not about this attempt,
and retrying it five more times only delays the failure the client is waiting
for. The activity types are the workflow type names.
"""

from __future__ import annotations

import uuid
from typing import ClassVar

from alkera_core.files.authz.authorize import Denied
from alkera_core.files.errors import InvalidRequest, QuotaExceeded
from alkera_core.files.ids import OperationId
from alkera_core.files.store.errors import ChecksumMismatch
from alkera_core.logging import get_logger
from alkera_core.schemas.temporal import SweepInput
from alkera_core.temporal import RECOVER_SETTLE_ACTIVITY, WorkflowType
from alkera_core.versioning import VersionedModel
from pydantic import Field
from temporalio import activity
from temporalio.exceptions import ApplicationError

from worker.tasks import files as tasks
from worker.temporal.heartbeat import heartbeating

log = get_logger(__name__)

PERMANENT: tuple[type[Exception], ...] = (ChecksumMismatch, QuotaExceeded, Denied, InvalidRequest)
"""The refusals a second attempt cannot change."""

NON_RETRYABLE_TYPES: tuple[str, ...] = tuple(exc.__name__ for exc in PERMANENT)
"""The ``ApplicationError.type`` names a workflow declares non-retryable."""


def _permanent(exc: Exception) -> ApplicationError:
    """The same refusal, typed so Temporal stops after one attempt."""
    return ApplicationError(str(exc), type=type(exc).__name__, non_retryable=True)


@activity.defn(name=WorkflowType.FILES_PROMOTE.value)
async def files_promote(op_id: str, org_team_id: str) -> str:
    """Turn the upload ``op_id`` was queued for into a version; returns its id."""
    async with heartbeating():
        try:
            return await tasks.promote(OperationId(uuid.UUID(op_id)), uuid.UUID(org_team_id))
        except PERMANENT as exc:
            log.warning("files.promote.refused", op_id=op_id, reason=type(exc).__name__)
            raise _permanent(exc) from exc


class JanitorPage(VersionedModel):
    """What one bounded janitor activity did, and where the next one starts.

    It crosses workflow history, so it is a versioned model with room to grow:
    ``cursor`` is the last org id of the page when more orgs remain and
    ``None`` when the fleet is done, and the ids are strings because that is
    what survives a JSON round trip unambiguously.
    """

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    swept: int = 0
    """Rows and objects this page's sweepers removed."""
    orgs: list[str] = Field(default_factory=list)
    """The orgs whose sweep completed, in id order."""
    cursor: str | None = None
    """Where the next page starts; ``None`` when this was the last page."""


@activity.defn(name=WorkflowType.FILES_JANITOR.value)
async def files_janitor(input: SweepInput | None = None, after: str | None = None) -> JanitorPage:
    """Run every sweeper once, in order, for one page of the orgs that use
    Files. Returns what it swept and the org the next page resumes at."""
    now = input.now if input is not None and input.now is not None else None
    async with heartbeating():
        page = await tasks.janitor(now, after=uuid.UUID(after) if after else None)
    log.info(
        "files.janitor.page",
        orgs=len(page.orgs),
        swept=page.swept,
        cursor=str(page.cursor) if page.cursor is not None else None,
    )
    return JanitorPage(
        swept=page.swept,
        orgs=[str(org) for org in page.orgs],
        cursor=str(page.cursor) if page.cursor is not None else None,
    )


class GcPage(VersionedModel):
    """What one bounded collector activity reclaimed, and where the next starts.

    It crosses workflow history, so it is a versioned model with room to grow.
    Counts rather than key lists: a pass may park thousands of objects and a
    workflow history is not a place to keep them -- the keys are on the log
    line the activity writes.
    """

    SCHEMA_VERSION: ClassVar[str] = "1.1.0"

    verdict: str = "ok"
    """``ok``, ``disabled``, or the coded reason the page refused to collect."""
    foreign_domains: int = 0
    """Unknown prefixes another deployment stamped: reported, never touched."""
    unstamped_domains: int = 0
    """Unknown prefixes nobody stamped: reported until an operator adopts them."""
    domains_scanned: int = 0
    """Domain prefixes this page visited."""
    orphan_domains: int = 0
    """Of those, the ones no drive row names any more."""
    parked: int = 0
    """Objects moved under ``deleted/`` for their recoverable week."""
    bytes_parked: int = 0
    erased: int = 0
    """Objects hard-deleted after that week."""
    kept_inside_grace: int = 0
    """Objects an orphan domain kept because they are too young to collect."""
    cursor: str | None = None
    """Where the next page starts; ``None`` when this was the last page."""


class GcRun(VersionedModel):
    """What a whole collection run did, as the workflow reports it.

    The verdict is on the result because a refusal is not a failure: a run that
    declined to collect completes, and the reason has to be readable from the
    workflow itself rather than only from a worker's log.
    """

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    verdict: str = "ok"
    parked: int = 0
    erased: int = 0
    pages: int = 0


@activity.defn(name=WorkflowType.FILES_GC.value)
async def files_gc(
    input: SweepInput | None = None,
    after: str | None = None,
    dry_run: bool = False,
    allow_mass_collect: bool = False,
) -> GcPage:
    """Collect one bounded page of the domains no drive row names any more."""
    now = input.now if input is not None and input.now is not None else None
    async with heartbeating():
        report = await tasks.files_gc(
            now, after=after, dry_run=dry_run, allow_mass_collect=allow_mass_collect
        )
    return GcPage(
        verdict=report.verdict,
        foreign_domains=len(report.foreign),
        unstamped_domains=len(report.unstamped),
        domains_scanned=len(report.scanned),
        orphan_domains=len(report.orphaned),
        parked=len(report.moved),
        bytes_parked=report.bytes_moved,
        erased=len(report.erased),
        kept_inside_grace=report.kept_young,
        cursor=report.cursor,
    )


@activity.defn(name=WorkflowType.FILES_ACL_REWRITE.value)
async def files_acl_rewrite(op_id: str, org_team_id: str) -> int:
    """Repair one batch of a subtree's ACL caches; returns how many it fixed."""
    async with heartbeating():
        try:
            return await tasks.acl_rewrite(OperationId(uuid.UUID(op_id)), uuid.UUID(org_team_id))
        except PERMANENT as exc:
            log.warning("files.acl_rewrite.refused", op_id=op_id, reason=type(exc).__name__)
            raise _permanent(exc) from exc


@activity.defn(name=WorkflowType.FILES_LARGE_MOVE.value)
async def files_large_move(op_id: str, org_team_id: str) -> str:
    """Drive an oversized subtree's queued move to its end; returns the
    operation's terminal state."""
    async with heartbeating():
        try:
            return await tasks.large_move(OperationId(uuid.UUID(op_id)), uuid.UUID(org_team_id))
        except PERMANENT as exc:
            log.warning("files.large_move.refused", op_id=op_id, reason=type(exc).__name__)
            raise _permanent(exc) from exc


@activity.defn(name=WorkflowType.FILES_COPY.value)
async def files_copy(op_id: str, org_team_id: str) -> str:
    """Drive a queued subtree copy to its end; returns the operation's terminal
    state."""
    async with heartbeating():
        try:
            return await tasks.copy(OperationId(uuid.UUID(op_id)), uuid.UUID(org_team_id))
        except PERMANENT as exc:
            log.warning("files.copy.refused", op_id=op_id, reason=type(exc).__name__)
            raise _permanent(exc) from exc


@activity.defn(name=WorkflowType.FILES_BULK.value)
async def files_bulk(op_id: str, org_team_id: str) -> str:
    """Drive a queued batch of tree changes to its end; returns the operation's
    terminal state."""
    async with heartbeating():
        try:
            return await tasks.bulk(OperationId(uuid.UUID(op_id)), uuid.UUID(org_team_id))
        except PERMANENT as exc:
            log.warning("files.bulk.refused", op_id=op_id, reason=type(exc).__name__)
            raise _permanent(exc) from exc


class QueuedHandoff(VersionedModel):
    """One abandoned operation and the workflow that should finish it.

    It crosses workflow history, so it is a versioned model, and its ids are
    strings for the same reason the janitor's are.
    """

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    workflow: str = ""
    """The workflow TYPE name to start, keyed by the operation."""
    op_id: str = ""
    org_team_id: str = ""


class RecoveryPage(VersionedModel):
    """What one recovery tick claimed, and what the workflow must now start."""

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    handoffs: list[QueuedHandoff] = Field(default_factory=list)
    """Operations for the workflow to start; empty on an inline deployment."""
    ran: int = 0
    """Operations this tick drove to their end in-process (inline deployments)."""
    failed: int = 0
    """Operations that ran out of offers and were failed with a reason."""
    unrecoverable: int = 0
    """Abandoned rows nothing can restart from the row alone."""


@activity.defn(name=WorkflowType.FILES_RECOVER_QUEUED.value)
async def files_recover_queued(input: SweepInput | None = None) -> RecoveryPage:
    """Find every abandoned ``queued`` operation and say who should run it.

    Nothing is counted against a row here. A row whose runner exists but has
    not reached a worker slot is indistinguishable from an abandoned one in
    the database, so what an offer was worth is only known once the
    orchestrator has answered — which is what ``files.recover_queued.settle``
    records. An inline deployment has no orchestrator to ask and settles its
    own, which is why this can still report rows it ran.
    """
    now = input.now if input is not None and input.now is not None else None
    async with heartbeating():
        page = await tasks.recover_queued(now)
    return RecoveryPage(
        handoffs=[
            QueuedHandoff(
                workflow=handoff.workflow.value,
                op_id=str(handoff.op_id),
                org_team_id=str(handoff.org_team_id),
            )
            for handoff in page.handoffs
        ],
        ran=len(page.ran),
        failed=len(page.failed),
        unrecoverable=len(page.unrecoverable),
    )


class RecoverySettlement(VersionedModel):
    """What the orchestrator answered for one tenant's offered operations."""

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    org_team_id: str = ""
    outcomes: dict[str, str] = Field(default_factory=dict)
    """Operation id to ``nudged`` (a fresh runner was taken — an attempt) or
    ``runner_present`` (one already existed, so the row is not abandoned)."""


@activity.defn(name=RECOVER_SETTLE_ACTIVITY)
async def files_recover_settle(settlement: RecoverySettlement) -> int:
    """Count the attempts that were really made, and fail the rows that are
    gone. Returns how many operations were abandoned."""
    async with heartbeating():
        failed = await tasks.settle_recovery(settlement.outcomes, uuid.UUID(settlement.org_team_id))
    return len(failed)


__all__ = [
    "NON_RETRYABLE_TYPES",
    "PERMANENT",
    "GcPage",
    "GcRun",
    "JanitorPage",
    "QueuedHandoff",
    "RecoveryPage",
    "RecoverySettlement",
    "files_acl_rewrite",
    "files_bulk",
    "files_copy",
    "files_gc",
    "files_janitor",
    "files_large_move",
    "files_promote",
    "files_recover_queued",
    "files_recover_settle",
]
