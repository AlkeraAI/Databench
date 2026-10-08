"""An adopt and a park of the same object cannot interleave.

A writer that finds its bytes already under their content key adopts the
object and drops its own staged copy. The object is old by definition, so the
sweep's age horizon never protects it, and until the version row commits no
root named it either. These cases drive a real ``ContentService`` and a real
``Janitor`` against the same filesystem bucket and the same Postgres, on two
connections, and stop each side at a named point with explicit barriers — no
sleeps decide the order.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from typing import Any

import pytest
from alkera_core.config import settings
from alkera_core.files.checkpoints import PausingCheckpoints
from alkera_core.files.content import ContentService
from alkera_core.files.gc import Janitor, SweepResult, object_lock_key
from alkera_core.files.hashing import StreamHasher
from alkera_core.files.ids import OrgScope
from alkera_core.files.repo import FilesRepo
from alkera_core.files.store import keys
from alkera_core.files.store.filesystem import FilesystemStore
from alkera_core.models.files.platform import FileSweepShard
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from tests.files._kit.engine import open_files_session
from tests.files.content.conftest import ContentRig, stream

pytestmark = pytest.mark.asyncio

SIZE = settings.files_inline_max_bytes + 4096
PAYLOAD = (hashlib.sha256(b"adopt-race").digest() * (SIZE // 32 + 1))[:SIZE]


class _AdminFactory:
    def __init__(self, store: Any) -> None:
        self._store = store

    async def for_domain(self, domain_id: Any) -> Any:
        raise AssertionError("the janitor never takes a domain-bound handle")

    def admin(self) -> Any:
        return self._store


@pytest.fixture
async def janitor_session(files_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """The sweep's own connection: the guard is only real across two backends."""
    session = await open_files_session(files_engine)
    try:
        yield session
    finally:
        await session.close()


@pytest.fixture
async def shard(janitor_session: AsyncSession) -> int:
    import uuid

    number = uuid.uuid4().int % 1_000_000 + 2_000_000
    janitor_session.add(FileSweepShard(shard=number, cursor={}))
    await janitor_session.commit()
    return number


def make_janitor(
    rig: ContentRig, session: AsyncSession, checkpoints: PausingCheckpoints | None = None
) -> Janitor:
    admin = FilesystemStore(rig.root, clock=lambda: rig.service._clock.now(), layout="bucket")

    def repo_for(scope: OrgScope) -> FilesRepo:
        return FilesRepo(session, scope)

    return Janitor(repo_for, _AdminFactory(admin), rig.service._clock, checkpoints)


