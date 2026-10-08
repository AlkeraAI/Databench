"""The durable owner of edits a session has not written to its source yet.

A write back is otherwise started by what happens on a socket (an edit, a
writer leaving, the keepalive tick of one that holds the document): a process
that stops between an edit and its write back leaves the edit with nobody to
write it. Every replica sweeps on start and then periodically, and finds such
a session by its row alone (its content hash against the one it last wrote).

A sweep claims what it takes: the rows it picks are locked as it reads them
(another replica's sweep skips them) and marked not due for :data:`CLAIM_SECONDS`,
so each session is tried by one replica at a time. The oldest sessions due a
try go first, and a session whose write back was refused is parked on a
widening wait (:func:`park`), so a session that cannot save neither starves
the others nor is tried again every few seconds.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from alkera_core.logging import get_logger
from alkera_core.models import CrdtDoc
from alkera_core.models.crdt_doc import CRDT_UNSAVED_PREDICATE, stored_doc_type
from sqlalchemy import func, or_, select, text, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.crdt.errors import safe_error
from backend.services.crdt.registry import CrdtRegistry, DocRef

log = get_logger(__name__)

#: How often each replica sweeps, and how many sessions one sweep takes on.
UNSAVED_SWEEP_SECONDS: Final = 10.0
UNSAVED_SWEEP_LIMIT: Final = 200
#: How long a session a sweep took is left to that replica before another
#: may take it again: long enough for its write back to land or be refused.
CLAIM_SECONDS: Final = 60.0
#: A refused session's first wait, doubling per refusal up to the cap.
PARK_FIRST_SECONDS: Final = 30.0
PARK_MAX_SECONDS: Final = 3600.0


#: A file's session nobody has touched for this long, with everything in it
#: on the drive, is deleted: its text and every edit's history are kept only
#: as long as someone is co-editing, and the drive's versions are the record.
DORMANT_DAYS: Final = 30
#: A file's session closed because the file stopped being editable (trashed,
#: too large, gone) is deleted once it has been closed this long.
CLOSED_DAYS: Final = 1
#: How many sessions one pass deletes, so a backlog never holds a long lock.
EXPIRE_LIMIT: Final = 100
#: An unsaved session whose last edit is older than this is backlog: a write
#: back runs seconds after an edit, so one still unsaved this long after is
#: parked or failing, and the edits in it are on no drive.
BACKLOG_AGE_SECONDS: Final = 300.0
#: The most rows one backlog count reads, so the count is bounded however
#: large the backlog grows (it reads the unsaved sessions' partial index).
BACKLOG_COUNT_CAP: Final = 10_000


def park_wait(failures: int) -> float:
    """How long a session refused ``failures`` times in a row waits."""
    return float(min(PARK_FIRST_SECONDS * 2 ** max(failures - 1, 0), PARK_MAX_SECONDS))


async def unsaved(
    factory: Callable[[], AsyncSession],
    registry: CrdtRegistry,
    *,
    limit: int,
    now: datetime,
    orgs: frozenset[uuid.UUID] | None = None,
) -> list[DocRef]:
    """Claim the sessions (of types with a source) whose content is not what
    they last wrote to it and that are due a try at ``now``: at most
    ``limit``, oldest first, in ``orgs`` (every org when None)."""
    sourced = [
        name
        for name in registry.names()
        if (found := registry.get(name)) is not None and found.source is not None
    ]
    if not sourced:
        return []
    query = (
        select(CrdtDoc.org_id, CrdtDoc.doc_type, CrdtDoc.doc_id)
        .where(
            CrdtDoc.doc_type.in_(sourced),
            CrdtDoc.source_etag.is_not(None),
            CrdtDoc.quarantined_at.is_(None),
            CrdtDoc.projection.has_key("sha256"),
            CrdtDoc.projection["sha256"].astext != CrdtDoc.source_sha256,
            or_(CrdtDoc.save_retry_at.is_(None), CrdtDoc.save_retry_at <= now),
        )
        .order_by(CrdtDoc.save_retry_at.asc().nulls_first(), CrdtDoc.updated_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    if orgs is not None:
        query = query.where(CrdtDoc.org_id.in_(orgs))
    async with factory() as db:
        rows = (await db.execute(query)).all()
        if rows:
            keys = [(row.org_id, row.doc_type, row.doc_id) for row in rows]
            await db.execute(
                update(CrdtDoc)
                .where(tuple_(CrdtDoc.org_id, CrdtDoc.doc_type, CrdtDoc.doc_id).in_(keys))
                .values(
                    save_retry_at=now + timedelta(seconds=CLAIM_SECONDS),
                    # The claim is not an edit: the row keeps its place.
                    updated_at=CrdtDoc.updated_at,
                )
                .execution_options(synchronize_session=False)
            )
        await db.commit()
    if rows:
        log.info("crdt.sessions.unsaved_swept", docs=len(rows))
    return [DocRef(row.org_id, row.doc_type, row.doc_id) for row in rows]


async def park(
    factory: Callable[[], AsyncSession], ref: DocRef, *, reason: str, now: datetime
) -> bool:
    """Record that ``ref``'s write back was refused for ``reason`` and when
    the sweep may try it again. Answers whether the reason is new (a session
    refused for the same reason again says nothing new to its readers)."""
    async with factory() as db:
        doc = (
            await db.execute(
                select(CrdtDoc)
                .where(
                    CrdtDoc.org_id == ref.org_id,
                    CrdtDoc.doc_type == ref.doc_type,
                    CrdtDoc.doc_id == ref.doc_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if doc is None:
            return False
        fresh = doc.save_paused_reason != reason
        failures = 1 if fresh else doc.save_failures + 1
        await db.execute(
            update(CrdtDoc)
            .where(
                CrdtDoc.org_id == ref.org_id,
                CrdtDoc.doc_type == ref.doc_type,
                CrdtDoc.doc_id == ref.doc_id,
            )
            .values(
                save_paused_reason=reason[:64],
                save_failures=failures,
                save_retry_at=now + timedelta(seconds=park_wait(failures)),
                updated_at=CrdtDoc.updated_at,
            )
            .execution_options(synchronize_session=False)
        )
        await db.commit()
    return fresh


@dataclass(frozen=True, slots=True)
class Backlog:
    """The sessions holding edits not on their source for longer than
    :data:`BACKLOG_AGE_SECONDS`: how many (up to :data:`BACKLOG_COUNT_CAP`),
    how many of those are parked (a write back was refused), and how long
    ago the oldest was last edited."""

    count: int
    parked: int
    oldest_seconds: float


async def count_unsaved(
    db: AsyncSession,
    *,
    now: datetime,
    older_than: float = 0.0,
    orgs: frozenset[uuid.UUID] | None = None,
) -> int:
    """How many sessions hold edits not on their source whose last edit is
    at least ``older_than`` seconds before ``now``, in ``orgs`` (every org
    when None); at most :data:`BACKLOG_COUNT_CAP`."""
    return (await _backlog(db, now=now, older_than=older_than, orgs=orgs)).count


async def _backlog(
    db: AsyncSession, *, now: datetime, older_than: float, orgs: frozenset[uuid.UUID] | None
) -> Backlog:
    rows = (
        select(CrdtDoc.updated_at, CrdtDoc.save_paused_reason)
        .where(
            text(CRDT_UNSAVED_PREDICATE),
            CrdtDoc.updated_at <= now - timedelta(seconds=older_than),
        )
        .limit(BACKLOG_COUNT_CAP)
    )
    if orgs is not None:
        rows = rows.where(CrdtDoc.org_id.in_(orgs))
    found = rows.subquery()
    row = (
        await db.execute(
            select(
                func.count().label("count"),
                func.count(found.c.save_paused_reason).label("parked"),
                func.min(found.c.updated_at).label("oldest"),
            )
        )
    ).one()
    oldest = 0.0 if row.oldest is None else max((now - row.oldest).total_seconds(), 0.0)
    return Backlog(count=int(row.count), parked=int(row.parked), oldest_seconds=oldest)


async def report_backlog(
    factory: Callable[[], AsyncSession], *, now: datetime, orgs: frozenset[uuid.UUID] | None
) -> Backlog:
    """Count the backlog (:class:`Backlog`) and, when there is one, say so as
    ``crdt.sessions.unsaved_backlog``: the figure the backlog alarm reads.
    Quiet while there is none, so a healthy deployment logs nothing here."""
    async with factory() as db:
        found = await _backlog(db, now=now, older_than=BACKLOG_AGE_SECONDS, orgs=orgs)
        await db.commit()
    if found.count:
        log.warning(
            "crdt.sessions.unsaved_backlog",
            count=found.count,
            parked=found.parked,
            oldest_seconds=round(found.oldest_seconds),
        )
    return found


def start(
    sweep: Callable[[], Awaitable[int]],
    sleep: Callable[[float], Awaitable[None]],
    *,
    every: float,
) -> asyncio.Task[None]:
    """Run ``sweep`` now and every ``every`` seconds, until cancelled; a
    sweep that fails is the next one's to retry."""

    async def forever() -> None:
        while True:
            try:
                await sweep()
            except Exception as exc:
                log.warning("crdt.sessions.sweep_failed", error=safe_error(exc))
            await sleep(every)

    return asyncio.create_task(forever(), name="crdt-unsaved-sweeper")


