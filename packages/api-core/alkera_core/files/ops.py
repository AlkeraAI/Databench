"""Operations, their inverses, and undo.

Three ideas hold this together.

*The inverse is written by the mutation, never derived afterwards.* A rename
knows the name it replaced; ten minutes later nobody does. So every undoable
mutation takes the ``Operations`` object it belongs to and calls
:meth:`Operations.record_inverse` inside its own transaction, so the inverse
lands or rolls back with the change it undoes and can never describe a tree
that did not exist.

*Undo is a forward operation.* :meth:`Operations.undo` does not replay history
backwards: it starts a NEW operation and applies the stored inverse through the
very same services a user's request would, which records that operation's own
inverse. Undo of an undo is therefore a redo by construction rather than by a
second code path, and permanent deletion — which records no inverse — is never
covered.

*A state change is a compare-and-swap, not a read-then-write.* ``queued →
running`` and every terminal transition are single ``UPDATE … WHERE state = …``
statements, so two runners racing to pick up one operation cannot both win and
a cancel that lands between a poll and a commit cannot be lost.
"""

from __future__ import annotations

import base64
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Annotated, Any, Final, Literal, TypeVar

from pydantic import Field, TypeAdapter
from sqlalchemy import text

from alkera_core.authz.principal import ActingContext
from alkera_core.files import history
from alkera_core.files.attrs import USER_XATTR_PREFIX, stored_xattrs, validate_xattrs
from alkera_core.files.checkpoints import Checkpoints, NoopCheckpoints
from alkera_core.files.clock import Clock
from alkera_core.files.errors import FilesError, InvalidRequest, NotFound, PreconditionFailed
from alkera_core.files.ids import DriveId, NodeId, OperationId, TrashOpId
from alkera_core.files.leases import LeaseContext, fenced_write_for, is_hand_back
from alkera_core.files.repo import FilesRepo
from alkera_core.files.store.scoped import DomainStore
from alkera_core.models.files.ops import OP_KINDS
from alkera_core.versioning import VersionedModel

#: The least silence a ``running`` operation is allowed before the watchdog
#: calls its runner dead. A floor, not the whole answer: an operation that has
#: declared the bytes it is moving is given this PLUS the time those bytes need
#: at :data:`WATCHDOG_BYTES_PER_SECOND`, because ten minutes is a generous wait
#: for a rename and a guillotine for a terabyte landing in the store.
HEARTBEAT_DEADLINE: Final = timedelta(minutes=10)

#: The throughput the byte half of that wait is budgeted at. Deliberately far
#: below anything a datacentre link does (1 MiB/s), so the deadline is a guard
#: against a runner that has stopped and never a bound a working one meets.
#: The budget shrinks as the bytes land — an operation reports what it has
#: moved — so a stall in the last mile is caught as quickly as an early one.
WATCHDOG_BYTES_PER_SECOND: Final = 1_048_576

#: How many bytes move before a body that counts them publishes again. A beat
#: per chunk would write a history row per 64 KiB; this is one row per 64 MiB,
#: which is movement a reader can see and a cost a terabyte can pay.
BYTE_TICK_EVERY: Final = 64 * 1024 * 1024

#: A ``queued`` operation nobody has claimed for this long was handed to a
#: runner that never came — a request whose background task was lost, a worker
#: that was not told. Whoever next finds it may hand it to a runner again: the
#: claim is a compare-and-swap, so a second hand-off costs nothing if the first
#: one turns up after all.
QUEUED_STALE_AFTER: Final = timedelta(seconds=30)

#: Where a re-hand is counted, inside the operation's own ``progress`` document.
#: It has to be on the row: the sweep that re-hands is stateless and may run in
#: any replica, so "how many times has this been offered to a runner" cannot
#: live in one process's memory.
RECOVERY_ATTEMPTS_KEY: Final = "recovery_attempts"

#: What an operation nobody ever ran is failed as. A client polling a ``queued``
#: row has no way to tell "about to start" from "abandoned", so the row has to
#: say so itself rather than sit at ``queued 0/N`` forever.
RUNNER_LOST_CODE: Final = "files.runner_lost"

#: A hand-off was actually issued for this row: the orchestrator had no runner
#: for it and took a fresh one. That, and only that, is an attempt.
RUNNER_NUDGED: Final = "nudged"

#: The orchestrator already had a runner for this row. It is not abandoned —
#: it was told, and whoever was told has not reached a worker slot yet — so the
#: offer count goes back to zero rather than up.
RUNNER_PRESENT: Final = "runner_present"

RecoveryOutcome = Literal["nudged", "runner_present"]
"""What the orchestrator answered when this row was offered to a runner."""

#: How often :meth:`Progress.tick` writes: a per-item commit would cost more
#: than the work, and a per-operation one would make progress unobservable.
DEFAULT_TICK_EVERY: Final = 100

#: The default undo window. An operation with no explicit ``undoable_until`` is
#: undoable forever, which is what a small rename wants; the routes pass the
#: product's window.
NO_CHECKPOINTS: Checkpoints = NoopCheckpoints()

#: What a failure that is not a typed Files refusal is recorded as. An
#: arbitrary exception's text names rows, paths and driver internals, and an
#: operation's errors are read by whoever polls it — so only the typed
#: refusals, which are written to be read, put their own words on the wire.
GENERIC_FAILURE_CODE: Final = "files.operation_failed"

T = TypeVar("T")


