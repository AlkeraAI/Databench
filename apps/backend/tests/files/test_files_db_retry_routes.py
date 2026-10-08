"""The two Postgres verdicts that mean "run it again" never reach a Files client.

``40001`` and ``40P01`` are scheduling outcomes, not bugs: Postgres rolled the
losing transaction back whole, so replaying is safe *and* is the only honest
answer — an ordinary rename that collided with a sibling rename would otherwise
come back as a 500. The replay is wired in exactly one place, the
``idempotent_route`` wrapper, so it rides the idempotency claim every mutating
route already takes and can never become a second effect.

The collision is injected here on purpose: a real deadlock needs two racing
writers and is proved against the real database at the library level
(``packages/api-core/tests/files/concurrency/test_files_lock_ordering_pairs.py``).
What is real here is the *state* a deadlock leaves behind — the injected failure
aborts the transaction for real before it raises, so a wrapper that replayed
without rolling back would fail on ``25P02`` instead of passing.

Also pins the two contracts the wrapper's neighbours in ``_deps`` carry: the
platform-role window refuses to hand a request back with the tenant stamp gone,
and the etag parser is a public name.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from _files_kit import FilesFixtures, FilesOrgFixture, node_etag
from alkera_core.files import stars
from alkera_core.files.errors import InvalidRequest
from alkera_core.files.repo import APP_ROLE, ORG_SETTING, FilesRepo
from asyncpg.exceptions import DeadlockDetectedError, UniqueViolationError
from backend.api.deps.files import _parse_etag, as_platform, parse_etag
from backend.api.routes.files import items
from backend.services.files import operations_runner
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio

BASE = "/api/v1/files"


class FailsThenWorks:
    """A real driver failure on the first `times` calls, the real star after.

    The failing call first runs a statement Postgres refuses, so the session it
    leaves behind is the one a genuine deadlock leaves — aborted — and only then
    raises the driver error whose SQLSTATE the ladder reads.
    """

    def __init__(self, orig: BaseException, *, times: int = 1) -> None:
        self._orig = orig
        self.remaining = times
        self.calls = 0

    async def __call__(self, repo: FilesRepo, *args: Any, **kwargs: Any) -> bool:
        self.calls += 1
        if self.remaining <= 0:
            return bool(await _REAL_STAR(repo, *args, **kwargs))
        self.remaining -= 1
        try:
            await repo.session.execute(text("SELECT 1 / 0"))
        except DBAPIError:
            pass
        raise DBAPIError("UPDATE file_stars", {}, self._orig)


_REAL_STAR = stars.star


async def _keys(session: AsyncSession, org_id: uuid.UUID) -> int:
    rows = await session.execute(
        text("SELECT count(*) FROM file_idempotency_keys WHERE org_team_id = :org"),
        {"org": org_id},
    )
    return int(rows.scalar_one())


async def _starred(session: AsyncSession, node_id: uuid.UUID) -> int:
    rows = await session.execute(
        text("SELECT count(*) FROM file_stars WHERE node_id = :node"), {"node": node_id}
    )
    return int(rows.scalar_one())


async def test_a_deadlocked_mutation_is_replayed_and_spends_its_key_once(
    files_client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RED before the wiring: the PUT answers with the raw ``40P01``, nothing
    is starred, and the key row the failed attempt wrote is gone with it."""
    drive = (await files_client.get(f"{BASE}/drives")).json()
    node = await fx.node(b"raced.txt")
    fail = FailsThenWorks(DeadlockDetectedError("deadlock detected"))
    monkeypatch.setattr(stars, "star", fail)
    url = f"{BASE}/drives/{drive['id']}/items/{node.id}/star"
    headers = {
        "Idempotency-Key": uuid.uuid4().hex,
        "If-Match": await node_etag(real_session, node.id),
    }

    answered = await files_client.put(url, headers=headers)

    assert answered.status_code == 200, answered.text
    assert answered.json()["starred"] is True
    assert fail.calls == 2, "the wrapper did not replay the deadlocked attempt"
    assert await _starred(real_session, node.id) == 1
    assert await _keys(real_session, files_org.org.org_id) == 1, "the replay double-spent the key"

    replayed = await files_client.put(url, headers=headers)

    assert replayed.status_code == 200
    assert replayed.content == answered.content
    assert await _starred(real_session, node.id) == 1
    assert await _keys(real_session, files_org.org.org_id) == 1
    assert fail.calls == 2, "the stored answer was re-run instead of replayed"