def sourced_types(registry: CrdtRegistry | None = None) -> list[str]:
    """The stored ``doc_type`` of every registered type whose truth at rest is
    a file on the drive (a co-edited file, a notebook)."""
    registry = registry if registry is not None else CrdtRegistry()
    return sorted(
        stored_doc_type(name)
        for name in registry.names()
        if (found := registry.get(name)) is not None and found.source is not None
    )


async def unsaved_under(
    db: AsyncSession,
    *,
    org_id: uuid.UUID,
    node_id: uuid.UUID,
    limit: int = 50,
    doc_types: Sequence[str] | None = None,
) -> list[tuple[str, str, int]]:
    """The sessions at or under ``node_id`` (a file, or a folder's whole
    subtree) holding edits not yet on the drive, as ``(doc type, doc id,
    source etag)``, of the types ``doc_types`` (every sourced type when
    ``None``)."""
    rows = (
        await db.execute(
            text(
                "SELECT d.doc_type, d.doc_id, d.source_etag FROM crdt_docs d "
                "JOIN file_nodes n ON n.org_team_id = d.org_id AND CAST(n.id AS text) = d.doc_id "
                "WHERE d.org_id = :org AND d.doc_type = ANY(CAST(:types AS text[])) "
                "AND d.source_etag IS NOT NULL "
                "AND d.quarantined_at IS NULL AND d.projection ? 'sha256' "
                "AND (d.projection ->> 'sha256') <> d.source_sha256 "
                "AND n.path_ids <@ (SELECT path_ids FROM file_nodes "
                "                   WHERE id = :node AND org_team_id = :org) "
                "LIMIT :limit"
            ),
            {
                "org": org_id,
                "node": node_id,
                "limit": limit,
                "types": list(doc_types) if doc_types is not None else sourced_types(),
            },
        )
    ).all()
    return [(str(row.doc_type), str(row.doc_id), int(row.source_etag)) for row in rows]