def failure_record(exc: BaseException) -> dict[str, Any]:
    """One failure in the shape a polling client reads: code, then prose.

    The code is what a client branches on — a duplicate name is answered by
    asking the person, a store outage by offering a retry — so a stringified
    exception, whose ``code`` would be the sentence, is not enough.
    """
    if isinstance(exc, FilesError):
        return {"itemId": None, "code": exc.code, "message": str(exc)}
    return {"itemId": None, "code": GENERIC_FAILURE_CODE, "message": "the operation failed"}


# ---- inverses -----------------------------------------------------------


class Inverse(VersionedModel):
    """What to do to put the tree back. One variant per undoable mutation."""

    __abstract__ = True


class RenameInverse(Inverse):
    """Put the old name back on a node that was renamed."""

    SCHEMA_VERSION = "1.0.0"
    kind: Literal["rename"] = "rename"
    node_id: uuid.UUID
    #: Latin-1 escaped so an arbitrary byte name survives JSON. Names are bytes
    #: on purpose (see ``names``) and a UTF-8 assumption here would lose one.
    name: str


class MoveInverse(Inverse):
    """Move a node back under the parent it came from.

    Both parents are stored: the destination is what makes the inverse
    idempotent to validate, and the source is what the undo applies.
    """

    SCHEMA_VERSION = "1.0.0"
    kind: Literal["move"] = "move"
    node_id: uuid.UUID
    from_parent_id: uuid.UUID
    to_parent_id: uuid.UUID


class RestoreInverse(Inverse):
    """Undo of a trash: restore that trash op's subtree."""

    SCHEMA_VERSION = "1.0.0"
    kind: Literal["restore"] = "restore"
    trash_op_id: uuid.UUID


class TrashInverse(Inverse):
    """Undo of a restore: trash the node again."""

    SCHEMA_VERSION = "1.0.0"
    kind: Literal["trash"] = "trash"
    node_id: uuid.UUID


class AttrsInverse(Inverse):
    """Put the previous POSIX attributes back."""

    SCHEMA_VERSION = "1.0.0"
    kind: Literal["attrs"] = "attrs"
    node_id: uuid.UUID
    attrs: dict[str, Any]


class RawInverse(Inverse):
    """An inverse a newer writer recorded that this reader cannot apply.

    The catch-all variant of the union: an unknown tag routes here rather than
    raising, and undo refuses it explicitly instead of guessing at a tree.
    """

    SCHEMA_VERSION = "1.0.0"
    kind: str = "raw"
    body: dict[str, Any] = Field(default_factory=dict)


#: The applicable variants; anything else is a :class:`RawInverse`.
KNOWN_INVERSES: Final = ("rename", "move", "restore", "trash", "attrs")

OperationInverse = (
    Annotated[
        RenameInverse | MoveInverse | RestoreInverse | TrashInverse | AttrsInverse,
        Field(discriminator="kind"),
    ]
    | RawInverse
)

_INVERSE_ADAPTER: TypeAdapter[Any] = TypeAdapter(OperationInverse)


def parse_inverse(body: dict[str, Any] | None) -> Inverse | None:
    """Read a stored inverse, routing an unknown tag to :class:`RawInverse`."""
    if body is None:
        return None
    if body.get("kind") not in KNOWN_INVERSES:
        return RawInverse(kind=str(body.get("kind", "raw")), body=body)
    parsed = _INVERSE_ADAPTER.validate_python(body)
    assert isinstance(parsed, Inverse)
    return parsed


# ---- the operation's observable state ------------------------------------


@dataclass(frozen=True, slots=True)
class OperationState:
    """What any session can see about one operation."""

    id: OperationId
    drive_id: DriveId
    kind: str
    state: str
    done: int
    total: int | None
    #: How many bytes of work the operation has accounted for. Recorded in the
    #: same ``progress`` document ``done`` is, so a body publishes both in the
    #: statement it already beats with.
    bytes: int
    errors: list[Any]
    conflicts: list[Any]
    heartbeat_at: datetime | None
    undoable_until: datetime | None
    cancel_requested: bool
    inverse: Inverse | None
    result_node_id: uuid.UUID | None
    #: What the body recorded about what it produced — the version an upload
    #: wrote, whether those bytes were already there. Free-form on purpose: a
    #: family adds a fact by writing it, not by widening a table.
    result: dict[str, Any]


@dataclass(frozen=True, slots=True)
class RecoveredQueued:
    """One queued operation the sweep is offering to a runner again."""

    id: OperationId
    kind: str
    attempts: int
    """How many times it has now been offered, this offer included."""


@dataclass(frozen=True, slots=True)
class QueuedRecovery:
    """What one recovery pass did to one tenant's abandoned queued rows."""

    rehanded: tuple[RecoveredQueued, ...] = ()
    failed: tuple[OperationId, ...] = ()
    """The ones whose offers ran out; ``failed`` on the row, with a reason."""


class OperationCancelled(Exception):  # noqa: N818 - the class IS the outcome, not an error suffix
    """A body stopped at a batch boundary because a cancel was requested."""


