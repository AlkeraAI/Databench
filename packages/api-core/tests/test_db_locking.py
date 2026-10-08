"""Advisory locks: every key keeps the value it had before one module owned them.

A deployment rolls one task at a time, so for a while old and new code take
the same locks side by side. Two writers serialize only when they derive the
same number, so a key whose value moved would let an old task and a new one
both pass a check the lock exists to make atomic. The values below were
computed from the derivations that lived at each call site before they moved
here, and are checked where it matters: in ``pg_locks``, as the lock Postgres
actually granted.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import pytest
from alkera_core.config import settings
from alkera_core.db.errors import sqlstate_of
from alkera_core.db.locking import (
    CHECKS_ENV,
    AdvisoryKey,
    IOUnderLockError,
    KeyPart,
    LockOrderError,
    LockRank,
    advisory_claim,
    advisory_key,
    advisory_xact_lock,
    held_ranks,
    io_boundary,
    io_boundary_class,
    io_outside_locks,
    lock_rows,
    lock_text,
    register_rank,
    registered_ranks,
)
from sqlalchemy import column, select, table, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, create_async_engine
from structlog.testing import capture_logs

U1 = UUID("11111111-2222-3333-4444-555555555555")
U2 = UUID("66666666-7777-8888-9999-aaaaaaaaaaaa")
U3 = UUID("bbbbbbbb-cccc-dddd-eeee-ffffffffffff")

#: ``(namespace, parts, the 64-bit key, or (class, key) for the two-integer
#: form)``, as the old derivation produced them.
GOLDEN: list[tuple[str, tuple[KeyPart, ...], int | tuple[int, int]]] = [
    # sha256 over a prefix and the parts
    ("account:export", (U1,), 5341199582527627769),
    ("org-creation", (U1,), -7299783545231589689),
    ("kb-family", (U1, "item-7"), -4168807869175333211),
    ("kb-volume", (U1,), -1045218619854640505),
    ("team-tree", (U1,), 2828274041602289892),
    # sha256 over the org id alone
    ("audit-chain", (U1,), 1749920207203448298),
    # blake2b
    ("ci-token-mint", (U1,), -1466886375078093895),
    ("realtime-doc-creation", (U1, "chat"), -9157757760247281602),
    ("compute-session-slot", (U1, U2, "sess-1", U3), -3760331850887446985),
    # crc32
    ("worker-job", ("expire_grants",), 796346859),
    # Postgres hashtext
    ("billing-member-budget", (U1,), -1863316393),
    ("gateway-covered", (U1,), -1994944995),
    ("billing-cap-slot", ("budget", U1, U2), 1140753705),
    ("org-machine-purchase", (U1, "idem-1"), -1781836834),
    ("chat-spare", (U1, U2), -1342711976),
    ("files-drive-create", (U1,), -173305195),
    ("deployment-health", (), -1395508283),
    # Postgres hashtextextended
    ("files-object", (U1, "objects/aa/bb/aabb"), -8255973097454774080),
    ("notebook-kernels", ("item-7",), -6047918774769591276),
    ("crdt-doc", ("notebook/abc",), 2790611117240702994),
    ("workspace-projects", (U1, U2), -8872043588551210282),
    # the two-integer form: a class, and hashtext of the id
    ("compute-admission", ("grant-lock-1",), (0x4F6D6164, 1005915756)),
    ("compute-grant", (U1,), (0x436D7175, -1206255066)),
    ("compute-register", (U1,), (0x436D7267, -1206255066)),
    ("compute-power", (U1,), (0x50777231, -1206255066)),
    ("chat-attach", (U1,), (0x43417474, -1206255066)),
]


def _lock_tag(expected: int | tuple[int, int]) -> tuple[int, int, int]:
    """``(classid, objid, objsubid)`` as ``pg_locks`` shows the lock."""
    if isinstance(expected, tuple):
        klass, key = expected
        return klass & 0xFFFF_FFFF, key & 0xFFFF_FFFF, 2
    return (expected >> 32) & 0xFFFF_FFFF, expected & 0xFFFF_FFFF, 1


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    made = create_async_engine(settings.database_url, pool_size=2, max_overflow=0)
    try:
        yield made
    finally:
        await made.dispose()


async def _granted_to(conn: AsyncConnection) -> set[tuple[int, int, int]]:
    rows = await conn.execute(
        text(
            "SELECT classid::bigint, objid::bigint, objsubid::int FROM pg_locks "
            "WHERE locktype = 'advisory' AND granted AND pid = pg_backend_pid()"
        )
    )
    return {(int(a), int(b), int(c)) for a, b, c in rows.all()}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("namespace", "parts", "expected"),
    [pytest.param(*row, id=row[0]) for row in GOLDEN],
)
async def test_advisory_keys_unchanged(
    engine: AsyncEngine,
    namespace: str,
    parts: tuple[KeyPart, ...],
    expected: int | tuple[int, int],
) -> None:
    async with engine.connect() as conn:
        await advisory_xact_lock(conn, advisory_key(namespace, *parts))
        assert await _granted_to(conn) == {_lock_tag(expected)}
        await conn.rollback()
        assert await _granted_to(conn) == set()


@pytest.mark.parametrize(
    ("namespace", "parts", "expected"),
    [pytest.param(*row, id=row[0]) for row in GOLDEN if isinstance(row[2], int)],
)
def test_keys_computed_here_need_no_database(
    namespace: str, parts: tuple[KeyPart, ...], expected: int
) -> None:
    key = advisory_key(namespace, *parts)
    if key.form != "bigint":
        pytest.skip("Postgres computes this one")
    assert key.value == expected


def test_a_new_namespace_takes_the_canonical_derivation() -> None:
    first = advisory_key("notebook-run", U1)
    assert first.form == "bigint"
    assert first == advisory_key("notebook-run", U1)
    assert first != advisory_key("notebook-run", U2)
    assert first != advisory_key("notebook-runs", U1)


@pytest.mark.parametrize(
    ("parts", "error"),
    [
        pytest.param(("a", U1), ValueError, id="string-not-last"),
        pytest.param((True,), TypeError, id="bool"),
        pytest.param((1.5,), TypeError, id="float"),
    ],
)
def test_ambiguous_parts_are_refused(parts: tuple[object, ...], error: type[Exception]) -> None:
    with pytest.raises(error):
        advisory_key("some-namespace", *parts)  # type: ignore[arg-type]


def test_an_empty_namespace_is_refused() -> None:
    with pytest.raises(ValueError, match="namespace"):
        advisory_key("")


@pytest.mark.asyncio
async def test_try_only_refuses_a_held_key_and_shared_holders_admit_each_other(
    engine: AsyncEngine,
) -> None:
    key: AdvisoryKey = advisory_key("locking-test", U3)
    async with engine.connect() as first, engine.connect() as second:
        assert await advisory_xact_lock(first, key, shared=True)
        assert await advisory_xact_lock(second, key, try_only=True, shared=True)
        await second.rollback()
        assert not await advisory_xact_lock(second, key, try_only=True)
        await first.rollback()
        assert await advisory_xact_lock(second, key, try_only=True)
        await second.rollback()


async def _holders(conn: AsyncConnection, key: AdvisoryKey) -> int:
    """How many sessions in this database hold ``key`` right now."""
    assert key.value is not None
    classid, objid, objsubid = _lock_tag(key.value)
    rows = await conn.execute(
        text(
            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND granted "
            "AND classid::bigint = :c AND objid::bigint = :o AND objsubid = :s "
            "AND database = (SELECT oid FROM pg_database WHERE datname = current_database())"
        ),
        {"c": classid, "o": objid, "s": objsubid},
    )
    return int(rows.scalar_one())


@pytest.mark.asyncio
async def test_a_claim_is_held_for_its_block_and_gone_after_it(engine: AsyncEngine) -> None:
    """Nobody else gets the claim while the block runs, and once it ends no
    session in the database still holds it: the connection it rode on goes
    back to the pool clean, which is what a transaction pooler needs."""
    key = advisory_key("locking-test-claim", U1)
    async with advisory_claim(engine, key) as held:
        assert held is True
        async with advisory_claim(engine, key) as second:
            assert second is False
    async with engine.connect() as conn:
        assert await _holders(conn, key) == 0
    async with advisory_claim(engine, key) as again:
        assert again is True


@pytest.mark.asyncio
async def test_a_claim_survives_the_idle_limit(engine: AsyncEngine) -> None:
    """The claim's transaction runs nothing while the job works, so the idle
    limit a background connection carries must not end it."""
    impatient = create_async_engine(
        settings.database_url,
        pool_size=1,
        max_overflow=0,
        connect_args={"server_settings": {"idle_in_transaction_session_timeout": "200"}},
    )
    key = advisory_key("locking-test-idle", U2)
    try:
        async with advisory_claim(impatient, key) as held:
            assert held is True
            await asyncio.sleep(0.6)
            async with engine.connect() as probe:
                assert await _holders(probe, key) == 1
    finally:
        await impatient.dispose()


# --------------------------------------------------------------------------- #
# Row locks: ranks, waits
# --------------------------------------------------------------------------- #


@pytest.fixture
async def probe_table(engine: AsyncEngine) -> AsyncIterator[Any]:
    """A real table of three rows, visible to every connection."""
    name = f"locking_probe_{uuid4().hex[:12]}"
    async with engine.begin() as conn:
        await conn.execute(text(f"CREATE TABLE {name} (id int PRIMARY KEY)"))
        await conn.execute(text(f"INSERT INTO {name} VALUES (1), (2), (3)"))
    try:
        yield table(name, column("id"))
    finally:
        async with engine.begin() as conn:
            await conn.execute(text(f"DROP TABLE {name}"))


@pytest.mark.asyncio
async def test_a_rank_is_held_until_its_transaction_ends(
    engine: AsyncEngine, probe_table: Any
) -> None:
    async with AsyncSession(engine) as session:
        assert held_ranks() == ()
        rows = await lock_rows(
            session, LockRank.ALLOCATION, select(probe_table.c.id).where(probe_table.c.id == 1)
        )
        assert rows.scalars().all() == [1]
        assert held_ranks() == (LockRank.ALLOCATION,)
        await session.commit()
        assert held_ranks() == ()


@pytest.mark.asyncio
async def test_a_lock_that_found_no_row_holds_nothing(
    engine: AsyncEngine, probe_table: Any
) -> None:
    async with AsyncSession(engine) as session:
        await lock_rows(
            session, LockRank.ALLOCATION, select(probe_table.c.id).where(probe_table.c.id == 99)
        )
        assert held_ranks() == ()
        await session.rollback()


@pytest.mark.asyncio
async def test_a_row_lock_excludes_another_writer_and_skip_locked_passes_it_by(
    engine: AsyncEngine, probe_table: Any
) -> None:
    async with AsyncSession(engine) as holder, AsyncSession(engine) as other:
        await lock_rows(
            holder, LockRank.ALLOCATION, select(probe_table.c.id).where(probe_table.c.id == 1)
        )
        free = await lock_rows(
            other,
            LockRank.ALLOCATION,
            select(probe_table.c.id).order_by(probe_table.c.id),
            skip_locked=True,
        )
        assert free.scalars().all() == [2, 3]
        await other.rollback()
        with pytest.raises(DBAPIError) as refused:
            await lock_rows(
                other,
                LockRank.ALLOCATION,
                select(probe_table.c.id).where(probe_table.c.id == 1),
                timeout_ms=150,
            )
        assert sqlstate_of(refused.value) == "55P03"
        await other.rollback()
        await holder.rollback()


@pytest.mark.asyncio
async def test_a_bounded_wait_applies_to_its_statement_alone(
    engine: AsyncEngine, probe_table: Any
) -> None:
    async with AsyncSession(engine) as session:
        before = (await session.execute(text("SHOW lock_timeout"))).scalar_one()
        await lock_rows(
            session,
            LockRank.ALLOCATION,
            select(probe_table.c.id).where(probe_table.c.id == 2),
            timeout_ms=250,
        )
        after = (await session.execute(text("SHOW lock_timeout"))).scalar_one()
        assert after == before
        await session.rollback()


@pytest.mark.asyncio
async def test_a_hot_rank_gives_up_quickly(engine: AsyncEngine, probe_table: Any) -> None:
    hot = register_rank("locking_test_hot", 9_001, hot=True, reason="a test's hot row")
    async with AsyncSession(engine) as holder, AsyncSession(engine) as waiter:
        await lock_rows(holder, hot, select(probe_table.c.id).where(probe_table.c.id == 3))
        started = asyncio.get_running_loop().time()
        with pytest.raises(DBAPIError) as refused:
            await lock_rows(waiter, hot, select(probe_table.c.id).where(probe_table.c.id == 3))
        waited = asyncio.get_running_loop().time() - started
        assert sqlstate_of(refused.value) == "55P03"
        assert waited < 2.0
        await waiter.rollback()
        await holder.rollback()


@pytest.mark.asyncio
async def test_sql_text_gets_its_locking_clause_here(engine: AsyncEngine, probe_table: Any) -> None:
    async with AsyncSession(engine) as holder, AsyncSession(engine) as other:
        await lock_text(
            holder,
            LockRank.FILES_LEASE,
            f"SELECT p.id FROM {probe_table.name} p WHERE p.id = :id",
            {"id": 2},
            strength="share",
            of="p",
        )
        assert held_ranks() == (LockRank.FILES_LEASE,)
        shared = await lock_text(
            other,
            LockRank.FILES_LEASE,
            f"SELECT p.id FROM {probe_table.name} p WHERE p.id = :id",
            {"id": 2},
            strength="share",
            timeout_ms=150,
        )
        assert shared.scalars().all() == [2]
        await other.rollback()
        with pytest.raises(DBAPIError):
            await lock_text(
                other,
                LockRank.FILES_LEASE,
                f"SELECT p.id FROM {probe_table.name} p WHERE p.id = :id",
                {"id": 2},
                strength="no_key_update",
                timeout_ms=150,
            )
        await other.rollback()
        await holder.rollback()


def test_two_ranks_cannot_share_a_name_or_a_place() -> None:
    assert (
        register_rank("allocation", LockRank.ALLOCATION.value, hot=False, reason="again")
        is LockRank.ALLOCATION
    )
    with pytest.raises(ValueError, match="already registered"):
        register_rank("allocation", 201, hot=False, reason="moved")
    with pytest.raises(ValueError, match="already"):
        register_rank("someone_else", LockRank.ALLOCATION.value, hot=False, reason="same place")
    assert [rank.value for rank in registered_ranks()] == sorted(
        rank.value for rank in registered_ranks()
    )


# --------------------------------------------------------------------------- #
# The two rules
# --------------------------------------------------------------------------- #


@pytest.fixture
def enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(CHECKS_ENV, "1")


@pytest.mark.asyncio
@pytest.mark.usefixtures("enforced")
async def test_a_lower_rank_after_a_higher_one_is_refused(
    engine: AsyncEngine, probe_table: Any
) -> None:
    one = select(probe_table.c.id).where(probe_table.c.id == 1)
    two = select(probe_table.c.id).where(probe_table.c.id == 2)
    async with AsyncSession(engine) as session:
        await lock_rows(session, LockRank.ALLOCATION, one)
        with pytest.raises(LockOrderError, match="org_machine"):
            await lock_rows(session, LockRank.ORG_MACHINE, two)
        await session.rollback()


@pytest.mark.asyncio
@pytest.mark.usefixtures("enforced")
@pytest.mark.parametrize(
    ("first_shared", "then_shared", "refused"),
    [
        pytest.param(True, False, True, id="shared-then-exclusive"),
        pytest.param(True, True, False, id="shared-twice"),
        pytest.param(False, False, False, id="exclusive-twice"),
        pytest.param(False, True, False, id="exclusive-then-shared"),
    ],
)
async def test_an_advisory_lock_held_shared_is_never_upgraded(
    engine: AsyncEngine, first_shared: bool, then_shared: bool, refused: bool
) -> None:
    """Two holders of a shared lock that each ask for it exclusive wait for
    each other; the order rule refuses the upgrade in the one that asks."""
    key = advisory_key("lock-upgrade-probe", U1)
    rank = LockRank.FILES_NAMESPACE
    async with AsyncSession(engine) as session:
        await advisory_xact_lock(session, key, shared=first_shared, rank=rank)
        if refused:
            with pytest.raises(LockOrderError, match="exclusive while holding it shared"):
                await advisory_xact_lock(session, key, shared=then_shared, rank=rank)
        else:
            await advisory_xact_lock(session, key, shared=then_shared, rank=rank)
        await session.rollback()
        # A fresh transaction holds nothing shared.
        await advisory_xact_lock(session, key, rank=rank)
        await session.rollback()


@pytest.mark.asyncio
@pytest.mark.usefixtures("enforced")
async def test_a_lock_held_exclusive_may_be_asked_for_shared_then_exclusive_again(
    engine: AsyncEngine,
) -> None:
    """A transaction that took the lock exclusive first (a batch) may go on to
    writes that ask for it shared and then exclusive: it waits for nobody."""
    key = advisory_key("lock-upgrade-probe", U2)
    rank = LockRank.FILES_NAMESPACE
    async with AsyncSession(engine) as session:
        await advisory_xact_lock(session, key, rank=rank)
        await advisory_xact_lock(session, key, shared=True, rank=rank)
        await advisory_xact_lock(session, key, rank=rank)
        await session.rollback()


@pytest.mark.asyncio
@pytest.mark.usefixtures("enforced")
async def test_ascending_ranks_and_a_fresh_transaction_are_fine(
    engine: AsyncEngine, probe_table: Any
) -> None:
    one = select(probe_table.c.id).where(probe_table.c.id == 1)
    two = select(probe_table.c.id).where(probe_table.c.id == 2)
    async with AsyncSession(engine) as session:
        await lock_rows(session, LockRank.ORG_MACHINE, one)
        await lock_rows(session, LockRank.ALLOCATION, two)
        await lock_rows(session, LockRank.ALLOCATION, one)
        await session.commit()
        await lock_rows(session, LockRank.ORG_MACHINE, two)
        await session.rollback()


@pytest.mark.asyncio
@pytest.mark.usefixtures("enforced")
async def test_another_transaction_in_the_same_task_has_an_order_of_its_own(
    engine: AsyncEngine, probe_table: Any
) -> None:
    """Locks held by one session do not bind a second session's transaction,
    even in a task spawned while the first held them (it inherits what its
    parent held): the order is per transaction, and the second transaction
    taking a lower rank is not half of a cycle with the first."""
    one = select(probe_table.c.id).where(probe_table.c.id == 1)
    two = select(probe_table.c.id).where(probe_table.c.id == 2)
    async with AsyncSession(engine) as first, AsyncSession(engine) as second:
        await lock_rows(first, LockRank.ALLOCATION, one)

        async def other() -> None:
            await lock_rows(second, LockRank.ORG_MACHINE, two)

        await asyncio.create_task(other())
        with pytest.raises(LockOrderError, match="org_machine"):
            await lock_rows(first, LockRank.ORG_MACHINE, two)
        await first.rollback()
        await second.rollback()


@pytest.mark.asyncio
@pytest.mark.usefixtures("enforced")
async def test_a_lock_that_never_waits_may_come_in_any_order(
    engine: AsyncEngine, probe_table: Any
) -> None:
    """A skip-locked claim cannot wait on anyone, so it cannot close a cycle."""
    async with AsyncSession(engine) as session:
        await lock_rows(
            session, LockRank.ALLOCATION, select(probe_table.c.id).where(probe_table.c.id == 1)
        )
        claimed = await lock_rows(
            session,
            LockRank.ORG_MACHINE,
            select(probe_table.c.id).where(probe_table.c.id == 2),
            skip_locked=True,
        )
        assert claimed.scalars().all() == [2]
        await session.rollback()


@pytest.mark.asyncio
async def test_a_broken_order_is_counted_when_not_enforced(
    engine: AsyncEngine, probe_table: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(CHECKS_ENV, "0")
    async with AsyncSession(engine) as session:
        await lock_rows(
            session, LockRank.ALLOCATION, select(probe_table.c.id).where(probe_table.c.id == 1)
        )
        with capture_logs() as logs:
            await lock_rows(
                session, LockRank.ORG_MACHINE, select(probe_table.c.id).where(probe_table.c.id == 2)
            )
        await session.rollback()
    assert [entry["rule"] for entry in logs if entry["event"] == "db.lock_rule_violated"] == [
        "lock_order"
    ]


class _FakeStore:
    """Stands in for anything that leaves Postgres."""

    def __init__(self) -> None:
        self.calls = 0

    @io_boundary("test.store.put")
    async def put(self) -> None:
        self.calls += 1

    @io_boundary("test.store.get")
    async def get(self) -> AsyncIterator[bytes]:
        self.calls += 1
        yield b"x"


@pytest.mark.asyncio
@pytest.mark.usefixtures("enforced")
async def test_io_under_lock_sentinel(engine: AsyncEngine, probe_table: Any) -> None:
    store = _FakeStore()
    async with AsyncSession(engine) as session:
        await store.put()
        await lock_rows(
            session, LockRank.FILES_NODE, select(probe_table.c.id).where(probe_table.c.id == 1)
        )
        with pytest.raises(IOUnderLockError, match=r"test\.store\.put .*files_node"):
            await store.put()
        with pytest.raises(IOUnderLockError, match=r"test\.store\.get"):
            [chunk async for chunk in store.get()]
        assert store.calls == 1
        with io_outside_locks.allow(reason="the test says so"):
            await store.put()
        assert store.calls == 2
        await session.commit()
        await store.put()
        assert store.calls == 3


def test_an_escape_needs_its_reason() -> None:
    with pytest.raises(ValueError, match="reason"), io_outside_locks.allow(reason=" "):
        pass


@io_boundary_class("test.provider")
class _GuardedProvider:
    async def start(self) -> str:
        return "started"

    def kind(self) -> str:
        return "plain"


@pytest.mark.asyncio
@pytest.mark.usefixtures("enforced")
async def test_a_guarded_class_checks_every_public_coroutine(
    engine: AsyncEngine, probe_table: Any
) -> None:
    provider = _GuardedProvider()
    async with AsyncSession(engine) as session:
        await lock_rows(
            session, LockRank.ALLOCATION, select(probe_table.c.id).where(probe_table.c.id == 1)
        )
        assert provider.kind() == "plain"
        with pytest.raises(IOUnderLockError, match=r"test\.provider\.start"):
            await provider.start()
        await session.rollback()
    assert await provider.start() == "started"
