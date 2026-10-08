"""What happens to an org's machines when people, teams and orgs go away.

A person who leaves the org (removed or deactivated) is taken out of every
machine's audience, and nobody else is; a deleted team hands the machines it
held to the team above it, on record; a deleted org deletes every machine it
holds before its rows go, so each provider machine is released.
"""

from __future__ import annotations

import pytest
from alkera_core.compute.provider import GONE, RUNNING, NodeDescription
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import EventOutbox, OrgAuditEvent, OrgMembership
from alkera_core.models.org_machines import OrgMachine, OrgMachineAudience
from backend.services.compute import provisioning
from backend.services.org import org_memberships, removal_hooks
from backend.services.org import teams as team_service
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tests._org_machine_helpers import make_org_machine, make_team
from tests.conftest import OrgWithAdmin, make_member

pytestmark = [pytest.mark.asyncio, pytest.mark.compute_rows]


class _Provider:
    """A provider that terminates what it is asked to and then reports it
    gone, keeping the ids it was asked to terminate."""

    def __init__(self) -> None:
        self.terminated: list[str] = []

    async def terminate(self, machine_id: str) -> None:
        self.terminated.append(machine_id)

    async def describe(self, machine_id: str) -> NodeDescription:
        return NodeDescription(
            machine_id=machine_id, phase=GONE if machine_id in self.terminated else RUNNING
        )

    async def delete_credential(self, allocation_id: object) -> None:
        return None


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch) -> _Provider:
    fake = _Provider()
    monkeypatch.setattr(provisioning, "make_node_provider", lambda _kind, _config: fake)
    return fake


async def _grants(machine_id: object) -> list[tuple[str, object, object]]:
    async with AsyncSessionLocal() as session:
        rows = await session.execute(
            select(OrgMachineAudience).where(OrgMachineAudience.org_machine_id == machine_id)
        )
        return sorted(((g.grantee_kind, g.team_id, g.user_id) for g in rows.scalars()), key=str)


async def _membership(session: AsyncSession, org: OrgWithAdmin, user_id: object) -> OrgMembership:
    row = (
        await session.execute(
            select(OrgMembership).where(
                OrgMembership.org_team_id == org.org_id, OrgMembership.user_id == user_id
            )
        )
    ).scalar_one()
    return row


@pytest.mark.parametrize("how", ["remove", "deactivate"])
async def test_a_person_leaving_the_org_leaves_every_machines_audience(
    real_session: AsyncSession, org_admin: OrgWithAdmin, how: str
) -> None:
    leaving, _ = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    staying, _ = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    team = await make_team(real_session, org_id=org_admin.org_id)
    first, _ = await make_org_machine(
        real_session,
        org_id=org_admin.org_id,
        operator_id=org_admin.admin_id,
        audience=[("user", leaving.id), ("user", staying.id), ("team", team.id)],
    )
    second, _ = await make_org_machine(
        real_session,
        org_id=org_admin.org_id,
        operator_id=org_admin.admin_id,
        audience=[("user", leaving.id)],
    )
    membership = await _membership(real_session, org_admin, leaving.id)
    if how == "remove":
        await org_memberships.remove(real_session, membership, actor=None)
    else:
        await org_memberships.deactivate(real_session, membership, actor=None)
    await real_session.commit()
    assert await _grants(first.id) == sorted(
        [("team", team.id, None), ("user", None, staying.id)], key=str
    )
    assert await _grants(second.id) == []