class Operations:
    """Start, run, observe, cancel and undo the operations of one tenant."""

    def __init__(
        self,
        repo: FilesRepo,
        ctx: ActingContext,
        clock: Clock,
        store: DomainStore | None = None,
        *,
        checkpoints: Checkpoints = NO_CHECKPOINTS,
    ) -> None:
        self._repo = repo
        self._ctx = ctx
        self._clock = clock
        self._store = store
        self._checkpoints = checkpoints
        self._current: OperationId | None = None

    # ---- lifecycle -------------------------------------------------------

    async def start(
        self,
        kind: str,
        *,
        drive_id: DriveId,
        inverse: Inverse | None = None,
        undoable_until: datetime | None = None,
        total: int | None = None,
    ) -> OperationState:
        """Record a ``queued`` operation. Nothing has happened to the tree yet.

        The kind is checked here rather than left to the table's CHECK because a
        constraint violation would poison the caller's transaction, and a
        mis-typed kind is a programming error that should name itself.
        """
        if kind not in OP_KINDS:
            raise InvalidRequest(f"unknown operation kind {kind!r}")
        op_id = OperationId(uuid.uuid4())
        async with self._txn():
            await self._repo.session.execute(
                text(
                    "INSERT INTO file_ops (id, org_team_id, drive_id, kind, actor, inverse, "
                    "undoable_until, state, progress, errors, conflicts, result) VALUES "
                    "(:id, :org, :drive, :kind, :actor, CAST(:inverse AS jsonb), :until, "
                    "'queued', CAST(:progress AS jsonb), '[]'::jsonb, '[]'::jsonb, '{}'::jsonb)"
                ),
                {
                    "id": op_id,
                    "org": self._repo.scope.org_team_id,
                    "drive": drive_id,
                    "kind": kind,
                    "actor": self._actor(),
                    "inverse": _dump(inverse),
                    "until": undoable_until,
                    "progress": _json({"done": 0, "total": total}),
                },
            )
            await self._emit(op_id, drive_id)
        return await self.get(op_id)

    @asynccontextmanager
    async def perform(
        self,
        kind: str,
        *,
        drive_id: DriveId,
        undoable_until: datetime | None = None,
    ) -> AsyncIterator[OperationState]:
        """Run one short mutation as an operation.

        The body's own transaction is where the inverse lands, so the operation
        is bound for the whole block: a mutation called inside it with
        ``op=<this Operations>`` records into this row and nowhere else.
        """
        started = await self.start(kind, drive_id=drive_id, undoable_until=undoable_until)
        await self._transition(started.id, "queued", "running", heartbeat=True)
        previous, self._current = self._current, started.id
        try:
            yield started
        except BaseException:
            self._current = previous
            await self._transition(started.id, "running", "failed")
            raise
        self._current = previous
        await self._transition(started.id, "running", "done")

    async def run(
        self,
        op_id: OperationId,
        body: Callable[[Progress], Awaitable[T]],
    ) -> T | None:
        """Drive a long body, claiming the operation and settling its state.

        The claim is a CAS, so a second runner that picks up the same row gets
        zero rows and refuses rather than running the body twice. A body that
        dies without raising — a killed checkpoint, a SIGKILL — leaves the row
        ``running`` with a stale heartbeat, which is exactly what the watchdog
        is for; guessing ``failed`` here would be a lie about work that may
        still be committing.
        """
        claimed = await self._transition(op_id, "queued", "running", heartbeat=True)
        if not claimed:
            state = await self.get(op_id)
            raise PreconditionFailed(f"operation {op_id} is {state.state}, not queued")
        progress = Progress(self, op_id)
        previous, self._current = self._current, op_id
        try:
            result = await body(progress)
        except OperationCancelled:
            self._current = previous
            await self._transition(op_id, "running", "cancelled", done=progress.done)
            return None
        except Exception as exc:
            self._current = previous
            await self._transition(
                op_id, "running", "failed", error=failure_record(exc), done=progress.done
            )
            raise
        self._current = previous
        if (await self.get(op_id)).cancel_requested:
            await self._transition(op_id, "running", "cancelled", done=progress.done)
        else:
            await self._transition(op_id, "running", "done", done=progress.done)
        return result

    async def cancel(self, op_id: OperationId) -> OperationState:
        """Ask an operation to stop.

        A queued operation is cancelled outright; a running one is *asked*, and
        the flag is what its body polls at a batch boundary. Killing it here
        instead would leave a half-rewritten subtree with nobody to finish it.
        """
        state = await self.get(op_id)
        if state.state == "queued":
            await self._transition(op_id, "queued", "cancelled")
            return await self.get(op_id)
        if state.state == "running":
            async with self._txn():
                await self._repo.session.execute(
                    text(
                        "UPDATE file_ops SET progress = progress || "
                        "'{\"cancel_requested\": true}'::jsonb "
                        "WHERE id = :id AND org_team_id = :org AND state = 'running'"
                    ),
                    {"id": op_id, "org": self._repo.scope.org_team_id},
                )
                await self._emit(op_id, state.drive_id)
            return await self.get(op_id)
        raise InvalidRequest(f"operation {op_id} is already {state.state}")

    async def supersede_queued(self, op_id: OperationId) -> int:
        """Close every OTHER queued operation carrying the node ``op_id`` carries.

        A node ends up with several queued operations of one kind when the
        requests that routed them could not see each other's row yet — and
        running them all would run the first and fail the rest against a tree
        the first already rewrote. The one kept is ``op_id``; the duplicates go
        to ``cancelled`` with an error naming it, so a client polling one of
        them learns where its node's move went rather than watching a row
        nobody will touch. Returns how many were closed.
        """
        async with self._txn():
            rows = (
                await self._repo.session.execute(
                    text(
                        "UPDATE file_ops AS o SET state = 'cancelled', "
                        "errors = o.errors || CAST(:error AS jsonb) "
                        "FROM file_ops AS keep "
                        "WHERE keep.id = :id AND keep.org_team_id = :org "
                        "AND o.org_team_id = :org AND o.id <> keep.id "
                        "AND o.kind = keep.kind AND o.result_node_id = keep.result_node_id "
                        "AND o.result_node_id IS NOT NULL AND o.state = 'queued' "
                        "RETURNING o.id, o.drive_id"
                    ),
                    {
                        "id": op_id,
                        "org": self._repo.scope.org_team_id,
                        "error": _json(
                            [
                                {
                                    "code": "superseded",
                                    "message": f"superseded by operation {op_id}",
                                }
                            ]
                        ),
                    },
                )
            ).fetchall()
            for row in rows:
                await self._emit(OperationId(row[0]), DriveId(row[1]))
        return len(rows)

    async def get(self, op_id: OperationId) -> OperationState:
        """Read one operation. Any session, any process, no lock."""
        async with self._txn():
            row = (
                await self._repo.session.execute(
                    text(
                        "SELECT drive_id, kind, state, progress, errors, conflicts, "
                        "heartbeat_at, undoable_until, inverse, result_node_id, result "
                        "FROM file_ops WHERE id = :id AND org_team_id = :org"
                    ),
                    {"id": op_id, "org": self._repo.scope.org_team_id},
                )
            ).first()
        if row is None:
            raise NotFound(f"no operation {op_id}")
        progress = dict(row[3] or {})
        return OperationState(
            id=op_id,
            drive_id=DriveId(row[0]),
            kind=str(row[1]),
            state=str(row[2]),
            done=int(progress.get("done", 0)),
            total=progress.get("total"),
            bytes=int(progress.get("bytes", 0) or 0),
            errors=list(row[4] or []),
            conflicts=list(row[5] or []),
            heartbeat_at=row[6],
            undoable_until=row[7],
            cancel_requested=bool(progress.get("cancel_requested", False)),
            inverse=parse_inverse(row[8]),
            result_node_id=row[9],
            result=dict(row[10] or {}),
        )

    async def watchdog(self, now: datetime) -> list[OperationId]:
        """Fail every running operation whose silence has outlived its work.

        The wait is :data:`HEARTBEAT_DEADLINE` plus the time the bytes the
        operation still owes need at :data:`WATCHDOG_BYTES_PER_SECOND`, so a
        promote carrying a terabyte into the store is not declared dead at ten
        minutes for the crime of being large. An operation that declared no
        bytes is judged on the floor alone, which is what a rename wants.

        The deadline is compared inside the statement against the value the
        caller passes, so a test can hand it a clock that has crossed the
        boundary without waiting the real wait. Every batch the dead runner
        committed stays committed: a batched body is consistent at every
        boundary by construction, so failing the row loses no work.
        """
        async with self._txn():
            rows = (
                await self._repo.session.execute(
                    text(
                        "UPDATE file_ops SET state = 'failed', "
                        "errors = errors || '[\"heartbeat lost\"]'::jsonb "
                        "WHERE org_team_id = :org AND state = 'running' "
                        "AND heartbeat_at IS NOT NULL "
                        "AND heartbeat_at < CAST(:now AS timestamptz) - make_interval("
                        "secs => :floor + GREATEST("
                        "COALESCE((progress->>'byte_budget')::bigint, 0) "
                        "- COALESCE((progress->>'bytes')::bigint, 0), 0) / :throughput) "
                        "RETURNING id, drive_id"
                    ),
                    {
                        "org": self._repo.scope.org_team_id,
                        "now": now,
                        "floor": HEARTBEAT_DEADLINE.total_seconds(),
                        "throughput": float(WATCHDOG_BYTES_PER_SECOND),
                    },
                )
            ).fetchall()
            failed = [OperationId(row[0]) for row in rows]
            for row in rows:
                await self._emit(OperationId(row[0]), DriveId(row[1]))
        return failed

    async def abandoned_queued(self, now: datetime, *, limit: int) -> tuple[RecoveredQueued, ...]:
        """Every ``queued`` row of this tenant that no runner has claimed.

        The watchdog's counterpart. It judges ``running`` rows by their
        heartbeat; this finds the rows that never got one — ``queued`` past
        :data:`QUEUED_STALE_AFTER` with no ``heartbeat_at`` — which is the
        state a lost hand-off leaves behind. A route's nudge is best-effort
        inside a two-second budget: the orchestrator can be unreachable for
        exactly that long, or the process can die after answering 202, and the
        row is then durable with nobody at all named to run it.

        Read-only on purpose. Being FOUND here is not being abandoned: a row
        whose runner is merely waiting for a worker slot looks exactly the
        same from the database, and counting an attempt against it would fail
        a healthy operation for being slow. What each row's offer was worth is
        settled afterwards by :meth:`settle_recovery`, against what the
        orchestrator answered.

        Oldest first, so a steady trickle of newer abandoned rows cannot starve
        the one that has waited longest; ``now`` is the caller's clock, so a
        test crosses the staleness boundary without waiting it.
        """
        async with self._txn():
            rows = (
                await self._repo.session.execute(
                    text(
                        "SELECT id, kind, COALESCE((progress->>:key)::int, 0) "
                        "FROM file_ops "
                        "WHERE org_team_id = :org AND state = 'queued' "
                        "AND heartbeat_at IS NULL "
                        "AND created_at <= CAST(:cutoff AS timestamptz) "
                        "ORDER BY created_at LIMIT :limit"
                    ),
                    {
                        "org": self._repo.scope.org_team_id,
                        "cutoff": now - QUEUED_STALE_AFTER,
                        "limit": limit,
                        "key": RECOVERY_ATTEMPTS_KEY,
                    },
                )
            ).fetchall()
        return tuple(
            RecoveredQueued(id=OperationId(row[0]), kind=str(row[1]), attempts=int(row[2]))
            for row in rows
        )

    async def settle_recovery(
        self, outcomes: Mapping[OperationId, RecoveryOutcome], *, max_attempts: int
    ) -> QueuedRecovery:
        """Record what each offer was worth, and fail the rows that are gone.

        An attempt is counted only where a hand-off was actually ISSUED. Where
        the orchestrator answered that a runner already exists
        (:data:`RUNNER_PRESENT`) the row is not abandoned at all — it was told,
        and its runner is waiting for a worker slot — so its count is reset:
        two minutes of a busy queue must not fail an operation that is about
        to run. Only a row whose runner was provably absent every time, N+1
        times over, is failed with :data:`RUNNER_LOST_CODE`; a client that
        cannot be served deserves to be told so, and a row re-nudged every
        tick until the end of time is a permanent load nobody reads.

        Every write is a compare-and-swap on ``state = 'queued'``, so a row a
        runner claimed between the offer and this settlement is left exactly as
        the runner left it.
        """
        nudged = [op_id for op_id, outcome in outcomes.items() if outcome == RUNNER_NUDGED]
        present = [op_id for op_id, outcome in outcomes.items() if outcome == RUNNER_PRESENT]
        rehanded: list[RecoveredQueued] = []
        failed: list[OperationId] = []
        async with self._txn():
            if present:
                await self._count_offers(present, reset=True)
            counted = await self._count_offers(nudged, reset=False) if nudged else []
            spent = [row for row in counted if int(row[2]) > max_attempts]
            rehanded = [
                RecoveredQueued(id=OperationId(row[0]), kind=str(row[1]), attempts=int(row[2]))
                for row in counted
                if int(row[2]) <= max_attempts
            ]
            if spent:
                closed = (
                    await self._repo.session.execute(
                        text(
                            "UPDATE file_ops SET state = 'failed', "
                            "errors = errors || CAST(:error AS jsonb) "
                            "WHERE org_team_id = :org AND id = ANY(:ids) AND state = 'queued' "
                            "RETURNING id, drive_id"
                        ),
                        {
                            "org": self._repo.scope.org_team_id,
                            "ids": [row[0] for row in spent],
                            "error": _json(
                                [
                                    {
                                        "itemId": None,
                                        "code": RUNNER_LOST_CODE,
                                        "message": (
                                            "no runner ever started this operation; it was "
                                            f"handed over {max_attempts} times and each time "
                                            "the runner was gone again"
                                        ),
                                    }
                                ]
                            ),
                        },
                    )
                ).fetchall()
                failed = [OperationId(row[0]) for row in closed]
                for row in closed:
                    await self._emit(OperationId(row[0]), DriveId(row[1]))
        return QueuedRecovery(rehanded=tuple(rehanded), failed=tuple(failed))

    async def _count_offers(self, op_ids: Sequence[OperationId], *, reset: bool) -> Sequence[Any]:
        """Add one to each row's offer count, or clear it.

        Clearing rather than leaving the count alone is the point of the reset:
        a row that is told about, then lost, then told about again has not been
        abandoned three times in a row, and only a run of CONSECUTIVE absences
        says its runner is really gone.

        Two statements rather than one with the expression pasted in: what is
        written to the row is not a value a caller can steer.
        """
        bump = (
            "UPDATE file_ops AS o SET progress = jsonb_set("
            "  COALESCE(o.progress, '{}'::jsonb), ARRAY[:key],"
            "  to_jsonb(COALESCE((o.progress->>:key)::int, 0) + 1)) "
            "WHERE o.org_team_id = :org AND o.id = ANY(:ids) AND o.state = 'queued' "
            "RETURNING o.id, o.kind, (o.progress->>:key)::int"
        )
        clear = (
            "UPDATE file_ops AS o SET progress = jsonb_set("
            "  COALESCE(o.progress, '{}'::jsonb), ARRAY[:key], to_jsonb(0)) "
            "WHERE o.org_team_id = :org AND o.id = ANY(:ids) AND o.state = 'queued' "
            "RETURNING o.id, o.kind, (o.progress->>:key)::int"
        )
        return (
            await self._repo.session.execute(
                text(clear if reset else bump),
                {
                    "org": self._repo.scope.org_team_id,
                    "ids": list(op_ids),
                    "key": RECOVERY_ATTEMPTS_KEY,
                },
            )
        ).fetchall()

    # ---- inverses --------------------------------------------------------

    async def record_inverse(self, inverse: Inverse) -> None:
        """Write the bound operation's inverse, in the mutation's transaction.

        A no-op when nothing is bound: a mutation called outside an operation
        is simply not undoable, and refusing it here would make every ordinary
        rename require a ceremony.
        """
        if self._current is None:
            return
        self._repo._require_open()
        await self._repo.session.execute(
            text(
                "UPDATE file_ops SET inverse = CAST(:inverse AS jsonb) "
                "WHERE id = :id AND org_team_id = :org"
            ),
            {
                "id": self._current,
                "org": self._repo.scope.org_team_id,
                "inverse": _dump(inverse),
            },
        )

    async def undo(
        self, op_id: OperationId, *, lease: LeaseContext | None = None
    ) -> tuple[OperationState, OperationState | None]:
        """Apply an operation's inverse as a new, itself-undoable operation.

        The answer is a pair: the undo operation, and the operation the inverse
        had to queue rather than run inline — an oversized restore's re-parent —
        or ``None`` when the inverse ran whole. The queued one is handed back
        for the same reason ``trash.restore`` hands it back: only the caller can
        start its runner, so an operation swallowed here is a subtree that never
        arrives and a ``file_ops`` row nobody picks up.

        ``lease`` is the caller's own fencing context, handed down to every
        service the inverse runs through. Undo is forward work, so it is a write
        into whatever folder it lands in and is fenced exactly as that write
        would be — which means a stranger's undo inside a mount is refused, and
        the holder's own undo works. Left out, both were refused, and the undo
        toast on a mounted folder never did anything.
        """
        state = await self.get(op_id)
        if state.state != "done":
            raise InvalidRequest(f"operation {op_id} is {state.state}; only a done op undoes")
        if state.inverse is None:
            raise InvalidRequest(f"operation {op_id} has no inverse and cannot be undone")
        if isinstance(state.inverse, RawInverse):
            raise InvalidRequest(
                f"operation {op_id} records an inverse this build cannot apply "
                f"({state.inverse.kind!r})"
            )
        if state.undoable_until is not None and self._clock.now() > state.undoable_until:
            raise PreconditionFailed(f"operation {op_id} is no longer undoable")

        queued: OperationState | None = None
        async with self.perform(
            "undo", drive_id=state.drive_id, undoable_until=state.undoable_until
        ) as undone:
            await self._checkpoints.reach("ops.before_undo_apply")
            async with self._repo.transaction():
                queued = await self._apply(state.inverse, lease=lease)
        return await self.get(undone.id), queued

    async def _apply(
        self, inverse: Inverse, *, lease: LeaseContext | None = None
    ) -> OperationState | None:
        """Run one inverse through the real services, so it records its own.

        Returns the operation a service queued instead of writing inline, or
        ``None``. The caller starts it; nothing here does.

        Imported here rather than at module scope because the namespace and
        trash services import this module for the ``op`` argument; the cycle is
        real and this is the edge that breaks it.
        """
        from alkera_core.files.namespace import Namespace
        from alkera_core.files.trash import Trash

        if isinstance(inverse, RenameInverse):
            namespace = Namespace(self._repo, self._ctx, self._clock, self._store)
            node = await self._require_node(NodeId(inverse.node_id))
            await namespace.rename(
                NodeId(inverse.node_id),
                inverse.name.encode("latin-1"),
                if_match=node.etag,
                op=self,
                lease=lease,
            )
        elif isinstance(inverse, MoveInverse):
            namespace = Namespace(self._repo, self._ctx, self._clock, self._store)
            node = await self._require_node(NodeId(inverse.node_id))
            moved = await namespace.move(
                NodeId(inverse.node_id),
                NodeId(inverse.from_parent_id),
                if_match=node.etag,
                op=self,
                lease=lease,
            )
            return moved if isinstance(moved, OperationState) else None
        elif isinstance(inverse, RestoreInverse):
            trash = Trash(self._repo, self._ctx, self._clock, self._store)
            # ``restore`` already names its trash-op row ``op``; the operation
            # argument is spelled in full there rather than shadowing it.
            restored = await trash.restore(
                TrashOpId(inverse.trash_op_id), operation=self, lease=lease
            )
            return restored if isinstance(restored, OperationState) else None
        elif isinstance(inverse, TrashInverse):
            trash = Trash(self._repo, self._ctx, self._clock, self._store)
            node = await self._require_node(NodeId(inverse.node_id))
            await trash.trash(NodeId(inverse.node_id), if_match=node.etag, op=self, lease=lease)
        elif isinstance(inverse, AttrsInverse):
            await set_attrs(
                self._repo,
                self._ctx,
                NodeId(inverse.node_id),
                inverse.attrs,
                op=self,
                lease=lease,
            )
        else:  # pragma: no cover - RawInverse is refused before it reaches here
            raise InvalidRequest(f"cannot apply a {type(inverse).__name__}")
        return None

    async def _require_node(self, node_id: NodeId) -> Any:
        node = await self._repo.node(node_id)
        if node is None:
            raise NotFound(f"no node {node_id}")
        return node

    # ---- internals -------------------------------------------------------

    @asynccontextmanager
    async def _txn(self) -> AsyncIterator[None]:
        """Use the caller's Files transaction, or open one for this statement."""
        if self._repo._open:
            yield
            return
        async with self._repo.transaction():
            yield

    async def _transition(
        self,
        op_id: OperationId,
        expected: str,
        target: str,
        *,
        heartbeat: bool = False,
        error: dict[str, Any] | None = None,
        done: int | None = None,
    ) -> bool:
        """One compare-and-swap between two states. ``False`` means someone won.

        ``done`` publishes the body's final count in the same statement that
        settles the state. A tick only writes every Nth item, so the last short
        window of a batch — the single item of 101, the nine of 209 — was
        counted in memory and never reached the row: the operation read ``done``
        while its progress still said 100 of 101, and a client drawing that
        figure sat one item short for ever. It is written as a maximum because a
        resumed body counts only the items left to it: a run that finishes the
        tail of a batch must not publish a count smaller than the one the run
        before it already reached.
        """
        async with self._txn():
            row = (
                await self._repo.session.execute(
                    text(
                        # S608: every fragment below is a literal chosen by a
                        # keyword-only bool; no caller value reaches the SQL.
                        "UPDATE file_ops SET state = :target"  # noqa: S608
                        + (", heartbeat_at = now()" if heartbeat else "")
                        + (
                            ", progress = progress || jsonb_build_object('done', GREATEST("
                            "COALESCE((progress->>'done')::bigint, 0), CAST(:done AS bigint)))"
                            if done is not None
                            else ""
                        )
                        + (
                            ", errors = errors || CAST(:error AS jsonb)"
                            if error is not None
                            else ""
                        )
                        + " WHERE id = :id AND org_team_id = :org AND state = :expected "
                        "RETURNING drive_id"
                    ),
                    {
                        "id": op_id,
                        "org": self._repo.scope.org_team_id,
                        "expected": expected,
                        "target": target,
                        **({"error": _json([error])} if error is not None else {}),
                        **({"done": done} if done is not None else {}),
                    },
                )
            ).first()
            if row is not None:
                await self._emit(op_id, DriveId(row[0]))
        return row is not None

    async def _beat(
        self, op_id: OperationId, done: int, extra: dict[str, Any] | None = None
    ) -> None:
        """Publish progress and prove the runner is alive, in one statement.

        ``extra`` is merged into the same ``progress`` document, so a body that
        counts bytes as well as items publishes both without a second write —
        and the watchdog reads the byte figures out of the row the beat just
        wrote.
        """
        async with self._txn():
            row = (
                await self._repo.session.execute(
                    text(
                        "UPDATE file_ops SET heartbeat_at = now(), "
                        "progress = progress || CAST(:done AS jsonb) "
                        "WHERE id = :id AND org_team_id = :org AND state = 'running' "
                        "RETURNING drive_id"
                    ),
                    {
                        "id": op_id,
                        "org": self._repo.scope.org_team_id,
                        "done": _json({"done": done, **(extra or {})}),
                    },
                )
            ).first()
            if row is not None:
                await self._emit(op_id, DriveId(row[0]))

    async def _emit(self, op_id: OperationId, drive_id: DriveId) -> None:
        await history.emit_operation_changed(
            self._repo,
            self._ctx,
            op_id=op_id,
            drive_id=drive_id,
            version=0,
        )

    def _actor(self) -> uuid.UUID:
        """Whose operation this is — the human behind an agent, else the caller.

        Folded through ``history.subject_ref`` because a service principal's id is a
        component name: reading it as a UUID crashed every operation it opened.
        """
        return history.subject_ref(self._ctx)


