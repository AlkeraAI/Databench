"""Quota holds — the only writer of ``file_upload_sessions.quota_hold_*``.

A drive's room is decided once, in one place, so ``uploads.open`` and
``content.put_version`` cannot disagree about it. The decision is
``used + held + wanted <= quota``, where:

* **used** is the drive root's folded ``file_dir_stats`` row PLUS the delta rows
  beneath it the aggregator has not folded yet — the write path appends against
  the changed node's parent and never against the root, so the folded row alone
  trails by whatever was committed since the last pass and a burst could
  overshoot the ceiling. Both halves are read in the SAME statement; the node
  reference is a drive-id equality on ``file_nodes``, not a walk of the subtree,
  so the check stays inside its 10 ms budget no matter how many nodes the
  drive holds;
* **held** is the sum of the quota columns on the drive's *live* upload
  sessions — the second indexed read.

The hold lives on the session row rather than in a table of its own precisely so
``Σ holds == Σ open sessions`` is true by construction: a hold cannot outlive the
session that took it, because it *is* the session. Only the state machine takes
a hold out of the sum, so an expired session still holds its bytes until the
upload sweep (``UploadCompletion.sweep_expired``) flips it.

Two opens that would together overflow the drive are serialized on the drive row
(``SELECT … FOR UPDATE``, the first lock in the fixed order drive → parent → node
→ version), so exactly one of them gets the room and the other is refused before
a single byte is accepted. Session opens are rare relative to writes, so the
drive row does not become hot.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Final, cast

from sqlalchemy import CursorResult, Select, Table, and_, func, literal, select, update
from sqlalchemy.orm import InstrumentedAttribute
from sqlalchemy.sql.expression import ColumnElement

from alkera_core.authz.principal import ActingContext
from alkera_core.files.checkpoints import Checkpoints, NoopCheckpoints
from alkera_core.files.clock import Clock
from alkera_core.files.errors import Conflict, NotFound, QuotaExceeded
from alkera_core.files.history import subject_ref
from alkera_core.files.ids import DriveId, NodeId, SessionId
from alkera_core.files.repo import FilesRepo, charged_bytes
from alkera_core.files.store.scoped import DomainStore
from alkera_core.models.files.history import FileDirStats, FileDirStatsDelta
from alkera_core.models.files.stores import FileDrive
from alkera_core.models.files.tree import FileNode
from alkera_core.models.files.uploads import UPLOAD_SESSION_LIVE_STATES, FileUploadSession
from alkera_core.units import format_bytes

#: The default checkpoint seam: production reaches no pause points.
_NO_CHECKPOINTS: Final[Checkpoints] = NoopCheckpoints()

#: The tables the compare-and-swap statements write. The mapped class is not
#: used because a bare ``UPDATE`` must not go through the ORM's unit of work.
_DRIVES: Final[Table] = cast(Table, FileDrive.__table__)
_SESSIONS: Final[Table] = cast(Table, FileUploadSession.__table__)

#: ``file_drives.frozen_reason`` when the drive is over its byte or node quota.
OVER_QUOTA: Final = "over_quota"
#: The conflict a write on a frozen drive is refused with.
FROZEN_CODE: Final = "files.frozen"
#: The refusal when the CALLER's own bytes, not the drive's, are at the ceiling.
USER_QUOTA_CODE: Final = "files.user_quota_bytes"


@dataclass(frozen=True, slots=True)
class UserCeiling:
    """One limit on the bytes a member may own, org-wide or inside one team folder."""

    limit_bytes: int
    #: ``org`` for the org-wide limit, ``team`` for one under a team folder.
    scope: str
    team_id: uuid.UUID | None = None
    #: The team folder's ``path_ids``; ``None`` is the whole drive.
    scope_path: str | None = None
    #: The team whose allowance or cap this is, named so a refusal can say
    #: which limit it ran into; ``None`` for a member's org-wide cap.
    team_name: str | None = None

    def covers(self, path: str | None) -> bool:
        """Whether a write into the folder at ``path`` falls under this limit."""
        if self.scope_path is None:
            return True
        if path is None:
            return False
        return path == self.scope_path or path.startswith(self.scope_path + ".")

    def refusal(self, *, own: int, wanted: int) -> QuotaExceeded:
        """The 507 a write of ``wanted`` bytes meets when the member already
        owns ``own`` under this limit: it names the limit, the team that set
        it, what is left, and what the write needed — the facts that tell a
        person whether deleting something will help, and whom to ask."""
        left = max(0, self.limit_bytes - own)
        label = "storage limit" if self.team_name is None else f"{self.team_name} storage limit"
        room = f"{format_bytes(left)} is left" if left > 0 else "nothing is left"
        if wanted > 0:
            message = (
                f"This file needs {format_bytes(wanted)}; "
                f"your {label} is {format_bytes(self.limit_bytes)} and {room}."
            )
        else:
            message = f"Your {label} is {format_bytes(self.limit_bytes)} and {room}."
        return QuotaExceeded(
            message,
            code=USER_QUOTA_CODE,
            kind="bytes",
            detail={
                "scope": self.scope,
                "limit_bytes": self.limit_bytes,
                "used_bytes": own,
                "remaining_bytes": left,
                "needed_bytes": wanted,
                "team_id": None if self.team_id is None else str(self.team_id),
                "team_name": self.team_name,
            },
        )


@dataclass(frozen=True, slots=True)
class Ceilings:
    """The ceilings that apply to one caller's writes on one drive.

    ``org_bytes`` replaces the drive row's own figure when a resolver is
    installed — it is the platform override, else the plan's included storage,
    else that row — and ``None`` there is "unlimited". ``users`` are the
    caller's own limits; every one that covers the destination is checked.
    """

    org_bytes: int | None = None
    users: tuple[UserCeiling, ...] = ()


#: Resolves the ceilings lazily, only when a write actually asks. Production
#: installs one that reads the org's override and plan and the caller's own
#: limit rows; a service built without one is bounded by the drive row alone.
CeilingsResolver = Callable[[], Awaitable[Ceilings]]


@dataclass(frozen=True, slots=True)
class Hold:
    """The room one upload session has reserved on its drive."""

    session_id: SessionId
    bytes: int
    nodes: int


@dataclass(frozen=True, slots=True)
class Usage:
    """What a drive has spent, what is reserved, and what it is allowed."""

    bytes: int
    nodes: int
    held_bytes: int
    held_nodes: int
    quota_bytes: int
    quota_nodes: int

    @property
    def over_quota(self) -> bool:
        """Whether committed usage alone already exceeds either ceiling.

        Holds are deliberately excluded: a drive is frozen for what it stores,
        not for what someone is in the middle of uploading — a hold that is
        never committed would otherwise freeze the drive until it expired.
        """
        return self.bytes > self.quota_bytes or self.nodes > self.quota_nodes


def _held(exclude: SessionId | None, column: InstrumentedAttribute[int]) -> ColumnElement[int]:
    """Σ of one hold column over the drive's live sessions, as a subquery.

    Correlated on ``FileDrive`` so it rides along with the drive read instead of
    costing a round trip; the org predicate is spelled here because a correlated
    subquery is not what ``execute_scoped`` scopes.
    """
    conditions: list[ColumnElement[bool]] = [
        FileUploadSession.drive_id == FileDrive.id,
        FileUploadSession.org_team_id == FileDrive.org_team_id,
        FileUploadSession.state.in_(UPLOAD_SESSION_LIVE_STATES),
    ]
    if exclude is not None:
        conditions.append(FileUploadSession.id != exclude)
    return (
        select(func.coalesce(func.sum(column), 0))
        .where(*conditions)
        .correlate(FileDrive.__table__)
        .scalar_subquery()
    )


def _stat(column: InstrumentedAttribute[int]) -> ColumnElement[int]:
    """One aggregated stat for the drive's root folder, as a subquery."""
    return (
        select(column)
        .where(
            FileDirStats.node_id == FileDrive.root_node_id,
            FileDirStats.org_team_id == FileDrive.org_team_id,
        )
        .correlate(FileDrive.__table__)
        .scalar_subquery()
    )