async def test_a_deleted_team_hands_its_machines_to_the_team_above(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    parent = await make_team(real_session, org_id=org_admin.org_id)
    child = await team_service.create_subteam(
        real_session, org_team_id=org_admin.org_id, name="Child", parent_team_id=parent.id
    )
    await real_session.commit()
    held, _ = await make_org_machine(
        real_session,
        org_id=org_admin.org_id,
        operator_id=org_admin.admin_id,
        owner_team_id=child.id,
        audience=[("team", child.id), ("org",)],
    )
    elsewhere, _ = await make_org_machine(
        real_session,
        org_id=org_admin.org_id,
        operator_id=org_admin.admin_id,
        owner_team_id=parent.id,
    )
    version_before = held.version
    await team_service.delete_team(real_session, child)
    await real_session.commit()
    async with AsyncSessionLocal() as session:
        moved = await session.get(OrgMachine, held.id)
        kept = await session.get(OrgMachine, elsewhere.id)
        assert moved is not None and moved.owner_team_id == parent.id
        assert moved.version == version_before + 1
        assert kept is not None and kept.owner_team_id == parent.id
        audit = (
            (
                await session.execute(
                    select(OrgAuditEvent).where(
                        OrgAuditEvent.org_team_id == org_admin.org_id,
                        OrgAuditEvent.action == "machine.owner_moved",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert [(row.target, row.detail) for row in audit] == [
            (str(held.id), {"from_team_id": str(child.id), "to_team_id": str(parent.id)})
        ]
    assert await _grants(held.id) == [("org", None, None)]


async def test_the_org_deletion_hook_deletes_every_machine_the_org_holds(
    real_session: AsyncSession, org_admin: OrgWithAdmin, provider: _Provider
) -> None:
    """The hook delete_org runs before its purge: each provider machine is
    terminated at its provider, and each live machine is deleted on record."""
    one, one_alloc = await make_org_machine(
        real_session, org_id=org_admin.org_id, operator_id=org_admin.admin_id
    )
    two, two_alloc = await make_org_machine(
        real_session, org_id=org_admin.org_id, operator_id=org_admin.admin_id, look="stopped"
    )
    await removal_hooks.org_deleted(real_session, org_id=org_admin.org_id)
    await real_session.commit()
    pods = {alloc.provider_machine_id for alloc in (one_alloc, two_alloc)}
    assert set(provider.terminated) == pods
    async with AsyncSessionLocal() as session:
        for machine in (one, two):
            gone = await session.get(OrgMachine, machine.id)
            assert gone is not None
            assert (gone.deleted_at is not None, gone.desired_power) == (True, "off")


async def test_deleting_an_org_announces_each_machine_deleted_before_its_rows_go(
    real_session: AsyncSession, org_admin: OrgWithAdmin, provider: _Provider
) -> None:
    machine, _ = await make_org_machine(
        real_session, org_id=org_admin.org_id, operator_id=org_admin.admin_id
    )
    org = await team_service.get_by_id(real_session, org_admin.org_id)
    assert org is not None
    await team_service.delete_org(real_session, org)
    await real_session.commit()
    async with AsyncSessionLocal() as session:
        assert await session.get(OrgMachine, machine.id) is None
        frames = (
            (
                await session.execute(
                    select(EventOutbox.payload).where(
                        EventOutbox.type == "org_machine.changed",
                        EventOutbox.entity_id == str(machine.id),
                    )
                )
            )
            .scalars()
            .all()
        )
    assert any((frame or {}).get("state") == "deleted" for frame in frames)


def test_the_org_machine_hooks_are_registered_for_all_three_moments() -> None:
    assert removal_hooks.registered() == {
        "member_left": ("org_machines.audience",),
        "team_deleted": ("org_machines.owner",),
        "org_deleted": ("org_machines.machines",),
    }


async def test_a_hook_runs_inside_the_callers_transaction(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """A removal that rolls back leaves the audience as it was."""
    person, _ = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    machine, _ = await make_org_machine(
        real_session,
        org_id=org_admin.org_id,
        operator_id=org_admin.admin_id,
        audience=[("user", person.id)],
    )
    machine_id, person_id = machine.id, person.id
    await removal_hooks.member_left(real_session, org_id=org_admin.org_id, user_id=person_id)
    await real_session.rollback()
    assert await _grants(machine_id) == [("user", None, person_id)]


@pytest.mark.parametrize(
    "moment",
    [
        pytest.param("member_left", id="member-left"),
        pytest.param("team_deleted", id="team-deleted"),
        pytest.param("org_deleted", id="org-deleted"),
    ],
)
async def test_a_moment_missing_a_required_hook_is_refused_before_any_hook_runs(
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    monkeypatch: pytest.MonkeyPatch,
    provider: _Provider,
    moment: str,
) -> None:
    """A process that never imported a resource's hooks must not delete a
    person, team or org and leave that resource behind: the moment refuses,
    and the hooks it does hold have not run."""
    person, _ = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    team = await make_team(real_session, org_id=org_admin.org_id)
    machine, _ = await make_org_machine(
        real_session,
        org_id=org_admin.org_id,
        operator_id=org_admin.admin_id,
        owner_team_id=team.id,
        audience=[("user", person.id)],
    )
    machine_id, person_id = machine.id, person.id
    await real_session.commit()
    monkeypatch.setitem(
        removal_hooks.REQUIRED, moment, removal_hooks.REQUIRED[moment] | {"never.registered"}
    )
    with pytest.raises(removal_hooks.HooksMissingError, match=r"never\.registered"):
        if moment == "member_left":
            await removal_hooks.member_left(
                real_session, org_id=org_admin.org_id, user_id=person_id
            )
        elif moment == "team_deleted":
            await removal_hooks.team_deleted(
                real_session,
                org_id=org_admin.org_id,
                team_id=team.id,
                parent_team_id=org_admin.org_id,
            )
        else:
            await removal_hooks.org_deleted(real_session, org_id=org_admin.org_id)
    await real_session.commit()
    assert await _grants(machine_id) == [("user", None, person_id)]
    assert provider.terminated == []
    async with AsyncSessionLocal() as session:
        kept = await session.get(OrgMachine, machine_id)
        assert kept is not None
        assert (kept.deleted_at, kept.owner_team_id) == (None, team.id)