class Progress:
    """The handle a running body reports through.

    ``tick`` batches its writes because a commit per item would cost more than
    the work; ``cancelled`` reads fresh because the answer is written by
    another session and a cached one would run a cancelled operation to the
    end.
    """

    def __init__(
        self,
        ops: Operations,
        op_id: OperationId,
        *,
        every: int = DEFAULT_TICK_EVERY,
    ) -> None:
        self._ops = ops
        self._op_id = op_id
        self._every = max(1, every)
        self._done = 0
        self._since_beat = 0
        self._bytes = 0
        self._bytes_since_beat = 0

    @property
    def done(self) -> int:
        return self._done

    async def tick(self, items: int = 1) -> None:
        """Count ``items`` of work; publish every N."""
        self._done += items
        self._since_beat += items
        if self._since_beat >= self._every:
            self._since_beat = 0
            await self._ops._beat(self._op_id, self._done)
            await self._ops._checkpoints.reach("ops.after_tick")

    @property
    def bytes(self) -> int:
        return self._bytes

    async def declare_bytes(self, total: int) -> None:
        """Say how many bytes this body is on the hook for moving.

        Two things read it. The poll, which can then show a reader a figure
        that moves during a single large write instead of a part count that
        sits at zero; and the watchdog, which gives a silent operation time in
        proportion to the bytes it still owes rather than a flat ten minutes.
        A body that moves bytes and never declares them is judged on the floor,
        which is why the declaration belongs at the top of the body and not
        beside the first chunk.
        """
        self._bytes = 0
        self._bytes_since_beat = 0
        await self._ops._beat(
            self._op_id, self._done, extra={"byte_budget": max(0, total), "bytes": 0}
        )

    async def bytes_moved(self, count: int) -> None:
        """Count ``count`` bytes through the body; publish every N.

        Publishing is what turns a long write into an observable one: the beat
        both moves the poll's byte figure and proves the runner is alive, so a
        promote that is streaming is never mistaken for one that has died.
        """
        self._bytes += count
        self._bytes_since_beat += count
        if self._bytes_since_beat >= BYTE_TICK_EVERY:
            self._bytes_since_beat = 0
            await self._ops._beat(self._op_id, self._done, extra={"bytes": self._bytes})

    async def cancelled(self) -> bool:
        """Has a cancel been asked for? Poll this at a batch boundary."""
        return (await self._ops.get(self._op_id)).cancel_requested

    async def stop_if_cancelled(self) -> None:
        """Raise :class:`OperationCancelled` if one was asked for."""
        if await self.cancelled():
            raise OperationCancelled(str(self._op_id))


