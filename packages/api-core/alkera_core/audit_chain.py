"""The org audit trail's hash chain: how a row is hashed, appended and resealed.

``org_audit_events`` is tamper-evident: each chained row stores
``entry_hash = SHA-256(canonical(content) || prev_hash)``, so an in-place edit
breaks verification at that row. The backend's ``org_audit.record`` appends
through :func:`append_row` (adding its redaction policy on top), and account
erasure, which runs in the worker, rewrites the erased person's actor fields and
then calls :func:`reseal` to recompute the chain from the first rewritten row.

A reseal is never silent: the caller appends an ``audit.chain_resealed`` row
naming the old and new head, so an external checkpoint of the old head is
explained inside the chain itself.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from uuid import UUID

from sqlalchemy import Select, event, inspect, select
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from alkera_core.db.locking import (
    AdvisoryKey,
    advisory_key,
    advisory_xact_lock,
    advisory_xact_lock_at_commit,
    advisory_xact_lock_sync,
)
from alkera_core.db.tenant_session import stepped_out_at_commit
from alkera_core.models import OrgAuditEvent


def hash_event(
    *,
    org_id: UUID,
    actor_id: UUID | None,
    actor_email: str,
    action: str,
    target: str | None,
    detail: dict[str, Any] | None,
    created_at: datetime,
    prev_hash: str,
) -> str:
    """SHA-256 over the row's canonical content linked to ``prev_hash``. Stable
    across a JSONB round-trip (``sort_keys`` makes key order irrelevant), so the
    digest recomputed at verify-time matches the one written."""
    payload = json.dumps(
        {
            "org": str(org_id),
            "actor_id": str(actor_id) if actor_id is not None else None,
            "actor_email": actor_email,
            "action": action,
            "target": target,
            "detail": detail,
            "created_at": created_at.isoformat(),
            "prev": prev_hash,
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


async def lock_chain(db: AsyncSession, org_id: UUID) -> None:
    """Serialize every write to one org's chain (seals and reseals), so two
    writers cannot read the same tail and fork it. Transaction-scoped."""
    await advisory_xact_lock(db, chain_key(org_id))


def lock_chain_on(bind: Connection, org_id: UUID) -> None:
    """:func:`lock_chain` on a synchronous connection, for a migration that
    appends to an org's chain while the app may be running beside it."""
    advisory_xact_lock_sync(bind, chain_key(org_id))


def chain_key(org_id: UUID) -> AdvisoryKey:
    """The advisory lock one org's chain is sealed under."""
    return advisory_key("audit-chain", org_id)


async def chain_tail(db: AsyncSession, org_id: UUID) -> tuple[str, datetime | None]:
    """The newest chained row's ``(entry_hash, created_at)`` for the org —
    ``("", None)`` at genesis. ``created_at`` is strictly monotonic per org, so
    it is the chain's total order."""
    row = (await db.execute(_tail_statement(org_id))).first()
    if row is None:
        return "", None
    return row[0] or "", row[1]


def _tail_statement(org_id: UUID) -> Select[tuple[str | None, datetime]]:
    return (
        select(OrgAuditEvent.entry_hash, OrgAuditEvent.created_at)
        .where(OrgAuditEvent.org_team_id == org_id, OrgAuditEvent.entry_hash.is_not(None))
        .order_by(OrgAuditEvent.created_at.desc())
        .limit(1)
    )


#: Where a session keeps the rows it appended and has not sealed yet.
_PENDING: Final = "alkera.audit_chain.pending"


async def append_row(
    db: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID | None,
    actor_email: str,
    action: str,
    target: str | None,
    detail: dict[str, Any] | None,
) -> OrgAuditEvent:
    """Append one already-redacted event to the org's chain, in the caller's
    transaction.

    The row is written now and linked into the chain as its transaction
    commits (:func:`_seal_at_commit`): the chain's lock is held for the commit
    alone, never from here to the end of the request, so audited writes in one
    org do not queue behind each other's work. A committed row is always
    sealed; one written by a transaction that rolls back never existed.
    """
    appended = OrgAuditEvent(
        org_team_id=org_id,
        actor_id=actor_id,
        actor_email=actor_email,
        action=action,
        target=target,
        detail=detail,
        created_at=datetime.now(UTC),
        prev_hash=None,
        entry_hash=None,
    )
    db.add(appended)
    await db.flush()
    db.sync_session.info.setdefault(_PENDING, []).append(appended)
    return appended


async def seal_now(db: AsyncSession, org_id: UUID) -> None:
    """Seal the org's rows this transaction appended, now rather than at
    commit, for a caller that reads a row's hash before it commits (the
    reseal marker). The caller holds :func:`lock_chain`."""
    await db.run_sync(_seal_pending, only=org_id)