async def test_a_failure_that_is_not_a_collision_is_never_replayed(
    files_client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    real_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A unique violation is a real conflict: replaying it would either repeat
    an effect or hide it, so the ladder answers on the first attempt.

    The answer is the 409 every unique violation gets, never a retryable one —
    the contention ladder names only the two verdicts that mean "run it again",
    and the statement the driver quotes never reaches the caller.
    """
    drive = (await files_client.get(f"{BASE}/drives")).json()
    node = await fx.node(b"broken.txt")
    fail = FailsThenWorks(UniqueViolationError("duplicate key"), times=99)
    monkeypatch.setattr(stars, "star", fail)
    headers = {
        "Idempotency-Key": uuid.uuid4().hex,
        "If-Match": await node_etag(real_session, node.id),
    }

    answered = await files_client.put(
        f"{BASE}/drives/{drive['id']}/items/{node.id}/star", headers=headers
    )

    assert answered.status_code == 409
    assert answered.json()["error"]["code"] == "conflict"
    assert "retry-after" not in answered.headers
    assert "file_stars" not in answered.text
    assert fail.calls == 1, "a non-retryable failure was replayed"
    assert await _starred(real_session, node.id) == 0


async def test_a_platform_window_refuses_to_return_with_the_tenant_stamp_gone(
    real_session: AsyncSession,
    files_org: FilesOrgFixture,
) -> None:
    """RED before the guard: the block commits, the restore opens (and ends) a
    transaction of its own, and the request runs on as the platform role with no
    ``app.org_id`` beside it — RLS unbound, the fail-open direction."""
    await real_session.rollback()
    await real_session.begin()
    await real_session.execute(text(f"SET LOCAL ROLE {APP_ROLE}"))
    await real_session.execute(
        text("SELECT set_config(:name, :org, true)"),
        {"name": ORG_SETTING, "org": str(files_org.org.org_id)},
    )

    with pytest.raises(RuntimeError, match="ended the transaction"):
        async with as_platform(real_session):
            await real_session.commit()

    role = (await real_session.execute(text("SELECT current_user"))).scalar_one()
    assert role != APP_ROLE, "the request was left running as the platform role"
    await real_session.rollback()


async def test_the_etag_parser_is_a_public_name() -> None:
    """``uploads.py`` reads the same header on a body that makes it optional, so
    the parser is part of ``_deps``' surface rather than its inside."""
    assert parse_etag('W/"7"') == 7
    assert parse_etag(" 12 ") == 12
    assert _parse_etag is parse_etag
    with pytest.raises(InvalidRequest):
        parse_etag("not-an-etag")


class QueuesThenDeadlocks:
    """Queue the handler's background work for real, then lose the deadlock.

    The shape a handler that queues inline work has: the queue call is the last
    thing it does, so an attempt that collided has already put its task on the
    request's collector before the ladder replays it.
    """

    def __init__(self, real: Any) -> None:
        self._real = real
        self.calls = 0

    def __call__(self, background: Any, *args: Any, **kwargs: Any) -> None:
        self.calls += 1
        self._real(background, *args, **kwargs)
        if self.calls == 1:
            raise DBAPIError("INSERT INTO file_ops", {}, DeadlockDetectedError("deadlock detected"))


async def test_a_replayed_attempt_never_queues_its_background_work_twice(
    files_client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RED before the fix: `BackgroundTasks` is the request's single instance,
    so the attempt that lost the deadlock leaves its inline operation queued and
    the replay queues a second one -- one click, the operation run twice."""
    drive = (await files_client.get(f"{BASE}/drives")).json()
    source = await fx.node(b"copied.txt")
    target = await fx.node(b"into", kind="folder")
    ran: list[str] = []

    async def record(job: Any, queueing_session: AsyncSession) -> None:
        ran.append(job.kind)
        await queueing_session.commit()

    monkeypatch.setattr(operations_runner, "run_inline", record)
    queued = QueuesThenDeadlocks(operations_runner.queue_inline)
    monkeypatch.setattr(items, "queue_inline", queued)

    answered = await files_client.post(
        f"{BASE}/drives/{drive['id']}/items/{source.id}/copy",
        headers={"Idempotency-Key": uuid.uuid4().hex},
        json={"parentId": str(target.id)},
    )

    assert answered.status_code == 202, answered.text
    assert queued.calls == 2, "the wrapper did not replay the deadlocked attempt"
    assert ran == ["copy"], f"the failed attempt's background work ran too: {ran}"


async def test_an_ordinary_mutation_still_queues_its_background_work(
    files_client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other half of the same contract: an attempt that answers hands its
    tasks to the request, so the no-collision path is unchanged."""
    drive = (await files_client.get(f"{BASE}/drives")).json()
    source = await fx.node(b"plain.txt")
    target = await fx.node(b"dest", kind="folder")
    ran: list[str] = []

    async def record(job: Any, queueing_session: AsyncSession) -> None:
        ran.append(job.kind)
        await queueing_session.commit()

    monkeypatch.setattr(operations_runner, "run_inline", record)

    answered = await files_client.post(
        f"{BASE}/drives/{drive['id']}/items/{source.id}/copy",
        headers={"Idempotency-Key": uuid.uuid4().hex},
        json={"parentId": str(target.id)},
    )

    assert answered.status_code == 202, answered.text
    assert ran == ["copy"], "the wrapper swallowed the answering attempt's background work"
