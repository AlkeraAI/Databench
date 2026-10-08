"""Generic drain for webhook-inbox tables (Stripe events, GitHub deliveries).

Each inbox persists one row per received event with the same drain-state
columns (``received_at`` / ``processed_at`` / ``attempts`` / ``last_error``)
and drains them the same way: fewest-attempts first then oldest-first, each
row applied in its own transaction after locking it, deterministic failures
burning toward a dead-letter cap while transient blips retry for free
(escalating only once a row has been stuck far past blip territory). The
apply callback may return a value; ``post_commit`` receives it AFTER the
row's transaction commits, for effects that must never roll the applied row
back (the Stripe drain dispatches its billing emails there).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, NamedTuple, TypeVar, cast

from alkera_core.db.session import AsyncSessionLocal
from alkera_core.logging import get_logger
from alkera_core.observability.sentry import capture_message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from worker.tasks._hardening import is_transient_error

log = get_logger(__name__)

T = TypeVar("T")


class DrainCounts(NamedTuple):
    """What one drain page did, on two sides: the rows whose apply committed,
    and the rows whose apply raised and were recorded as failed (an attempt
    burned, or a transient blip noted) — those stay in the inbox for a later
    pass. A caller deciding whether to go again reads ``applied``; a page of
    failures is not progress."""

    applied: int
    failed: int

    @property
    def processed(self) -> int:
        """Every row the page took a decision on."""
        return self.applied + self.failed


@dataclass(frozen=True, slots=True)
class Inbox:
    """One drainable webhook-inbox table and how to name it in logs and alerts."""

    model: type[Any]
    """ORM class carrying the drain columns (received_at/processed_at/attempts/last_error)."""
    pk: InstrumentedAttribute[str]
    """The primary-key column attribute (the event's dedupe identity)."""
    kind: str
    """Human name for alert copy, e.g. ``"stripe event"``."""
    log_prefix: str
    """structlog event prefix, e.g. ``"billing.stripe_event"``."""
    describe: Callable[[Any], str]
    """Row -> short human identity for alert copy, e.g. ``invoice.paid (evt_123)``."""
    # A poison row is retried until it crosses this cap, then dead-lettered (left
    # unprocessed with `last_error` set, but no longer re-picked) so it can't
    # burn an apply every drain forever or starve fresh rows out of the window.
    max_attempts: int = 12
    # A transient failure (provider outage / DB blip) doesn't burn an attempt,
    # but past this age a still-failing row is a stuck dependency, not a blip,
    # and escalates onto the poison/attempts path so it eventually dead-letters.
    transient_max_age: timedelta = timedelta(hours=24)


async def drain_inbox(
    inbox: Inbox,
    apply: Callable[[AsyncSession, Any], Awaitable[T]],
    *,
    post_commit: Callable[[T], Awaitable[None]] | None = None,
    now: datetime | None = None,
    limit: int = 100,
    only: Collection[str] | None = None,
) -> DrainCounts:
    """Apply every unprocessed row, each in its own transaction. Fewest-attempts
    first (so a poison row can't starve fresh ones), then oldest-first. Returns
    how many rows were applied and how many were marked failed, apart.

    ``only`` narrows the drain to those primary keys: a caller that recorded
    specific rows applies exactly those, and leaves every other pending row to
    the drain that owns it."""
    now = now or datetime.now(UTC)
    model = inbox.model
    pending = [model.processed_at.is_(None), model.attempts < inbox.max_attempts]
    if only is not None:
        pending.append(inbox.pk.in_(list(only)))
    async with AsyncSessionLocal() as s:
        ids = list(
            (
                await s.execute(
                    select(inbox.pk)
                    .where(*pending)
                    # Never-tried rows FIRST: transient failures don't burn
                    # `attempts`, so ordering by attempts alone let >=limit old
                    # transiently-failing rows monopolize every drain for up to
                    # transient_max_age, freezing all newer money events.
                    .order_by(
                        model.last_error.is_not(None).asc(),
                        model.attempts.asc(),
                        model.received_at.asc(),
                    )
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
    applied_rows = 0
    failed_rows = 0
    for row_id in ids:
        result: T | None = None
        applied = False
        async with AsyncSessionLocal() as s:
            row = (
                await s.execute(select(model).where(inbox.pk == row_id).with_for_update())
            ).scalar_one_or_none()
            if row is None or row.processed_at is not None:
                continue  # raced with another drain: already handled
            try:
                result = await apply(s, row)
                row.processed_at = datetime.now(UTC)
                row.last_error = None
                await s.commit()
                applied = True
                applied_rows += 1
            except Exception as exc:
                await s.rollback()
                await _record_failure(
                    inbox, row_id, str(exc), transient=is_transient_error(exc), now=now
                )
                failed_rows += 1
        # Post-commit effects (best-effort, idempotent) run AFTER the row's
        # transaction committed so they can never roll the applied row back.
        if applied and post_commit is not None:
            await post_commit(cast("T", result))
    return DrainCounts(applied=applied_rows, failed=failed_rows)


async def _record_failure(
    inbox: Inbox, row_id: str, error: str, *, transient: bool, now: datetime
) -> None:
    """Record a failed apply. A TRANSIENT failure (a provider outage / DB blip)
    must NOT burn the poison attempts cap: a multi-minute incident would
    otherwise dead-letter every in-flight row even though the very next attempt
    would succeed. So a transient failure leaves ``attempts`` untouched (the row
    stays in the drain set and retries on the next drain); only a deterministic
    failure, or a transient one that has been failing for far too long to be a
    blip, counts toward the cap + eventual dead-letter."""
    async with AsyncSessionLocal() as s:
        row = await s.get(inbox.model, row_id)
        if row is None:
            return
        row.last_error = error[:1000]
        stuck_too_long = (now - row.received_at) > inbox.transient_max_age
        if transient and not stuck_too_long:
            # `detail`, not `event`: structlog reserves the `event` name for the
            # log line itself (its first positional), so that kwarg would collide.
            log.warning(
                inbox.log_prefix + ".transient_failure",
                event_id=row_id,
                detail=inbox.describe(row),
                error=error[:200],
            )
            await s.commit()
            return
        row.attempts += 1
        log.error(inbox.log_prefix + ".apply_failed", event_id=row_id, error=error[:200])
        if row.attempts >= inbox.max_attempts:
            # Dead-lettered: it won't be re-picked, so whatever effect the event
            # carried never happened (for a granting Stripe event that means a
            # customer paid and got nothing; for a GitHub delivery, a PR stuck
            # waiting on its check). Page a human, never let it go silent.
            log.error(
                inbox.log_prefix + ".dead_lettered",
                event_id=row_id,
                detail=inbox.describe(row),
                attempts=row.attempts,
                error=error[:200],
            )
            capture_message(
                f"{inbox.kind} dead-lettered after {row.attempts} attempts: {inbox.describe(row)}",
                level="error",
                event_id=row_id,
            )
        await s.commit()