# ---- the attribute mutation ---------------------------------------------

#: The POSIX attribute columns a caller may set. Anything else is derived (the
#: ctime) or structural (the name, the parent) and has its own mutation.
ATTR_COLUMNS: Final = ("mode", "uid", "gid", "atime_ns", "mtime_ns")

#: ``xattrs`` is settable too, but it is a JSONB document rather than a scalar
#: column, so it is spelled apart from the columns whose assignment is uniform.
SETTABLE_ATTRS: Final = (*ATTR_COLUMNS, "xattrs")


def _validated_xattrs(raw: Any) -> dict[str, bytes | str]:
    """The xattr document a caller may set, or the reason it is refused.

    The ``user.`` restriction is the point: ``system.``, ``security.`` and
    ``trusted.`` are kernel-owned namespaces a materializer could not restore
    without privilege, so accepting one would promise a round trip that cannot
    happen. The size limits are the schema's, applied here as well because the
    library is reachable without the wire model in front of it.
    """
    if not isinstance(raw, dict):
        raise InvalidRequest("xattrs must be a mapping of name to value")
    named: dict[str, bytes | str] = {}
    for name, value in raw.items():
        if not isinstance(name, str) or not name.startswith(USER_XATTR_PREFIX):
            raise InvalidRequest(f"xattr {name!r} is outside the {USER_XATTR_PREFIX!r} namespace")
        if not isinstance(value, bytes | str):
            raise InvalidRequest(f"xattr {name!r} is not bytes")
        named[name] = value
    try:
        validate_xattrs({name: _as_bytes(value) for name, value in named.items()})
    except ValueError as exc:
        raise InvalidRequest(str(exc)) from exc
    return named