def _pending(column: InstrumentedAttribute[int]) -> ColumnElement[int]:
    """Σ of one delta column over the drive's not-yet-folded rows.

    A delta names the changed node's parent, so every unfolded row for this
    drive is a node of this drive: the correlation is the indexed
    ``file_nodes.drive_id`` equality, never a ``path_ids`` walk of the subtree.
    """
    return (
        select(func.coalesce(func.sum(column), 0))
        .select_from(FileDirStatsDelta)
        .join(
            FileNode,
            and_(
                FileNode.id == FileDirStatsDelta.node_id,
                FileNode.org_team_id == FileDirStatsDelta.org_team_id,
            ),
        )
        .where(
            FileNode.drive_id == FileDrive.id,
            FileDirStatsDelta.org_team_id == FileDrive.org_team_id,
        )
        .correlate(FileDrive.__table__)
        .scalar_subquery()
    )


def _used(
    folded: InstrumentedAttribute[int], pending: InstrumentedAttribute[int]
) -> ColumnElement[int]:
    """The quota's *used*: the folded drive-root total plus the unfolded remainder."""
    return func.coalesce(_stat(folded), 0) + _pending(pending)


def _owned(
    subject: uuid.UUID | None, drive_id: DriveId, org_team_id: uuid.UUID | None
) -> ColumnElement[int]:
    """Σ size of the live files charged to ``subject`` in the drive — the one
    figure the member's own reading shows (:func:`charged_bytes`) — riding
    the same round trip; ``0`` for a caller nothing is attributed to."""
    if subject is None or org_team_id is None:
        return literal(0)
    return charged_bytes(subject, drive_id=drive_id, org_team_id=org_team_id)


