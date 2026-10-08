"""What provisioning a session machine owes the money it is spending.

Once the provider has handed back a pod id that pod costs money every minute,
and the meter only ever finds a pod through a committed ``provider_machine_id``.
So the two ways a live pod goes untracked are covered here: the request being
cut between the provider's answer and the commit, and the retry that a client's
own control timeout invites buying a second machine.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from typing import Any

import pytest
from alkera_core.authz import ActingContext
from alkera_core.compute.provider import RUNPOD
from alkera_core.compute.reconcile import pod_name_for, reconcile_pods
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import User
from alkera_core.models.compute import ComputeAllocation, ComputeMachineType
from backend.services.compute import service
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tests._compute_helpers import FakeProvider, make_grant, make_machine_type
from tests.conftest import OrgWithAdmin, login

pytestmark = pytest.mark.xdist_group("compute-fleet")


class CancelWhenThePodExists(FakeProvider):
    """The client-disconnect window, made deterministic: the running task is
    cancelled the instant a paid pod exists, so the cancellation lands on the
    very next await — the one that records the pod's id. Harsher than a real
    disconnect, which cannot reach inside the shielded section at all."""

    async def create_pod(
        self, *, name: str, machine_type: ComputeMachineType, ssh_public_key: str
    ) -> str:
        pod_id = await super().create_pod(
            name=name, machine_type=machine_type, ssh_public_key=ssh_public_key
        )
        task = asyncio.current_task()
        assert task is not None
        asyncio.get_running_loop().call_soon(task.cancel)
        return pod_id


def _commit_failing_on(session: AsyncSession, nth: int) -> Callable[[], Coroutine[Any, Any, None]]:
    """The session's commit, but the ``nth`` call raises instead — a database
    that drops the connection at exactly one point of the create."""
    original = session.commit
    calls = {"n": 0}

    async def failing_commit() -> None:
        calls["n"] += 1
        if calls["n"] == nth:
            raise RuntimeError("commit lost the connection")
        await original()

    return failing_commit


def _ctx(org: OrgWithAdmin) -> ActingContext:
    return ActingContext.for_user(user_id=org.admin_id, org_id=org.org_id, email=org.admin_email)


async def _rows(org: OrgWithAdmin) -> list[ComputeAllocation]:
    async with AsyncSessionLocal() as probe:
        return list(
            (
                await probe.execute(
                    select(ComputeAllocation)
                    .where(ComputeAllocation.org_team_id == org.org_id)
                    .order_by(ComputeAllocation.created_at.asc())
                )
            )
            .scalars()
            .all()
        )


async def test_a_cancel_between_the_pod_and_the_commit_still_records_the_pod(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """A cancellation is a BaseException: it used to walk past the guard that
    exists to stop exactly this, leaving a live pod nothing bills or releases."""
    mt = await make_machine_type(real_session)
    await make_grant(real_session, org_team_id=org_admin.org_id, machine_type_id=mt.id)
    provider = CancelWhenThePodExists()
    ctx = _ctx(org_admin)

    async with AsyncSessionLocal() as session:
        user = await session.get(User, org_admin.admin_id)
        assert user is not None
        with pytest.raises(asyncio.CancelledError):
            await service.create_allocation(
                session,
                ctx=ctx,
                user=user,
                machine_type=mt,
                project_path="/tmp/proj",
                session_id="cut-mid-flight",
                provider=provider,
            )

    # The money invariant, however hard the cancel landed: the pod the provider
    # built is named by a committed row, so it is findable — either by the id
    # the record commit wrote, or by the name the intent commit wrote, which is
    # what the reconciler joins on.
    (row,) = await _rows(org_admin)
    assert row.state == "provisioning"
    assert provider.create_calls[0]["name"] == pod_name_for(row.id)
    assert row.provider_machine_id in ("", "fakepod-1")
    async with AsyncSessionLocal() as db:
        await reconcile_pods(db, provider=provider, provider_kind=RUNPOD, now=datetime.now(UTC))
    (row,) = await _rows(org_admin)
    assert row.provider_machine_id == "fakepod-1"
    # The pod is tracked, so it is the meter's to release — never thrown away.
    assert provider.terminate_calls == []


async def test_a_commit_that_fails_before_the_provider_call_buys_nothing(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The intent is committed first, so a database that cannot take it stops
    the whole thing before a cent is spent: no pod, no row."""
    mt = await make_machine_type(real_session)
    await make_grant(real_session, org_team_id=org_admin.org_id, machine_type_id=mt.id)
    provider = FakeProvider()

    async with AsyncSessionLocal() as session:
        user = await session.get(User, org_admin.admin_id)
        assert user is not None
        session.commit = _commit_failing_on(session, 1)  # type: ignore[method-assign]
        with pytest.raises(RuntimeError):
            await service.create_allocation(
                session,
                ctx=_ctx(org_admin),
                user=user,
                machine_type=mt,
                project_path="/tmp/proj",
                session_id="intent-commit-fails",
                provider=provider,
            )

    assert provider.create_calls == []
    assert provider.terminate_calls == []
    assert await _rows(org_admin) == []