def _as_bytes(value: bytes | str) -> bytes:
    """The raw bytes behind a value the caller sent, or an inverse replayed."""
    return value if isinstance(value, bytes) else base64.b64decode(value.encode("ascii"))


async def set_attrs(
    repo: FilesRepo,
    ctx: ActingContext,
    node_id: NodeId,
    attrs: dict[str, Any],
    *,
    op: Operations | None = None,
    lease: LeaseContext | None = None,
) -> None:
    """Set a node's POSIX attributes, recording the previous ones as the inverse.

    Lives here rather than in ``namespace`` because it is the one mutation with
    no structural effect: the tree is unchanged and only the stat block moves,
    so its inverse is simply the values it replaced.

    Fenced anyway, and for the same reason a rename is: the stat block is part
    of what a mount holds, a mode or an xattr the holder never set lands on its
    disk at the next pull, and the counter this bumps is the etag every other
    writer's precondition is read against. ``into`` stays false — this changes
    the node itself, so even a lease that takes inbound writes refuses it.

    ``xattrs`` is a whole-document replace, not a merge, because that is what a
    filesystem's own ``setxattr`` sweep produces and it is the only shape whose
    inverse is the document it replaced — a merge would have no way to record
    the removal of a name. Values arrive as bytes and are stored as the base64
    text the wire renders, so one spelling goes in and the same one comes out.
    """
    unknown = set(attrs) - set(SETTABLE_ATTRS)
    if unknown:
        raise InvalidRequest(f"not settable attributes: {sorted(unknown)}")
    node = await repo.node(node_id)
    if node is None:
        raise NotFound(f"no node {node_id}")
    covering = await fenced_write_for(repo, node, lease)
    before = {name: getattr(node, name) for name in attrs}
    columns = {name: value for name, value in attrs.items() if name != "xattrs"}
    assignments = [f"{name} = :{name}" for name in columns]
    params: dict[str, Any] = {"id": node_id, "org": repo.scope.org_team_id, **columns}
    if "xattrs" in attrs:
        xattrs = _validated_xattrs(attrs["xattrs"])
        assignments.append("xattrs = CAST(:xattrs AS jsonb)")
        params["xattrs"] = _json(stored_xattrs(xattrs))
    await repo.session.execute(
        text(
            # S608: the assignment list is built from SETTABLE_ATTRS only — an
            # attribute the caller named that is not in it was refused above.
            f"UPDATE file_nodes SET {', '.join(assignments)}, "  # noqa: S608
            "etag = etag + 1, updated_at = now() "
            "WHERE id = :id AND org_team_id = :org"
        ),
        params,
    )
    await repo.session.refresh(node)
    if op is not None:
        await op.record_inverse(AttrsInverse(node_id=node_id, attrs=_snapshot(before)))
    await history.record(
        repo,
        ctx,
        node_id=node_id,
        kind="attrs",
        before=_snapshot(before),
        after=_snapshot(attrs),
    )
    # The holder restoring the stat block of a file it just saved is the tail
    # of that save, not a person's change: said as ``live_saved`` and with the
    # folder named, a reader refreshes the file and its listing and nothing
    # else. Reasonless, it read as a person's action and every open portal
    # refetched its chat list and every folder listing after each box save.
    await history.emit_node_changed(
        repo,
        ctx,
        node_id=node_id,
        drive_id=DriveId(node.drive_id),
        version=node.etag,
        parent_id=None if node.parent_id is None else NodeId(node.parent_id),
        reason="live_saved" if is_hand_back(covering, lease) else None,
    )