def _owned_held(subject: uuid.UUID | None, exclude: SessionId | None) -> ColumnElement[int]:
    """Σ of the byte holds on ``subject``'s own live sessions in the drive."""
    if subject is None:
        return literal(0)
    conditions: list[ColumnElement[bool]] = [
        FileUploadSession.drive_id == FileDrive.id,
        FileUploadSession.org_team_id == FileDrive.org_team_id,
        FileUploadSession.created_by == subject,
        FileUploadSession.state.in_(UPLOAD_SESSION_LIVE_STATES),
    ]
    if exclude is not None:
        conditions.append(FileUploadSession.id != exclude)
    return (
        select(func.coalesce(func.sum(FileUploadSession.quota_hold_bytes), 0))
        .where(*conditions)
        .correlate(FileDrive.__table__)
        .scalar_subquery()
    )


def _usage_statement(
    drive_id: DriveId,
    *,
    exclude: SessionId | None = None,
    subject: uuid.UUID | None = None,
    org_team_id: uuid.UUID | None = None,
) -> Select[tuple[int, int, int, int, int, int, str | None, int, int]]:
    """The whole quota picture for one drive, in a single statement.

    Every aggregate hangs off the drive row as a correlated subquery, so the
    check is a handful of indexed reads. ``file_nodes`` is named once, by
    the pending sum, and only as a ``drive_id`` equality — a lookup, never a
    walk of the tree.
    """
    return select(
        _used(FileDirStats.bytes, FileDirStatsDelta.bytes_delta),
        _used(FileDirStats.files, FileDirStatsDelta.files_delta),
        _held(exclude, FileUploadSession.quota_hold_bytes),
        _held(exclude, FileUploadSession.quota_hold_nodes),
        FileDrive.quota_bytes,
        FileDrive.quota_nodes,
        FileDrive.frozen_reason,
        _owned(subject, drive_id, org_team_id),
        _owned_held(subject, exclude),
    ).where(FileDrive.id == drive_id)


def _over_quota_predicate(drive_id: DriveId) -> ColumnElement[bool]:
    """``used > quota`` on either axis, evaluated inside a CAS statement."""
    used_bytes = _used(FileDirStats.bytes, FileDirStatsDelta.bytes_delta)
    used_nodes = _used(FileDirStats.files, FileDirStatsDelta.files_delta)
    return (used_bytes > FileDrive.quota_bytes) | (used_nodes > FileDrive.quota_nodes)


