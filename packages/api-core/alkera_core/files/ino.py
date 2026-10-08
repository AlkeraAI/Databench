"""Inode numbers: one block-allocating UPDATE, never a read-then-write, and
never in the caller's transaction.

A mount façade addresses a node by ``(drive, ino)``, so an ino must be unique
per drive and must never be reused — a purge that freed 42 and a create that
handed 42 back would let a cached client open the wrong file. Both properties
come from one place: ``file_drives.next_ino`` only ever moves forward, and it
moves in a single ``UPDATE … RETURNING`` so two backends racing for a block get
two different blocks instead of the same number twice.

The block is 1,000 wide and reserved in a transaction of its own, committed
before the create that needs it goes on (:func:`reserve_apart`), and the
process keeps it for every later create in the drive. Reserving inside the
caller's transaction held the drive row from that UPDATE to the caller's
commit — to the end of the request — so every create in the org queued on the
org's one row, and the block lived only as long as the request, so every
request reserved again. Wasting the tail of a block is free: the space is
2^63 wide and nothing reads it as a count, and a block whose create rolls back
is wasted, never handed out twice.

The one exception is a transaction that already holds the drive row (building
a drive's skeleton, the quota decision): a reservation apart would wait on
that very lock, so it reserves in the transaction instead, and that block is
kept by this allocator alone.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Final

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession

from alkera_core.db.errors import LOCK_NOT_AVAILABLE, sqlstate_of
from alkera_core.db.locking import LockRank, held_ranks, wait_at_most
from alkera_core.db.session import has_spare_connection
from alkera_core.files.ids import DriveId
from alkera_core.files.repo import FilesRepo

#: How many inos one UPDATE reserves.
DEFAULT_BLOCK: Final = 1_000


@dataclass(slots=True)
class InoBlock:
    """A reserved half-open range ``[next, end)`` of inos for one drive."""

    next: int
    end: int

    @property
    def exhausted(self) -> bool:
        return self.next >= self.end


#: Blocks reserved apart and committed, kept by the process for every
#: allocator that does not bring its own. Safe to share: a committed block is
#: this process's alone, and a block left over when the process ends is wasted.
_COMMITTED_BLOCKS: dict[uuid.UUID, InoBlock] = {}

#: How long a reservation apart waits for a drive row somebody else holds
#: (the quota decision, a moment) before reserving in the caller's own
#: transaction instead, as it did before blocks were kept.
APART_LOCK_TIMEOUT_MS: Final = 1_000


class InoAllocator:
    """Hands out inos for the drives one backend touches.

    ``cache`` holds the blocks reserved apart; it defaults to the process's
    own, and a caller that reserved its own blocks ahead (a tree report)
    passes those. A block reserved inside the caller's transaction is kept
    apart from it, by this allocator alone: a rollback returns ``next_ino`` to
    where it was, so that block must not outlive the transaction.
    """

    def __init__(
        self,
        repo: FilesRepo,
        *,
        block: int = DEFAULT_BLOCK,
        cache: dict[uuid.UUID, InoBlock] | None = None,
    ) -> None:
        if block < 1:
            raise ValueError(f"block must be >= 1, got {block}")
        self._repo = repo
        self._block = block
        self._cache: dict[uuid.UUID, InoBlock] = _COMMITTED_BLOCKS if cache is None else cache
        self._in_transaction: dict[uuid.UUID, InoBlock] = {}

    @property
    def block_size(self) -> int:
        return self._block

    async def allocate(self, drive_id: DriveId) -> int:
        """The next ino for ``drive_id``, reserving a block when one runs out."""
        for blocks in (self._cache, self._in_transaction):
            held = blocks.get(drive_id)
            if held is not None and not held.exhausted:
                ino = held.next
                held.next += 1
                return ino
        if LockRank.FILES_DRIVE in held_ranks() or not self._apart_has_a_connection():
            held = await self._reserve(drive_id)
            self._in_transaction[drive_id] = held
        else:
            try:
                held = await reserve_apart(
                    self._repo, drive_id, block=self._block, wait_ms=APART_LOCK_TIMEOUT_MS
                )
            except (LookupError, DBAPIError) as exc:
                # The drive row is this transaction's own (created or locked in
                # it), or somebody holds it longer than a moment: reserve here
                # instead.
                if isinstance(exc, DBAPIError) and sqlstate_of(exc) != LOCK_NOT_AVAILABLE:
                    raise
                held = await self._reserve(drive_id)
                self._in_transaction[drive_id] = held
            else:
                self._cache[drive_id] = held
        ino = held.next
        held.next += 1
        return ino

    def _apart_has_a_connection(self) -> bool:
        """Whether a reservation apart would get its own connection at once.
        It runs while this transaction holds one, so with the pool spent it
        would wait for a connection that callers like this one never hand
        back; the block is reserved in this transaction instead."""
        bind = self._repo.session.bind
        engine = bind.engine if isinstance(bind, AsyncConnection) else bind
        return isinstance(engine, AsyncEngine) and has_spare_connection(engine)

    async def _reserve(self, drive_id: DriveId) -> InoBlock:
        """Take the next block with one statement, in the repo's transaction."""
        self._repo._require_open()
        return await _take_block(self._repo, drive_id, block=self._block)


async def _take_block(repo: FilesRepo, drive_id: DriveId, *, block: int) -> InoBlock:
    """``next_ino = next_ino + block`` is evaluated by Postgres against the row
    it has just locked for the update, so a concurrent reserver either waits
    for this transaction and then reads the new value, or has already moved it
    and this statement adds to *that*. Neither can observe the value this one
    read, which is what a Python-side read-then-write cannot promise."""
    row = (
        await repo.session.execute(
            text(
                "UPDATE file_drives SET next_ino = next_ino + :block "
                "WHERE id = :drive_id AND org_team_id = :org "
                "RETURNING next_ino"
            ),
            {"block": block, "drive_id": drive_id, "org": repo.scope.org_team_id},
        )
    ).scalar_one_or_none()
    if row is None:
        raise LookupError(f"no drive {drive_id} in this org")
    end = int(row)
    return InoBlock(next=end - block, end=end)


async def reserve_apart(
    repo: FilesRepo, drive_id: DriveId, *, block: int, wait_ms: int | None = None
) -> InoBlock:
    """Reserve a block of ``block`` inos in a transaction of its own, committed
    before this returns.

    The reserving UPDATE holds the drive row until its transaction ends, so a
    reservation made inside a long batch keeps every other writer in the org
    queued on that row for the whole batch. Committed apart, the row is held
    for one statement. Uniqueness does not depend on the caller's transaction:
    ``next_ino`` only moves forward, so a block whose batch later rolls back
    is wasted, never handed out twice.

    The caller must not already hold the drive row: the reservation runs on
    another connection and would wait on it. ``wait_ms`` bounds that wait (a
    ``55P03`` after it); without it the connection's own limit applies.
    Raises :class:`LookupError` for a drive the committed database does not
    have yet.
    """
    bind = repo.session.bind
    engine = bind.engine if isinstance(bind, AsyncConnection) else bind
    async with AsyncSession(bind=engine, expire_on_commit=False) as side:
        side_repo = FilesRepo(side, repo.scope)
        async with side_repo.transaction():
            if wait_ms is not None:
                await wait_at_most(side, wait_ms)
            return await _take_block(side_repo, drive_id, block=block)


__all__ = ["DEFAULT_BLOCK", "InoAllocator", "InoBlock", "reserve_apart"]
