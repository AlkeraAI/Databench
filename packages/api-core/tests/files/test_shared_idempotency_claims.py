"""The shared exactly-once claim (``alkera_core.idempotency``) against real Postgres.

Its rows live in the table Files keeps its replay records in, which is why it
is proved here beside Files' own idempotency tests, on the same seeded orgs.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from alkera_core.idempotency import (
    MAX_RETENTION,
    Claim,
    IdempotencyMismatch,
    IdempotencyScope,
    KeyOwner,
    Replay,
    body_digest,
    register_scope,
    registered_scopes,
    replay_or_claim,
    settle,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from tests.files._kit.factory import FilesOrg

WEEK = register_scope("test.shared_claims.week", retention=timedelta(days=7))
HOUR = register_scope("test.shared_claims.hour", retention=timedelta(hours=1))


def _owner(org: FilesOrg) -> KeyOwner:
    return KeyOwner(org_id=org.org_team_id, principal_id=org.member_id)


async def _row(session: AsyncSession, org: FilesOrg, key: str) -> dict[str, object]:
    found = await session.execute(
        text(
            "SELECT scope, route, status, expires_at - created_at AS kept "
            "FROM file_idempotency_keys WHERE org_team_id = :org AND key = :key"
        ),
        {"org": org.org_team_id, "key": key},
    )
    return dict(found.mappings().one())


# --- the registry -------------------------------------------------------------


def test_registering_a_scope_twice_with_its_own_retention_answers_the_same_scope() -> None:
    again = register_scope("test.shared_claims.week", retention=timedelta(days=7))
    assert again == IdempotencyScope("test.shared_claims.week", timedelta(days=7))
    assert registered_scopes()["test.shared_claims.week"] == again


def test_a_second_retention_for_one_scope_is_refused() -> None:
    with pytest.raises(ValueError, match="already registered"):
        register_scope("test.shared_claims.week", retention=timedelta(days=8))
    assert registered_scopes()["test.shared_claims.week"].retention == timedelta(days=7)


@pytest.mark.parametrize(
    ("name", "retention"),
    [
        pytest.param("", timedelta(hours=1), id="empty-name"),
        pytest.param("x" * 65, timedelta(hours=1), id="name-wider-than-the-column"),
        pytest.param("test.zero", timedelta(0), id="no-retention"),
        pytest.param("test.forever", MAX_RETENTION + timedelta(seconds=1), id="past-the-ceiling"),
    ],
)
def test_a_scope_the_table_cannot_hold_is_refused(name: str, retention: timedelta) -> None:
    with pytest.raises(ValueError, match="idempotency scope"):
        register_scope(name, retention=retention)
    assert name not in registered_scopes()


def test_a_body_digest_ignores_key_order_and_sees_every_value() -> None:
    first = body_digest({"name": "gpu", "storage_gb": 100})
    assert first == body_digest({"storage_gb": 100, "name": "gpu"})
    assert first != body_digest({"name": "gpu", "storage_gb": 101})
    assert first != body_digest({"name": "gpu", "storage_gb": "100"})


async def test_an_unregistered_scope_claims_nothing(
    files_session: AsyncSession, files_org: FilesOrg
) -> None:
    stray = IdempotencyScope("test.shared_claims.never_registered", timedelta(hours=1))
    with pytest.raises(ValueError, match="not registered"):
        await replay_or_claim(files_session, stray, "k", b"d", owner=_owner(files_org))
    count = await files_session.execute(
        text("SELECT count(*) FROM file_idempotency_keys WHERE scope = :s"), {"s": stray.name}
    )
    assert count.scalar_one() == 0


# --- claim, settle, replay ------------------------------------------------------


async def test_a_settled_claim_is_replayed_with_its_answer(
    files_session: AsyncSession, files_org: FilesOrg
) -> None:
    key = uuid.uuid4().hex
    first = await replay_or_claim(files_session, WEEK, key, b"body", owner=_owner(files_org))
    assert first == Claim(owner=_owner(files_org), route=WEEK.name, key=key)
    await settle(files_session, first, {"machine_id": "m-1"})
    await files_session.commit()

    again = await replay_or_claim(files_session, WEEK, key, b"body", owner=_owner(files_org))
    await files_session.commit()

    assert again == Replay(answer={"machine_id": "m-1"})


async def test_the_same_key_for_a_different_request_is_refused(
    files_session: AsyncSession, files_org: FilesOrg
) -> None:
    key = uuid.uuid4().hex
    claim = await replay_or_claim(files_session, WEEK, key, b"first", owner=_owner(files_org))
    assert isinstance(claim, Claim)
    await settle(files_session, claim, {"machine_id": "m-1"})
    await files_session.commit()

    with pytest.raises(IdempotencyMismatch) as refused:
        await replay_or_claim(files_session, WEEK, key, b"second", owner=_owner(files_org))
    await files_session.rollback()

    assert (refused.value.code, refused.value.status) == ("idempotency.mismatch", 409)
    assert refused.value.key == key


async def test_a_claim_whose_writer_never_answered_is_claimed_again(
    files_session: AsyncSession, files_org: FilesOrg
) -> None:
    key = uuid.uuid4().hex
    await replay_or_claim(files_session, WEEK, key, b"body", owner=_owner(files_org))
    await files_session.commit()  # the claim landed, its answer never did

    again = await replay_or_claim(files_session, WEEK, key, b"body", owner=_owner(files_org))
    await files_session.commit()

    assert isinstance(again, Claim)
    assert (await _row(files_session, files_org, key))["status"] == "in_progress"


async def test_another_principal_with_the_same_key_owns_a_key_of_its_own(
    files_session: AsyncSession, files_org: FilesOrg
) -> None:
    key = uuid.uuid4().hex
    mine = await replay_or_claim(files_session, WEEK, key, b"mine", owner=_owner(files_org))
    assert isinstance(mine, Claim)
    await settle(files_session, mine, {"machine_id": "m-1"})
    other = KeyOwner(org_id=files_org.org_team_id, principal_id=files_org.admin_id)

    theirs = await replay_or_claim(files_session, WEEK, key, b"theirs", owner=other)
    await files_session.commit()

    assert isinstance(theirs, Claim)


# --- each row keeps its scope's retention ----------------------------------------


@pytest.mark.parametrize(
    ("scope", "kept"),
    [
        pytest.param(WEEK, timedelta(days=7), id="week"),
        pytest.param(HOUR, timedelta(hours=1), id="hour"),
    ],
)
async def test_a_claim_expires_when_its_scope_says(
    files_session: AsyncSession, files_org: FilesOrg, scope: IdempotencyScope, kept: timedelta
) -> None:
    key = uuid.uuid4().hex
    await replay_or_claim(files_session, scope, key, b"body", owner=_owner(files_org))
    await files_session.commit()

    row = await _row(files_session, files_org, key)

    assert row == {"scope": scope.name, "route": scope.name, "status": "in_progress", "kept": kept}


async def test_a_route_narrows_a_key_within_its_scope(
    files_session: AsyncSession, files_org: FilesOrg
) -> None:
    key = uuid.uuid4().hex
    one = await replay_or_claim(
        files_session, WEEK, key, b"a", owner=_owner(files_org), route="POST /one"
    )
    two = await replay_or_claim(
        files_session, WEEK, key, b"b", owner=_owner(files_org), route="POST /two"
    )
    await files_session.commit()

    assert isinstance(one, Claim)
    assert isinstance(two, Claim)
    assert (one.route, two.route) == ("POST /one", "POST /two")