async def test_a_pod_whose_id_cannot_be_made_durable_is_still_named_by_its_row(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The id-recording commit fails after the machine exists. The pod is NOT
    thrown away — the row committed its NAME before the provider was called, so
    the machine the customer is paying for is still traceable, and the
    reconciler gives it back its id rather than the plane destroying it."""
    mt = await make_machine_type(real_session)
    await make_grant(real_session, org_team_id=org_admin.org_id, machine_type_id=mt.id)
    provider = FakeProvider()

    async with AsyncSessionLocal() as session:
        user = await session.get(User, org_admin.admin_id)
        assert user is not None
        session.commit = _commit_failing_on(session, 2)  # type: ignore[method-assign]
        with pytest.raises(RuntimeError):
            await service.create_allocation(
                session,
                ctx=_ctx(org_admin),
                user=user,
                machine_type=mt,
                project_path="/tmp/proj",
                session_id="record-commit-fails",
                provider=provider,
            )

    assert [c["pod_id"] for c in provider.create_calls] == ["fakepod-1"]
    (row,) = await _rows(org_admin)
    assert row.state == "provisioning"
    # The join that makes the pod findable: the provider named it after the row.
    assert provider.create_calls[0]["name"] == pod_name_for(row.id)

    async with AsyncSessionLocal() as db:
        summary = await reconcile_pods(
            db, provider=provider, provider_kind=RUNPOD, now=datetime.now(UTC)
        )
    assert summary.adopted == 1
    (row,) = await _rows(org_admin)
    assert row.provider_machine_id == "fakepod-1"
    assert provider.terminate_calls == []


# --------------------------------------------------------------------------- #
# the retry a client's own control timeout invites
# --------------------------------------------------------------------------- #


async def _create(
    session: AsyncSession,
    org: OrgWithAdmin,
    mt: ComputeMachineType,
    provider: FakeProvider,
    *,
    session_id: str,
) -> ComputeAllocation:
    user = await session.get(User, org.admin_id)
    assert user is not None
    return await service.create_allocation(
        session,
        ctx=_ctx(org),
        user=user,
        machine_type=mt,
        project_path="/tmp/proj",
        session_id=session_id,
        provider=provider,
    )


async def test_a_repeat_for_one_session_gets_the_machine_it_already_has(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """Provisioning outlives the client's control timeout, so the retry that
    timeout invites must not buy a second machine."""
    mt = await make_machine_type(real_session)
    await make_grant(real_session, org_team_id=org_admin.org_id, machine_type_id=mt.id, ceiling=4)
    provider = FakeProvider()

    first = await _create(real_session, org_admin, mt, provider, session_id="chat-1")
    again = await _create(real_session, org_admin, mt, provider, session_id="chat-1")

    assert again.id == first.id
    assert len(provider.create_calls) == 1
    assert [r.id for r in await _rows(org_admin)] == [first.id]


async def test_a_second_session_gets_its_own_machine(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The dedupe is per session, not per user: a different session is a
    different request and buys its own machine."""
    mt = await make_machine_type(real_session)
    await make_grant(real_session, org_team_id=org_admin.org_id, machine_type_id=mt.id, ceiling=4)
    provider = FakeProvider()

    first = await _create(real_session, org_admin, mt, provider, session_id="chat-1")
    second = await _create(real_session, org_admin, mt, provider, session_id="chat-2")

    assert second.id != first.id
    assert len(provider.create_calls) == 2


async def test_an_unnamed_session_is_never_deduped(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """An empty session id names nothing, so two such requests are two requests
    — they must not collapse onto one machine."""
    mt = await make_machine_type(real_session)
    await make_grant(real_session, org_team_id=org_admin.org_id, machine_type_id=mt.id, ceiling=4)
    provider = FakeProvider()

    first = await _create(real_session, org_admin, mt, provider, session_id="")
    second = await _create(real_session, org_admin, mt, provider, session_id="")

    assert second.id != first.id
    assert len(provider.create_calls) == 2


async def test_a_released_session_may_ask_for_another_machine(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """Only a LIVE machine answers the repeat: a session that gave its machine
    back is not locked out of ever getting another."""
    mt = await make_machine_type(real_session)
    await make_grant(real_session, org_team_id=org_admin.org_id, machine_type_id=mt.id, ceiling=4)
    provider = FakeProvider()

    first = await _create(real_session, org_admin, mt, provider, session_id="chat-1")
    await service.terminate_allocation(real_session, first, provider=provider)
    second = await _create(real_session, org_admin, mt, provider, session_id="chat-1")

    assert second.id != first.id
    assert len(provider.create_calls) == 2


async def test_a_machine_of_another_type_is_not_the_one_this_session_asked_for(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    mt_cpu = await make_machine_type(real_session, compute_class="cpu")
    mt_gpu = await make_machine_type(real_session, compute_class="gpu")
    await make_grant(real_session, org_team_id=org_admin.org_id, ceiling=4)
    provider = FakeProvider()

    cpu = await _create(real_session, org_admin, mt_cpu, provider, session_id="chat-1")
    gpu = await _create(real_session, org_admin, mt_gpu, provider, session_id="chat-1")

    assert gpu.id != cpu.id
    assert len(provider.create_calls) == 2


async def test_the_route_answers_a_retry_with_the_same_machine(
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Through the real route, with the ceiling that a second machine would
    breach: a retry is answered, not refused and not charged twice."""
    provider = FakeProvider()
    monkeypatch.setattr("backend.api.routes.compute.compute.get_provider", lambda _mt: provider)
    mt = await make_machine_type(real_session)
    await make_grant(real_session, org_team_id=org_admin.org_id, machine_type_id=mt.id, ceiling=1)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    body = {
        "machine_type_id": str(mt.id),
        "project_path": "/tmp/proj",
        "session_id": "chat-1",
    }

    first = await client.post("/api/v1/compute/allocations", json=body)
    second = await client.post("/api/v1/compute/allocations", json=body)

    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert second.json()["id"] == first.json()["id"]
    assert len(provider.create_calls) == 1