def _seal_pending(session: Session, *, only: UUID | None = None) -> None:
    """Link every row ``session`` appended (of ``only``'s org, when named)
    onto its org's chain: the chain's lock, the tail read under it, then each
    row's ``created_at`` pushed past the tail and its hashes written, in the
    order the rows were appended. Orgs are sealed in ascending id, so two
    transactions that audit the same two orgs cannot wait on each other."""
    pending: list[OrgAuditEvent] = session.info.get(_PENDING, [])
    kept: list[OrgAuditEvent] = []
    by_org: dict[UUID, list[OrgAuditEvent]] = {}
    for row in pending:
        state = inspect(row)
        if not state.persistent or row.entry_hash is not None:
            continue  # rolled back with a savepoint, or sealed already
        if only is not None and row.org_team_id != only:
            kept.append(row)
            continue
        by_org.setdefault(row.org_team_id, []).append(row)
    session.info[_PENDING] = kept
    if not by_org:
        return
    # The commit may come while the Files role is in force (a repo's own
    # transaction commits under it), and that role holds no grant on the
    # audit table: the chain is read and written as the role outside it.
    with stepped_out_at_commit(session):
        _seal_orgs(session, by_org)


def _seal_orgs(session: Session, by_org: dict[UUID, list[OrgAuditEvent]]) -> None:
    for org_id in sorted(by_org, key=str):
        advisory_xact_lock_at_commit(session, chain_key(org_id))
        tail = session.execute(_tail_statement(org_id)).first()
        prev = (tail[0] or "") if tail is not None else ""
        last = tail[1] if tail is not None else None
        for row in by_org[org_id]:
            created_at = row.created_at
            if last is not None and created_at <= last:
                created_at = last + timedelta(microseconds=1)
            row.created_at = created_at
            row.prev_hash = prev
            row.entry_hash = hash_event(
                org_id=org_id,
                actor_id=row.actor_id,
                actor_email=row.actor_email,
                action=row.action,
                target=row.target,
                detail=row.detail,
                created_at=created_at,
                prev_hash=prev,
            )
            prev, last = row.entry_hash, created_at
    session.flush()


@event.listens_for(Session, "before_commit")
def _seal_at_commit(session: Session) -> None:
    # A savepoint's release is not the commit: sealing there would hold the
    # chain's lock from the savepoint to the end of the transaction.
    if session.info.get(_PENDING) and not session.in_nested_transaction():
        _seal_pending(session)


@event.listens_for(Session, "after_rollback")
def _forget_at_rollback(session: Session) -> None:
    session.info.pop(_PENDING, None)


@dataclass(frozen=True, slots=True)
class Reseal:
    """What a reseal changed: how many rows it rehashed and the head before/after."""

    rows: int
    old_head: str | None
    new_head: str | None


async def reseal(db: AsyncSession, org_id: UUID, *, since: datetime) -> Reseal:
    """Recompute ``prev_hash``/``entry_hash`` for every chained row of the org
    from the first one at or after ``since``, in order, in the caller's
    transaction. The caller holds :func:`lock_chain` and has already rewritten
    the rows it needed to; rows before ``since`` keep their hashes and anchor
    the recomputation."""
    old_head, _ = await chain_tail(db, org_id)
    anchor = (
        await db.execute(
            select(OrgAuditEvent.entry_hash)
            .where(
                OrgAuditEvent.org_team_id == org_id,
                OrgAuditEvent.entry_hash.is_not(None),
                OrgAuditEvent.created_at < since,
            )
            .order_by(OrgAuditEvent.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    prev = anchor or ""
    rows = (
        (
            await db.execute(
                select(OrgAuditEvent)
                .where(
                    OrgAuditEvent.org_team_id == org_id,
                    OrgAuditEvent.entry_hash.is_not(None),
                    OrgAuditEvent.created_at >= since,
                )
                .order_by(OrgAuditEvent.created_at)
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        row.prev_hash = prev
        row.entry_hash = hash_event(
            org_id=row.org_team_id,
            actor_id=row.actor_id,
            actor_email=row.actor_email,
            action=row.action,
            target=row.target,
            detail=row.detail,
            created_at=row.created_at,
            prev_hash=prev,
        )
        prev = row.entry_hash
    await db.flush()
    return Reseal(rows=len(rows), old_head=old_head or None, new_head=prev or None)


__all__ = [
    "Reseal",
    "append_row",
    "chain_key",
    "chain_tail",
    "hash_event",
    "lock_chain",
    "reseal",
    "seal_now",
]