async def orphan_object(rig: ContentRig) -> str:
    """The shape a refused commit leaves: the bytes published, no row naming them."""
    hasher = StreamHasher()
    hasher.update(PAYLOAD)
    key = keys.object_key(hasher.finalize().content_hash)
    path = rig.object_path(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(PAYLOAD)
    return key


async def read_back(rig: ContentRig, version_id: Any) -> bytes:
    chunks = [chunk async for chunk in await rig.service.open(version_id)]
    return b"".join(chunks)


async def test_a_sweep_that_runs_while_a_writer_holds_an_adopted_object_lets_it_go(
    content_rig: ContentRig, janitor_session: AsyncSession, shard: int
) -> None:
    """The writer has adopted the object and dropped its own copy; its commit
    has not landed. A whole sweep runs in that gap. The claim on the session
    row is the only root there is, and it has to be enough."""
    rig = content_rig
    key = await orphan_object(rig)
    rig.checkpoints.pause("content.before_commit")
    writing = asyncio.create_task(
        rig.service.put_version(
            rig.node_id("a.bin"), stream(PAYLOAD), size_declared=SIZE, if_match=0
        )
    )
    try:
        await rig.checkpoints.wait_paused("content.before_commit")
        assert not rig.object_path(keys.incoming_key(_session_of(rig), "object")).exists()
        result = await make_janitor(rig, janitor_session).sweep(
            rig.domain_id, org=OrgScope(org_team_id=rig.org.org_team_id), shard=shard, dry_run=False
        )
    finally:
        rig.checkpoints.release("content.before_commit")
    info = await writing

    assert isinstance(result, SweepResult)
    assert key in result.skipped, "the sweep did not see the writer's claim"
    assert key not in result.moved
    assert rig.object_path(key).exists()
    assert await read_back(rig, info.id) == PAYLOAD


def _session_of(rig: ContentRig) -> Any:
    import uuid

    calls = [str(call.key) for call in rig.store.calls if str(call.key).startswith("incoming/")]
    return uuid.UUID(calls[0].split("/")[1])


async def _waiting_on_advisory_lock(session: AsyncSession) -> bool:
    row = (
        await session.execute(
            text(
                "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted "
                "AND database = (SELECT oid FROM pg_database WHERE datname = current_database())"
            )
        )
    ).first()
    await session.rollback()
    return bool(row is not None and row[0] > 0)


async def test_a_writer_that_arrives_while_the_sweep_is_parking_takes_the_object_back(
    content_rig: ContentRig,
    janitor_session: AsyncSession,
    shard: int,
    files_engine: AsyncEngine,
) -> None:
    """The other order. The sweep has re-checked the key and is about to move
    it; the writer's claim queues behind the object's lock, so the move lands
    first and the writer finds the object parked. It moves it back, and the
    version it commits reads."""
    rig = content_rig
    key = await orphan_object(rig)
    gc_points = PausingCheckpoints()
    gc_points.pause("gc.before_move")
    scope = OrgScope(org_team_id=rig.org.org_team_id)
    sweeping = asyncio.create_task(
        make_janitor(rig, janitor_session, gc_points).sweep(
            rig.domain_id, org=scope, shard=shard, dry_run=False
        )
    )
    observer = await open_files_session(files_engine)
    try:
        await gc_points.wait_paused("gc.before_move")
        writing = asyncio.create_task(
            rig.service.put_version(
                rig.node_id("a.bin"), stream(PAYLOAD), size_declared=SIZE, if_match=0
            )
        )
        # The barrier: the writer is provably queued on the object's lock,
        # read off Postgres's own lock table rather than guessed from a delay.
        for _ in range(4000):
            if await _waiting_on_advisory_lock(observer):
                break
            await asyncio.sleep(0.005)
        else:
            pytest.fail("the writer never queued on the object's lock")
        assert rig.object_path(key).exists(), "nothing has moved yet"
    finally:
        gc_points.release("gc.before_move")
        await observer.close()
    result = await sweeping
    info = await writing

    assert isinstance(result, SweepResult)
    assert result.moved == (key,), "the sweep held the lock, so its move goes first"
    assert rig.object_path(key).exists(), "the writer did not take the parked object back"
    assert not rig.object_path(keys.deleted_key(key)).exists()
    moves = [str(c.key) for c in rig.store.calls if c.method == "move"]
    assert moves == [keys.deleted_key(key)], "the object was re-published, not un-parked"
    assert await read_back(rig, info.id) == PAYLOAD


async def test_the_object_lock_is_named_per_org_and_per_key() -> None:
    """Two orgs holding the same bytes never queue behind each other."""
    import uuid

    a, b = uuid.uuid4(), uuid.uuid4()
    assert object_lock_key(a, "objects/aa/bb/aabb") != object_lock_key(b, "objects/aa/bb/aabb")
    assert object_lock_key(a, "objects/aa/bb/aabb") != object_lock_key(a, "objects/aa/bb/aabc")


async def test_a_route_that_carries_its_claim_to_the_requests_commit_is_skipped_not_waited_on(
    content_rig: ContentRig, janitor_session: AsyncSession, shard: int
) -> None:
    """The ``put_content`` route runs its ``ContentService`` over a JOINED repo:
    every ``transaction()`` is a savepoint inside the request's own
    transaction, so the claim row is invisible and the object's lock is held
    from the claim until the request commits. The sweep must not queue behind
    that for the length of an upload: it tries the lock, finds it held, and
    lets the key go this pass. The object survives and the version reads."""
    rig = content_rig
    key = await orphan_object(rig)
    await rig.session.begin()
    joined = FilesRepo.joined(rig.session, OrgScope(org_team_id=rig.org.org_team_id))
    service = ContentService(
        joined, rig.ctx, rig.service._clock, rig.service._store, checkpoints=rig.checkpoints
    )
    rig.checkpoints.pause("content.before_commit")
    writing = asyncio.create_task(
        service.put_version(rig.node_id("a.bin"), stream(PAYLOAD), size_declared=SIZE, if_match=0)
    )
    try:
        await rig.checkpoints.wait_paused("content.before_commit")
        async with asyncio.timeout(10):
            result = await make_janitor(rig, janitor_session).sweep(
                rig.domain_id,
                org=OrgScope(org_team_id=rig.org.org_team_id),
                shard=shard,
                dry_run=False,
            )
    finally:
        rig.checkpoints.release("content.before_commit")
    info = await writing
    await rig.session.commit()

    assert isinstance(result, SweepResult)
    assert result.skipped == (key,), "the sweep waited on, or took, a key a request holds"
    assert rig.object_path(key).exists()
    assert await read_back(rig, info.id) == PAYLOAD