class QuotaService:
    """Reserve, release and reconcile a drive's quota holds."""

    def __init__(
        self,
        repo: FilesRepo,
        ctx: ActingContext,
        clock: Clock,
        store: DomainStore | None = None,
        *,
        checkpoints: Checkpoints = _NO_CHECKPOINTS,
        ceilings: CeilingsResolver | None = None,
    ) -> None:
        self._repo = repo
        self._ctx = ctx
        self._clock = clock
        self._store = store
        self._checkpoints = checkpoints
        self._resolve_ceilings = ceilings
        self._ceilings: Ceilings | None = None

    async def ceilings(self) -> Ceilings | None:
        """The resolved ceilings, read once per service; ``None`` without a resolver."""
        if self._resolve_ceilings is None:
            return None
        if self._ceilings is None:
            self._ceilings = await self._resolve_ceilings()
        return self._ceilings

    async def assert_room(
        self,
        drive_id: DriveId,
        *,
        bytes: int,
        nodes: int,
        parent_path: str | None,
        exclude: SessionId | None = None,
    ) -> None:
        """Refuse a write that would land past a ceiling, or that starts at one.

        A write that adds bytes or nodes is refused once usage is *at* the
        ceiling, not only past it: an org or a member at their limit is in
        safety mode, where nothing new is created until usage drops below
        again. A reserve that adds nothing is never refused by that rule. The
        caller's own limits are asked first, then the drive's, so a member over
        their own ceiling is told so even while the org is over its ceiling
        too — that is the limit they can do something about.

        Everything drive-wide comes back in the one usage round trip; only a
        limit scoped to a team folder costs a second, indexed read.
        """
        resolved = await self.ceilings()
        subject = None if resolved is None or not resolved.users else subject_ref(self._ctx)
        row = (
            await self._repo.execute_scoped(
                _usage_statement(
                    drive_id,
                    exclude=exclude,
                    subject=subject,
                    org_team_id=self._repo.scope.org_team_id,
                )
            )
        ).one()
        adds = bytes > 0 or nodes > 0
        if resolved is not None:
            for ceiling in resolved.users:
                if not ceiling.covers(parent_path):
                    continue
                if ceiling.scope_path is None:
                    own = row[7] + row[8]
                else:
                    own = await self._repo.owned_bytes_under(
                        drive_id, subject_ref(self._ctx), ceiling.scope_path
                    )
                if (adds and own >= ceiling.limit_bytes) or own + bytes > ceiling.limit_bytes:
                    raise ceiling.refusal(own=own, wanted=bytes)
        org_ceiling: int | None = row[4] if resolved is None else resolved.org_bytes
        used_bytes = row[0] + row[2]
        if org_ceiling is not None and (
            (adds and used_bytes >= org_ceiling) or used_bytes + bytes > org_ceiling
        ):
            raise QuotaExceeded(kind="bytes")
        if row[1] + row[3] + nodes > row[5]:
            raise QuotaExceeded(kind="nodes")

    async def usage(self, drive_id: DriveId) -> Usage:
        """The drive's committed usage, its live holds and its ceilings."""
        row = (await self._repo.execute_scoped(_usage_statement(drive_id))).one_or_none()
        if row is None:
            raise NotFound(message=f"drive {drive_id}")
        return Usage(
            bytes=row[0],
            nodes=row[1],
            held_bytes=row[2],
            held_nodes=row[3],
            quota_bytes=row[4],
            quota_nodes=row[5],
        )

    async def assert_scope_entry(
        self,
        drive_id: DriveId,
        *,
        bytes: int,
        source_path: str | None,
        dest_path: str | None,
    ) -> None:
        """Refuse a move that carries the caller's bytes into a scope that is
        at, or would go past, one of their ceilings.

        A move changes nothing for the drive and nothing for a limit that
        covers both folders — the same bytes, still theirs — so only the
        ceilings that cover the destination and not the source are asked. A
        move out of a limited folder is therefore never refused.
        """
        resolved = await self.ceilings()
        if resolved is None:
            return
        for ceiling in resolved.users:
            if ceiling.scope_path is None or not ceiling.covers(dest_path):
                continue
            if ceiling.covers(source_path):
                continue
            own = await self._repo.owned_bytes_under(
                drive_id, subject_ref(self._ctx), ceiling.scope_path
            )
            if own >= ceiling.limit_bytes or own + bytes > ceiling.limit_bytes:
                raise ceiling.refusal(own=own, wanted=bytes)

    async def reserve(
        self,
        drive_id: DriveId,
        *,
        bytes: int,
        nodes: int,
        session_id: SessionId,
        parent_path: str | None = None,
        unbounded: bool = False,
    ) -> Hold:
        """Take room on the drive, or refuse before any byte is accepted.

        The drive row is locked first so two concurrent opens see each other's
        holds: whichever gets the lock second reads the first one's hold and is
        refused, so the two cannot both spend the same free space.

        ``unbounded`` is the lease hand-back: a box materializing a chat folder
        back into Files must land even past every ceiling, because the bytes
        already exist and refusing them would lose work. The hold is still
        taken, so the usage that results is counted and the next ordinary write
        is what safety mode refuses.
        """
        if bytes < 0 or nodes < 0:
            raise QuotaExceeded(kind="bytes" if bytes < 0 else "nodes")
        drive = await self._repo.lock_drive(drive_id)
        if drive is None:
            raise NotFound(message=f"drive {drive_id}")
        await self._checkpoints.reach("quota.after_lock")
        frozen = drive.frozen_reason
        if frozen is not None and not (unbounded and frozen == OVER_QUOTA):
            raise Conflict(FROZEN_CODE, f"drive {drive_id} is {frozen}")
        if not unbounded:
            # The session's own row may already exist in this transaction; its
            # current hold is being replaced, so it must not count against itself.
            await self.assert_room(
                drive_id, bytes=bytes, nodes=nodes, parent_path=parent_path, exclude=session_id
            )
        return Hold(session_id=session_id, bytes=bytes, nodes=nodes)

    async def reserve_on_session(
        self,
        session: FileUploadSession,
        *,
        parent_path: str | None = None,
        unbounded: bool = False,
    ) -> Hold:
        """Reserve for a session row the caller created in this transaction.

        The hold columns are set here and nowhere else, which is what makes the
        row the single truth for ``Σ holds``.
        """
        hold = await self.reserve(
            DriveId(session.drive_id),
            bytes=session.declared_size,
            nodes=1,
            session_id=SessionId(session.id),
            parent_path=parent_path,
            unbounded=unbounded,
        )
        session.quota_hold_bytes = hold.bytes
        session.quota_hold_nodes = hold.nodes
        await self._repo.flush()
        return hold

    async def release(self, session_id: SessionId) -> None:
        """Zero a session's hold, in the transaction that ends the session."""
        await self._update_hold(session_id, bytes=0, nodes=0)

    async def reconcile(
        self, session_id: SessionId, *, actual_bytes: int, actual_nodes: int
    ) -> None:
        """Turn a hold into committed usage.

        The hold goes to zero and the same amounts land as a ``file_dir_stats``
        delta on the folder the session was opened against, so the drive's total
        never dips between the two — a reader inside this transaction sees the
        hold released and the bytes counted (the quota read sums the unfolded
        rows), never a moment where neither holds them.
        """
        _, parent_id = await self._session_target(session_id)
        await self._update_hold(session_id, bytes=0, nodes=0)
        await self._add_stats(parent_id, bytes_delta=actual_bytes, files_delta=actual_nodes)

    async def settle_head_swap(
        self, drive_id: DriveId, parent_id: NodeId, *, bytes_delta: int, files_delta: int = 0
    ) -> None:
        """Account a head swap that never went through an upload session.

        A restore and a conflict resolution both republish bytes that are
        already in the store: no session is opened, so no hold is reconciled and
        nothing else in the write path would ever count the size change. This is
        the one seam for that — the delta row and the cache fold that
        :meth:`reconcile` performs, followed by the freeze decision, both
        directions, because a head swap can move usage either way and only one
        of the two can win. ``files_delta`` is 1 for a conflicted copy's first
        head: a recount counts a file once it has one, whoever published it.
        """
        if bytes_delta or files_delta:
            await self._add_stats(parent_id, bytes_delta=bytes_delta, files_delta=files_delta)
        if not await self.freeze_if_over(drive_id):
            await self.thaw_if_under(drive_id)

    async def freeze_if_over(self, drive_id: DriveId) -> bool:
        """Freeze the drive iff its committed usage is over either ceiling.

        One compare-and-swap: the predicate is evaluated by Postgres against the
        row it is about to write, so two callers cannot both decide "not frozen"
        and race a thaw in between.
        """
        statement = (
            update(_DRIVES)
            .where(
                FileDrive.id == drive_id,
                FileDrive.org_team_id == self._repo.scope.org_team_id,
                FileDrive.frozen_reason.is_(None),
                _over_quota_predicate(drive_id),
            )
            .values(frozen_reason=OVER_QUOTA)
        )
        result = cast(CursorResult[Any], await self._repo.session.execute(statement))
        return int(result.rowcount) > 0

    async def thaw_if_under(self, drive_id: DriveId) -> bool:
        """Clear an ``over_quota`` freeze once usage is back under the ceiling.

        Only that reason is cleared: a ``teardown`` freeze is not a quota
        decision and must survive a delete.
        """
        statement = (
            update(_DRIVES)
            .where(
                FileDrive.id == drive_id,
                FileDrive.org_team_id == self._repo.scope.org_team_id,
                FileDrive.frozen_reason == OVER_QUOTA,
                ~_over_quota_predicate(drive_id),
            )
            .values(frozen_reason=None)
        )
        result = cast(CursorResult[Any], await self._repo.session.execute(statement))
        return int(result.rowcount) > 0

    # ---- internals -------------------------------------------------------

    async def _session_target(self, session_id: SessionId) -> tuple[DriveId, NodeId]:
        """The session's drive and the folder its bytes land in."""
        statement = select(FileUploadSession.drive_id, FileUploadSession.parent_id).where(
            FileUploadSession.id == session_id
        )
        row = (await self._repo.execute_scoped(statement)).one_or_none()
        if row is None:
            raise NotFound(message=f"upload session {session_id}")
        return DriveId(row[0]), NodeId(row[1])

    async def _update_hold(self, session_id: SessionId, *, bytes: int, nodes: int) -> None:
        statement = (
            update(_SESSIONS)
            .where(
                FileUploadSession.id == session_id,
                FileUploadSession.org_team_id == self._repo.scope.org_team_id,
            )
            .values(quota_hold_bytes=bytes, quota_hold_nodes=nodes)
        )
        await self._repo.session.execute(statement)

    async def _add_stats(self, node_id: NodeId, *, bytes_delta: int, files_delta: int) -> None:
        """Append the delta row against the changed node's PARENT.

        The root is never charged directly and the cache is never folded here:
        :func:`alkera_core.files.stats.aggregate` carries every delta up the
        path to the root, so a direct root charge would be counted twice once
        the job folded the same row. The quota decision does not wait for that
        job — it reads the folded row plus these unfolded rows in one statement,
        so a burst still cannot overshoot the ceiling.
        """
        await self._repo.add(
            FileDirStatsDelta(
                org_team_id=self._repo.scope.org_team_id,
                node_id=node_id,
                bytes_delta=bytes_delta,
                files_delta=files_delta,
                direct_children_delta=0,
            )
        )
        await self._repo.flush()


__all__ = [
    "FROZEN_CODE",
    "OVER_QUOTA",
    "USER_QUOTA_CODE",
    "Ceilings",
    "CeilingsResolver",
    "Hold",
    "QuotaService",
    "Usage",
    "UserCeiling",
]
