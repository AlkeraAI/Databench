"""The erasure ledger: which identities were erased, kept where a restore cannot reach.

Restoring a database backup taken before an erasure brings the person back:
their row, their chats, their address. The ledger is what remembers that they
must not come back. It lives in the object store under
``account-erasure-ledger/``, never in Postgres, so a database restore does not
roll it back; the prefix is meant to sit under the bucket's object lock and
outside whatever restores the Files content.

One object per erased identity, ``account-erasure-ledger/<user id hex>.json``,
written once with ``if_absent`` (append-only by construction) and holding no
personal data: the user id, when it was erased, and the request that did it.
A user id names nobody once the row behind it is a tombstone.

:func:`reerase` is what the restore runbook runs after any restore, and what
the worker runs on boot when it finds a ledgered id whose row is no longer a
tombstone (``python -m worker account reerase`` by hand).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from blake3 import blake3
from sqlalchemy import select

from alkera_core.db.session import AsyncSessionLocal
from alkera_core.files.store.errors import PreconditionFailed
from alkera_core.files.store.protocol import ObjectStore
from alkera_core.logging import get_logger
from alkera_core.models import User

log = get_logger(__name__)

PREFIX = "account-erasure-ledger"
SCHEMA = 1

_store: ObjectStore | None = None


def set_ledger_store(store: ObjectStore | None) -> None:
    """Install (or, with ``None``, drop) the store the ledger lives in."""
    global _store
    _store = store


def ledger_store() -> ObjectStore:
    """The ledger's store: one a deployment or a test installed, else the export
    archive store's (the same bucket, a different prefix)."""
    if _store is not None:
        return _store
    from alkera_core.account.archive_store import archive_store

    return archive_store()


def ledger_key(user_id: uuid.UUID) -> str:
    return f"{PREFIX}/{user_id.hex}.json"


async def _once(data: bytes) -> AsyncIterator[bytes]:
    yield data


async def record(
    store: ObjectStore, *, user_id: uuid.UUID, request_id: uuid.UUID, erased_at: datetime
) -> bool:
    """Write the identity's ledger entry. Returns whether this call wrote it; an
    entry already there (a retried erasure) is left exactly as it was."""
    body = json.dumps(
        {
            "schema": SCHEMA,
            "user_id": str(user_id),
            "request_id": str(request_id),
            "erased_at": erased_at.astimezone(UTC).isoformat(),
        },
        sort_keys=True,
    ).encode()
    try:
        await store.put(
            ledger_key(user_id),
            _once(body),
            size=len(body),
            checksum=blake3(body).digest(),
            if_absent=True,
        )
    except PreconditionFailed:
        return False
    return True


async def erased_ids(store: ObjectStore) -> list[uuid.UUID]:
    """Every identity the ledger says was erased."""
    found: list[uuid.UUID] = []
    after: str | None = None
    while True:
        page = await store.list_prefix(f"{PREFIX}/", after=after)
        for key in page.keys:
            stem = Path(key).stem
            try:
                found.append(uuid.UUID(hex=stem))
            except ValueError:
                log.warning("account.ledger.unreadable_key", key=key)
        if page.next_after is None:
            return found
        after = page.next_after


def _chunks(ids: list[uuid.UUID], size: int = 500) -> Iterable[list[uuid.UUID]]:
    for start in range(0, len(ids), size):
        yield ids[start : start + size]


async def resurrected(ids: list[uuid.UUID]) -> list[uuid.UUID]:
    """The ledgered identities whose row exists and is not a tombstone: what a
    restore of an older backup brought back."""
    back: list[uuid.UUID] = []
    async with AsyncSessionLocal() as db:
        for chunk in _chunks(ids):
            rows = await db.execute(
                select(User.id).where(User.id.in_(chunk), User.deleted_at.is_(None))
            )
            back.extend(rows.scalars().all())
    return back


@dataclass
class ReerasureReport:
    ledgered: int = 0
    resurrected: int = 0
    erased: list[uuid.UUID] = field(default_factory=list)
    failed: list[uuid.UUID] = field(default_factory=list)


async def reerase(store: ObjectStore | None = None) -> ReerasureReport:
    """Erase again every ledgered identity a restore brought back.

    The erasure that ledgered them was already decided and done, so nothing
    blocks the second one (see ``erase(..., reerasure=True)``); the person is
    not emailed again. Idempotent: an identity already a tombstone is skipped."""
    from alkera_core.account.lifecycle import reerase_one

    target = store or ledger_store()
    report = ReerasureReport()
    ids = await erased_ids(target)
    report.ledgered = len(ids)
    back = await resurrected(ids)
    report.resurrected = len(back)
    for user_id in back:
        try:
            await reerase_one(user_id, store=target)
            report.erased.append(user_id)
        except Exception:
            log.error("account.reerasure.failed", user_id=str(user_id), exc_info=True)
            report.failed.append(user_id)
    if back:
        log.warning(
            "account.reerasure",
            ledgered=report.ledgered,
            resurrected=report.resurrected,
            erased=len(report.erased),
            failed=len(report.failed),
        )
    return report


__all__ = [
    "PREFIX",
    "ReerasureReport",
    "erased_ids",
    "ledger_key",
    "ledger_store",
    "record",
    "reerase",
    "resurrected",
    "set_ledger_store",
]
