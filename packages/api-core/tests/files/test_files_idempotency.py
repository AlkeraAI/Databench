"""Exactly-once for a non-GET, proved against real Postgres.

Every test here counts a *real* effect — a row the runner inserts — as well as
the number of times the runner ran, because a runner that ran twice and wrote
once would satisfy a call-count assertion and still be the bug.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Callable

import pytest
from alkera_core.authz.enums import PrincipalKind
from alkera_core.authz.principal import ActingContext, Principal
from alkera_core.files.errors import InvalidRequest
from alkera_core.files.idempotency import (
    MissingIdempotencyKey,
    StoredResponse,
    idempotent,
    principal_column,
)
from alkera_core.files.repo import FilesRepo
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from tests.files._kit.factory import FilesOrg

ROUTE = "POST /files/items"


@pytest.fixture
def ctx(files_org: FilesOrg) -> ActingContext:
    return ActingContext(
        acting_principal=Principal(
            kind=PrincipalKind.USER,
            id=str(files_org.member_id),
            org_id=files_org.org_team_id,
        )
    )


class Runner:
    """A runner with a side effect a test can count independently of calls.

    The effect is a row in a scratch table, written on the caller's session so
    it lives or dies with the idempotency claim — exactly like a real Files
    mutation would.
    """

    def __init__(self, repo: FilesRepo, tag: str = "e") -> None:
        self._repo = repo
        self._tag = tag
        self.calls = 0

    async def __call__(self) -> StoredResponse:
        self.calls += 1
        await self._repo.session.execute(
            text("INSERT INTO files_idem_effects (id, tag) VALUES (:id, :tag)"),
            {"id": uuid.uuid4(), "tag": self._tag},
        )
        return StoredResponse(
            status=201,
            body=b'{"id":"fixed"}',
            headers={"ETag": '"1"'},
        )


async def _effects(session: AsyncSession, tag: str) -> int:
    result = await session.execute(
        text("SELECT count(*) FROM files_idem_effects WHERE tag = :tag"), {"tag": tag}
    )
    return int(result.scalar_one())


@pytest.fixture
async def effects_table(files_session: AsyncSession) -> AsyncIterator[None]:
    """The scratch table the runner's effect lands in.

    Deliberately not a Files table: the point is to count an effect the
    idempotency layer knows nothing about, so it cannot be the same row the
    layer itself writes.

    Dropped again on the way out. A table no model declares is drift as far as
    ``alembic check`` is concerned, so leaving it behind makes the migration
    suite fail for whoever runs after this module on the same database.
    """
    await files_session.execute(
        text("CREATE TABLE IF NOT EXISTS files_idem_effects (id uuid PRIMARY KEY, tag text)")
    )
    # The effect runs under the Files app role, exactly as a real mutation does.
    await files_session.execute(text("GRANT ALL ON files_idem_effects TO alkera_files_app"))
    # Emptied per test: a previous test's rows would otherwise be counted as
    # this one's effects.
    await files_session.execute(text("TRUNCATE files_idem_effects"))
    await files_session.commit()
    try:
        yield
    finally:
        await files_session.rollback()
        await files_session.execute(text("DROP TABLE IF EXISTS files_idem_effects"))
        await files_session.commit()


@pytest.fixture
def repo_factory(
    files_org: FilesOrg,
) -> Callable[[AsyncSession], FilesRepo]:
    def make(session: AsyncSession) -> FilesRepo:
        return FilesRepo(session, files_org.scope)

    return make


async def test_a_first_request_runs_once_and_stores_its_answer(
    repo: FilesRepo,
    ctx: ActingContext,
    files_org: FilesOrg,
    files_session: AsyncSession,
    effects_table: None,
) -> None:
    runner = Runner(repo, tag="first")
    answer = await idempotent(
        repo,
        ctx,
        route=ROUTE,
        key="k-first",
        request_hash=b"\x01\x02",
        run=runner,
    )
    assert answer == StoredResponse(201, b'{"id":"fixed"}', {"ETag": '"1"'})
    assert runner.calls == 1
    assert await _effects(files_session, "first") == 1
    stored = await files_session.execute(
        text(
            "SELECT status FROM file_idempotency_keys WHERE key = 'k-first' AND org_team_id = :org"
        ),
        {"org": files_org.org_team_id},
    )
    assert stored.scalar_one() == "succeeded"


async def test_a_replay_after_completion_returns_the_stored_bytes_and_no_second_effect(
    repo: FilesRepo,
    ctx: ActingContext,
    files_session: AsyncSession,
    effects_table: None,
) -> None:
    first = Runner(repo, tag="replay")
    original = await idempotent(
        repo, ctx, route=ROUTE, key="k-replay", request_hash=b"\xaa", run=first
    )
    second = Runner(repo, tag="replay")
    again = await idempotent(
        repo, ctx, route=ROUTE, key="k-replay", request_hash=b"\xaa", run=second
    )
    assert again == original
    assert again.body == original.body
    assert second.calls == 0
    assert await _effects(files_session, "replay") == 1


async def test_the_same_key_with_a_different_body_is_refused(
    repo: FilesRepo,
    ctx: ActingContext,
    files_session: AsyncSession,
    effects_table: None,
) -> None:
    await idempotent(
        repo, ctx, route=ROUTE, key="k-mismatch", request_hash=b"\x01", run=Runner(repo, "mm")
    )
    clashing = Runner(repo, "mm")
    with pytest.raises(InvalidRequest) as raised:
        await idempotent(
            repo, ctx, route=ROUTE, key="k-mismatch", request_hash=b"\x02", run=clashing
        )
    assert raised.value.code == "files.idempotency_mismatch"
    assert raised.value.status == 422
    assert clashing.calls == 0
    assert await _effects(files_session, "mm") == 1


@pytest.mark.parametrize(
    ("route_b", "principal_b", "shared"),
    [
        pytest.param("POST /files/other", False, False, id="other-route-is-a-different-key"),
        pytest.param(ROUTE, True, False, id="other-principal-is-a-different-key"),
        pytest.param(ROUTE, False, True, id="same-route-and-principal-is-the-same-key"),
    ],
)
async def test_the_key_is_scoped_to_the_route_and_the_principal(
    repo: FilesRepo,
    ctx: ActingContext,
    files_org: FilesOrg,
    files_session: AsyncSession,
    effects_table: None,
    route_b: str,
    principal_b: bool,
    shared: bool,
) -> None:
    tag = f"scope-{route_b}-{principal_b}"
    other = (
        ActingContext(
            acting_principal=Principal(
                kind=PrincipalKind.USER,
                id=str(files_org.admin_id),
                org_id=files_org.org_team_id,
            )
        )
        if principal_b
        else ctx
    )
    await idempotent(
        repo, ctx, route=ROUTE, key="k-scope", request_hash=b"\x07", run=Runner(repo, tag)
    )
    second = Runner(repo, tag)
    await idempotent(repo, other, route=route_b, key="k-scope", request_hash=b"\x07", run=second)
    assert second.calls == (0 if shared else 1)
    assert await _effects(files_session, tag) == (1 if shared else 2)


def test_a_missing_key_is_a_428_the_caller_raises() -> None:
    """428 and not 400: the request becomes acceptable once the header is added."""
    error = MissingIdempotencyKey()
    assert error.status == 428
    assert error.code == "files.idempotency_key_required"


def test_a_non_uuid_agent_principal_folds_to_a_stable_distinct_uuid(
    files_org: FilesOrg,
) -> None:
    """Two agent sessions must never collapse onto one key."""

    def agent(session: str) -> ActingContext:
        return ActingContext(
            acting_principal=Principal(
                kind=PrincipalKind.AGENT, id=session, org_id=files_org.org_team_id
            )
        )

    first = principal_column(agent("chat-a"))
    assert first == principal_column(agent("chat-a"))
    assert first != principal_column(agent("chat-b"))
