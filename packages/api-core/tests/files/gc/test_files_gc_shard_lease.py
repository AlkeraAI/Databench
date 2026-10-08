"""Shard mutual exclusion is a lease row, and it holds without connection affinity.

Every statement here runs on a *fresh connection* (`per_statement_connection`),
which is what a PgBouncer transaction pool does. A `pg_advisory_lock` would
evaporate between two such statements and let both sweepers believe they held
the shard; the compare-and-swap `UPDATE` does not.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import pytest
from _files_gc_factory import AdminOnlyFactory
from alkera_core.files.clock import FakeClock
from alkera_core.files.gc import Janitor
from alkera_core.files.ids import OrgScope
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio


@dataclass(frozen=True, slots=True)
class PooledRepo:
    """Just enough repo for the lease: a session and a scope, no affinity.

    The lease statement is deliberately the only thing that runs through it, so
    the test cannot accidentally prove mutual exclusion by holding a
    transaction open across statements.
    """

    session: Any
    scope: OrgScope


def sweeper(session: Any, clock: FakeClock, name: str) -> Janitor:
    return Janitor(
        lambda _scope: None,  # type: ignore[arg-type,return-value]
        AdminOnlyFactory(None),
        clock,
        holder=name,
    )


@pytest.fixture
async def free_shard(files_session: AsyncSession) -> int:
    number = uuid.uuid4().int % 1_000_000 + 2_000_000
    await files_session.execute(
        text("INSERT INTO file_sweep_shards (shard, cursor) VALUES (:s, '{}'::jsonb)"),
        {"s": number},
    )
    await files_session.commit()
    return number


async def test_two_sweepers_racing_for_a_shard_leave_exactly_one_holder(
    sessions: Any,
    files_org: Any,
    clock: FakeClock,
    free_shard: int,
    files_session: AsyncSession,
) -> None:
    """One claim wins, the other is told to skip — no sleeps, no advisory lock."""
    left, right = await sessions(2, per_statement_connection=True)
    first = sweeper(left, clock, "sweeper-a")
    second = sweeper(right, clock, "sweeper-b")

    won = await first.claim_shard(PooledRepo(left, files_org.scope), free_shard)  # type: ignore[arg-type]
    lost = await second.claim_shard(PooledRepo(right, files_org.scope), free_shard)  # type: ignore[arg-type]

    assert won is True
    assert lost is False, "two sweepers held one shard at the same instant"

    holder = (
        await files_session.execute(
            text("SELECT holder FROM file_sweep_shards WHERE shard = :s"), {"s": free_shard}
        )
    ).scalar_one()
    assert holder == "sweeper-a"


async def test_an_expired_lease_hands_the_shard_to_the_waiting_sweeper(
    sessions: Any,
    files_org: Any,
    clock: FakeClock,
    free_shard: int,
    files_session: AsyncSession,
) -> None:
    """The negative twin: expiry is what makes a dead holder recoverable.

    The deadline is Postgres's own ``now()``, so the test expires the lease by
    writing the row's ``expires_at`` into the past rather than by moving a
    client clock the database never reads.
    """
    left, right = await sessions(2, per_statement_connection=True)
    first = sweeper(left, clock, "sweeper-a")
    second = sweeper(right, clock, "sweeper-b")

    assert await first.claim_shard(PooledRepo(left, files_org.scope), free_shard) is True  # type: ignore[arg-type]
    assert await second.claim_shard(PooledRepo(right, files_org.scope), free_shard) is False  # type: ignore[arg-type]

    await files_session.execute(
        text(
            "UPDATE file_sweep_shards SET expires_at = now() - interval '1 second' WHERE shard = :s"
        ),
        {"s": free_shard},
    )
    await files_session.commit()

    assert await second.claim_shard(PooledRepo(right, files_org.scope), free_shard) is True  # type: ignore[arg-type]
    holder = (
        await files_session.execute(
            text("SELECT holder FROM file_sweep_shards WHERE shard = :s"), {"s": free_shard}
        )
    ).scalar_one()
    assert holder == "sweeper-b"


async def test_release_only_gives_back_a_shard_this_sweeper_still_holds(
    sessions: Any,
    files_org: Any,
    clock: FakeClock,
    free_shard: int,
    files_session: AsyncSession,
) -> None:
    """A revived sweeper cannot release the lease that superseded its own."""
    (left,) = await sessions(1, per_statement_connection=True)
    stale = sweeper(left, clock, "sweeper-a")
    live = sweeper(left, clock, "sweeper-b")

    assert await live.claim_shard(PooledRepo(left, files_org.scope), free_shard) is True  # type: ignore[arg-type]
    await stale.release_shard(PooledRepo(left, files_org.scope), free_shard)  # type: ignore[arg-type]

    holder = (
        await files_session.execute(
            text("SELECT holder FROM file_sweep_shards WHERE shard = :s"), {"s": free_shard}
        )
    ).scalar_one()
    assert holder == "sweeper-b", "a stale sweeper released a lease it no longer held"