def _snapshot(values: dict[str, Any]) -> dict[str, Any]:
    """The attribute set as JSON. An xattr value is arbitrary bytes, so it takes
    the same base64 spelling the column and the wire use — the history row and
    the recorded inverse have to be readable back by the same reader."""
    return {
        name: stored_xattrs(value) if name == "xattrs" else value for name, value in values.items()
    }


def _dump(inverse: Inverse | None) -> str | None:
    return None if inverse is None else inverse.model_dump_json()


def _json(value: Any) -> str:
    import json

    return json.dumps(value)


__all__ = [
    "ATTR_COLUMNS",
    "BYTE_TICK_EVERY",
    "DEFAULT_TICK_EVERY",
    "HEARTBEAT_DEADLINE",
    "KNOWN_INVERSES",
    "QUEUED_STALE_AFTER",
    "RECOVERY_ATTEMPTS_KEY",
    "RUNNER_LOST_CODE",
    "RUNNER_NUDGED",
    "RUNNER_PRESENT",
    "SETTABLE_ATTRS",
    "WATCHDOG_BYTES_PER_SECOND",
    "AttrsInverse",
    "Inverse",
    "MoveInverse",
    "OperationCancelled",
    "OperationInverse",
    "OperationState",
    "Operations",
    "Progress",
    "QueuedRecovery",
    "RawInverse",
    "RecoveredQueued",
    "RecoveryOutcome",
    "RenameInverse",
    "RestoreInverse",
    "TrashInverse",
    "parse_inverse",
    "set_attrs",
]
