"""The account lifecycle's background work, each step in its own sessions.

* :func:`process_deletion` runs one due deletion: the wind-down, then the
  erasure, then (after it commits) the archive cleanup and the completion notice.
* :func:`sweep` is the scheduled pass that runs every due deletion, so a lost
  nudge or a crashed worker only ever delays the work.

The worker's activities call these; the stores and the notice sender are
parameters, so a test drives them against a filesystem store with no Files
deployment at all, and this module never imports the email stack (the worker
passes :func:`alkera_core.email.account.send_account_notice`).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select, update

from alkera_core.account import archive_store as archives
from alkera_core.account import ledger
from alkera_core.account.erasure import ErasureBlocked, erase, wind_down
from alkera_core.account.notices import AccountNoticeSender, DeletionBlocked, DeletionCompleted
from alkera_core.account.requests import due_deletions
from alkera_core.db.locking import io_outside_locks
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.files.store.protocol import ObjectStore
from alkera_core.logging import get_logger
from alkera_core.models import AccountDeletionRequest, User

log = get_logger(__name__)


# --------------------------------------------------------------------------- #
# Deletions
# --------------------------------------------------------------------------- #


@dataclass
class DeletionOutcome:
    status: str
    blocked_reason: str | None = None


async def _record_block(
    request_id: uuid.UUID, code: str, now: datetime, *, notify: AccountNoticeSender
) -> None:
    """Write why a due erasure waits, and tell the person once per reason."""
    person: tuple[str, str] | None = None
    async with AsyncSessionLocal() as db:
        request = await db.get(AccountDeletionRequest, request_id, with_for_update=True)
        if request is None or request.status != "scheduled":
            return
        first_time = request.blocked_reason != code or request.blocked_notified_at is None
        request.blocked_reason = code
        request.blocked_at = request.blocked_at or now
        if first_time:
            request.blocked_notified_at = now
            user = await db.get(User, request.user_id)
            if user is not None:
                person = (user.email, user.display_name)
        await db.commit()
    if person is not None:
        await notify(DeletionBlocked(to=person[0], name=person[1], code=code))


async def process_deletion(
    request_id: uuid.UUID,
    *,
    store: ObjectStore | None = None,
    ledger_store: ObjectStore | None = None,
    notify: AccountNoticeSender,
    now: datetime | None = None,
) -> DeletionOutcome:
    """Wind down, then erase, one due deletion. The ledger entry is written
    before the erasure commits, so every committed erasure is ledgered (an
    entry for an erasure that then failed to commit is harmless: the request is
    still due and the next pass erases it)."""
    at = now or datetime.now(UTC)
    async with AsyncSessionLocal() as db:
        request = await db.get(AccountDeletionRequest, request_id)
        if request is None or request.status != "scheduled" or request.purge_after > at:
            return DeletionOutcome(status="skipped")
        user_id = request.user_id
    async with AsyncSessionLocal() as db:
        await wind_down(db, user_id)
        await db.commit()
    async with AsyncSessionLocal() as db:
        request = await db.get(AccountDeletionRequest, request_id, with_for_update=True)
        if request is None or request.status != "scheduled":
            return DeletionOutcome(status="skipped")
        try:
            result = await erase(db, request, now=at)
        except ErasureBlocked as blocked:
            await db.rollback()
            await _record_block(request_id, blocked.code, at, notify=notify)
            return DeletionOutcome(status="blocked", blocked_reason=blocked.code)
        await _ledger_erasure(ledger_store, user_id=user_id, request_id=request_id, at=at)
        await db.commit()
    await _drop_archives(store, result.archive_keys)
    await notify(DeletionCompleted(to=result.email, name=result.first_name))
    return DeletionOutcome(status="completed")


async def _ledger_erasure(
    store: ObjectStore | None, *, user_id: uuid.UUID, request_id: uuid.UUID, at: datetime
) -> None:
    """Write the erasure's ledger entry, before the erasure commits, so a
    restored backup can never bring back a committed erasure. The erasure's
    rows stay locked for this one small write: one person's rows, in a
    background pass."""
    with io_outside_locks.allow(reason="an erasure is ledgered before it commits"):
        await ledger.record(
            store or ledger.ledger_store(), user_id=user_id, request_id=request_id, erased_at=at
        )


async def _drop_archives(store: ObjectStore | None, keys: tuple[str, ...]) -> None:
    target_store = store or archives.archive_store()
    for key in keys:
        try:
            await target_store.delete(key)
        except Exception:  # the expiry sweep has no row left to retry it; log loudly
            log.error("account.erasure.archive_delete_failed", key=key, exc_info=True)


async def reerase_one(
    user_id: uuid.UUID,
    *,
    store: ObjectStore | None = None,
    archive: ObjectStore | None = None,
    now: datetime | None = None,
) -> None:
    """Erase again an identity a restore brought back. Runs whatever request the
    restored rows hold (or a new one, sourced ``restore``), ignores blockers,
    and sends no email: the person was told when they were erased."""
    at = now or datetime.now(UTC)
    async with AsyncSessionLocal() as db:
        await wind_down(db, user_id)
        await db.commit()
    async with AsyncSessionLocal() as db:
        request = await db.scalar(
            select(AccountDeletionRequest)
            .where(
                AccountDeletionRequest.user_id == user_id,
                AccountDeletionRequest.status == "scheduled",
            )
            .with_for_update()
        )
        if request is None:
            request = AccountDeletionRequest(
                user_id=user_id,
                source="restore",
                status="scheduled",
                requested_at=at,
                purge_after=at,
                plan={},
            )
            db.add(request)
            await db.flush()
        result = await erase(db, request, now=at, reerasure=True)
        await _ledger_erasure(store, user_id=user_id, request_id=request.id, at=at)
        await db.commit()
    await _drop_archives(archive, result.archive_keys)
    log.warning("account.reerased", user_id=str(user_id))


@dataclass
class SweepReport:
    deletions: dict[str, int] = field(default_factory=dict)


async def sweep(
    *,
    store: ObjectStore | None = None,
    ledger_store: ObjectStore | None = None,
    notify: AccountNoticeSender,
    now: datetime | None = None,
) -> SweepReport:
    """One scheduled pass: every due deletion."""
    at = now or datetime.now(UTC)
    report = SweepReport()
    async with AsyncSessionLocal() as db:
        due = await due_deletions(db, now=at)
    for request_id in due:
        try:
            outcome = await process_deletion(
                request_id, store=store, ledger_store=ledger_store, notify=notify, now=at
            )
        except Exception:
            log.error("account.erasure.failed", request_id=str(request_id), exc_info=True)
            outcome = DeletionOutcome(status="failed")
        report.deletions[outcome.status] = report.deletions.get(outcome.status, 0) + 1
    return report


async def mark_due_now(request_id: uuid.UUID, *, now: datetime | None = None) -> None:
    """Bring a scheduled deletion's grace window to an end (the support tool's
    "erase now" for a request already verified through another channel)."""
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(AccountDeletionRequest)
            .where(
                AccountDeletionRequest.id == request_id,
                AccountDeletionRequest.status == "scheduled",
            )
            .values(purge_after=now or datetime.now(UTC))
        )
        await db.commit()


__all__ = [
    "DeletionOutcome",
    "SweepReport",
    "mark_due_now",
    "process_deletion",
    "reerase_one",
    "sweep",
]