async def expire_dormant(
    db: AsyncSession,
    *,
    now: datetime,
    limit: int = EXPIRE_LIMIT,
    orgs: frozenset[uuid.UUID] | None = None,
    doc_types: Sequence[str] | None = None,
) -> int:
    """Delete the sessions (of ``doc_types``: every sourced type when
    ``None``) nobody needs any more, at most ``limit``: those
    untouched for :data:`DORMANT_DAYS` with nothing unsaved, and those closed
    for :data:`CLOSED_DAYS`, provided no socket holds a peer on them now, in
    ``orgs`` (every org when None). Their updates go with them (cascade), and
    their peers. Answers how many."""
    rows = (
        await db.execute(
            text(
                "SELECT org_id, doc_type, doc_id FROM crdt_docs d "
                "WHERE d.doc_type = ANY(CAST(:types AS text[])) AND ("
                "  (d.source_etag IS NULL AND d.updated_at < :closed)"
                "  OR (d.source_etag IS NOT NULL AND d.updated_at < :dormant"
                "      AND NOT (d.projection ? 'sha256'"
                "               AND (d.projection ->> 'sha256') <> d.source_sha256))"
                ") AND (CAST(:orgs AS uuid[]) IS NULL OR d.org_id = ANY(CAST(:orgs AS uuid[]))) "
                "AND NOT EXISTS (SELECT 1 FROM crdt_peers p WHERE p.org_id = d.org_id "
                "  AND p.doc_type = d.doc_type AND p.doc_id = d.doc_id AND p.held_until > :now) "
                "ORDER BY d.updated_at LIMIT :limit FOR UPDATE SKIP LOCKED"
            ),
            {
                "closed": now - timedelta(days=CLOSED_DAYS),
                "dormant": now - timedelta(days=DORMANT_DAYS),
                "now": now,
                "limit": limit,
                "orgs": None if orgs is None else list(orgs),
                "types": list(doc_types) if doc_types is not None else sourced_types(),
            },
        )
    ).all()
    for row in rows:
        keys = {"org": row.org_id, "type": row.doc_type, "doc": row.doc_id}
        await db.execute(
            text(
                "DELETE FROM crdt_peers WHERE org_id = :org AND doc_type = :type AND doc_id = :doc"
            ),
            keys,
        )
        await db.execute(
            text(
                "DELETE FROM crdt_docs WHERE org_id = :org AND doc_type = :type AND doc_id = :doc"
            ),
            keys,
        )
    return len(rows)


async def expire(
    factory: Callable[[], AsyncSession], *, now: datetime, orgs: frozenset[uuid.UUID] | None
) -> int:
    """:func:`expire_dormant` in a session of its own, committed and logged."""
    async with factory() as db:
        expired = await expire_dormant(db, now=now, orgs=orgs)
        await db.commit()
    if expired:
        log.info("crdt.sessions.expired", docs=expired)
    return expired


__all__ = [
    "BACKLOG_AGE_SECONDS",
    "BACKLOG_COUNT_CAP",
    "CLAIM_SECONDS",
    "CLOSED_DAYS",
    "DORMANT_DAYS",
    "PARK_FIRST_SECONDS",
    "PARK_MAX_SECONDS",
    "UNSAVED_SWEEP_LIMIT",
    "UNSAVED_SWEEP_SECONDS",
    "Backlog",
    "count_unsaved",
    "expire",
    "expire_dormant",
    "park",
    "park_wait",
    "report_backlog",
    "sourced_types",
    "start",
    "unsaved",
    "unsaved_under",
]
