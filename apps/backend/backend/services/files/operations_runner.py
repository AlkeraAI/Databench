"""Run a queued Files operation inside the process that queued it.

A Files route that queues work answers ``202`` with an operation the client
polls; the Temporal worker family (``worker.tasks.files``) is what turns that
row into a version, a copied subtree or a rewritten one. A single-process
self-hosted install has no such worker, and a route test has no Temporal at
all — so ``FILES_INLINE_OPERATIONS`` lets the request run the work itself,
after its response has been sent.

Two properties make that safe to have in the tree at all:

*The same core, never a second implementation.* Each entry below calls exactly
the library function ``worker/tasks/files.py`` calls — ``UploadCompletion``'s
``promote``, ``resume_copy``, ``resume_large_move``. Nothing about the work,
its authorization, its batching or its error recording is duplicated here; a
divergence between the inline path and the worker path would be a bug that only
one deployment shape could see. The backend never imports the worker package
either: both callers sit on the library, which is the only thing they share.

*Its own session, after the queueing one committed.* The run is a Starlette
background task and opens a session of its own, exactly as the worker's cores
do, rebuilding the org's repo and store handle around it. That second session
can only see the operation row once the request's unit of work is durable, and
a background task runs *inside* the ASGI call — ahead of the request-scoped
``get_db`` teardown that would otherwise be what commits it. So the job ends
the queueing session's transaction itself before reading: the worker gets that
for free by being another process, and the inline path has to say it. Errors
land on the operation row through ``Operations.run``, which is the same
recording the worker gets; the raise is swallowed here because a background
task has nobody to fail to and the row is already the record.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Final, Literal, get_args

from alkera_core.authz.principal import ActingContext
from alkera_core.config import Settings
from alkera_core.db.session import background_session
from alkera_core.files.clock import Clock, SystemClock
from alkera_core.files.ids import DomainId, OperationId, OrgScope
from alkera_core.files.repo import FilesRepo
from alkera_core.logging import get_logger
from alkera_core.temporal.contract import WorkflowType
from fastapi import BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.files.context import FilesContext
from backend.services.files.store import store_factory
from backend.services.infra import nudge_files_operation

log = get_logger(__name__)

InlineKind = Literal["promote", "copy", "large_move", "bulk"]
"""The queued kinds that have a library core to resume.

Each names the one library entry point ``worker.tasks.files`` calls for that
kind; a kind whose work lives only here would be the second implementation this
module exists to avoid.
"""

WORKER_WORKFLOW: Final[dict[InlineKind, WorkflowType]] = {
    "promote": WorkflowType.FILES_PROMOTE,
    "copy": WorkflowType.FILES_COPY,
    "large_move": WorkflowType.FILES_LARGE_MOVE,
    "bulk": WorkflowType.FILES_BULK,
}
"""Who runs each kind when the deployment leaves queued work to the worker.

Total over :data:`InlineKind` on purpose, and checked below. The two halves of
a queued operation — the route that writes the row and the process that runs it
— are in different deployments' hands, and nothing sweeps a queued row: a kind
the route knows how to queue but nobody knows how to start is an operation that
answers 202 and then sits in ``queued`` forever, which is exactly what a bulk
trash of a large selection did. Adding a kind now has to name its workflow.
"""

_MISSING = set(get_args(InlineKind)) - WORKER_WORKFLOW.keys()
assert not _MISSING, f"a queued Files kind with no worker to run it: {sorted(_MISSING)}"


@dataclass(frozen=True, slots=True)
class InlineJob:
    """One queued operation, described by value so it outlives the request.

    Ids and the acting context only — never the request's repo, session or
    store handle, all three of which are gone by the time the task runs.
    """

    kind: InlineKind
    op_id: OperationId
    org_team_id: uuid.UUID
    domain_id: DomainId
    ctx: ActingContext
    settings: Settings


def inline_job(
    files: FilesContext,
    kind: InlineKind,
    op_id: OperationId,
) -> InlineJob:
    """Describe the operation ``files``'s route just queued."""
    return InlineJob(
        kind=kind,
        op_id=op_id,
        org_team_id=files.repo.scope.org_team_id,
        domain_id=DomainId(files.drive.dedup_domain_id),
        ctx=files.ctx,
        settings=files.settings,
    )


def queue_inline(
    background: BackgroundTasks,
    files: FilesContext,
    kind: InlineKind,
    op_id: OperationId,
) -> None:
    """Ask this request to run the operation it just queued, or hand it off.

    Every queued operation leaves here with exactly one runner named. When the
    deployment leaves queued work to the worker the request runs nothing
    itself, but it nudges the workflow :data:`WORKER_WORKFLOW` names for the
    kind, after the response and keyed by the operation — nothing on that shape
    sweeps queued rows, so a row nobody nudged waits for a pickup that never
    comes.
    """
    if not files.settings.files_inline_operations:
        background.add_task(
            nudge_files_operation,
            WORKER_WORKFLOW[kind],
            uuid.UUID(str(op_id)),
            files.repo.scope.org_team_id,
        )
        return
    background.add_task(
        run_inline,
        inline_job(files, kind, op_id),
        files.repo.session,
    )


async def run_inline(job: InlineJob, queueing_session: AsyncSession) -> None:
    """Run one queued operation to its terminal state, on a fresh session.

    ``queueing_session`` is the request's own session, still open behind this
    task: committing it is what makes the operation row the job is about
    visible to any other session, this one included.
    """
    try:
        await queueing_session.commit()
        # The worker's limits, not a request's: this is the worker's job, run
        # after the response, and a batch statement over a large folder is not
        # a statement some caller is waiting on.
        async with background_session() as session:
            await _dispatch(job, session, SystemClock())
    except Exception:
        # The failure is already on the operation row (``Operations.run``
        # records it before re-raising), and a background task has no caller to
        # propagate to. Logging it is what a reader of the process gets.
        log.exception(
            "files.inline_operation.failed",
            kind=job.kind,
            op_id=str(job.op_id),
            org_team_id=str(job.org_team_id),
        )


async def _dispatch(job: InlineJob, session: AsyncSession, clock: Clock) -> None:
    """Call the one library entry point ``worker.tasks.files`` calls for ``kind``."""
    from alkera_core.files import bulk as bulk_core
    from alkera_core.files.copy import resume_copy
    from alkera_core.files.large_move import resume_large_move
    from alkera_core.files.uploads import UploadCompletion

    repo = FilesRepo(session, OrgScope(org_team_id=job.org_team_id))
    if job.kind == "promote":
        store = await store_factory(job.settings, clock=clock).for_domain(job.domain_id)
        await UploadCompletion(repo, job.ctx, clock, store).promote_operation(job.op_id)
        return
    if job.kind == "copy":
        await resume_copy(repo, job.ctx, job.op_id, clock=clock)
        return
    if job.kind == "bulk":
        await bulk_core.resume(repo, job.ctx, job.op_id, clock=clock)
        return
    await resume_large_move(repo, job.ctx, job.op_id, clock=clock)


__all__ = [
    "WORKER_WORKFLOW",
    "InlineJob",
    "InlineKind",
    "inline_job",
    "queue_inline",
    "run_inline",
]
