"""Every mutation the portal watches announces itself on the event outbox.

One case per producer, driven through the real service (and, for the actor
provenance, the real route): the mutation leaves exactly the expected rows —
type, entity, entity id, version, visibility, payload and actor — and the
same mutation rolled back leaves none, because the announcement rides the
caller's transaction. The no-op branches (a role that did not change, an
activation that was already set) are cases too: they must announce nothing.

The actor is the acting principal's persisted record wherever a request
principal exists — the admin's own session on the member routes — and a
named system actor only where nobody in particular acted (the dev seed, the
identity provider). The rig the tables run on is ``apps/backend/tests/_outbox_cases.py``;
a product domain with emit points of its own keeps its table beside its code.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from typing import Any
from uuid import UUID

import pytest
import pytest_asyncio
from alkera_core.authz.principal import ActingContext
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.events import EventType, actor_for_user, actor_system
from alkera_core.models import TeamRole, User
from backend.services.identity import email_verification as email_verification_service
from backend.services.identity import users as user_service
from backend.services.org import invitations as invitation_service
from backend.services.org import memberships as membership_service
from backend.services.org import teams as team_service
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from tests._outbox_cases import (
    Case,
    Row,
    World,
    assert_rolled_back,
    assert_rows,
    build_world,
    by_caller,
    by_nobody,
    get_user,
    head_id,
    row,
    rows_after,
)
from tests.conftest import OrgWithAdmin, login, make_member

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _files_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """The tables below name what each service emits for itself.

    Files is a bridge on top of some of them — joining an org also creates the
    member's home folder, which announces a ``file_node.changed`` of its own —
    so whether a case leaves one row or two would otherwise depend on the
    operator's ``FILES_ENABLED``. Pinned off here so the tables mean the
    service's own emits on every machine; the bridge's extra row is asserted
    explicitly in ``test_a_join_with_files_on_also_announces_the_home_folder``.
    """
    monkeypatch.setattr(settings, "files_enabled", False)


@pytest_asyncio.fixture
async def world(
    org_admin: OrgWithAdmin, real_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> World:
    return await build_world(org_admin, real_session, monkeypatch)


# --- email verification ---------------------------------------------------------------------


def _verified_row(user: User, *, actor: dict[str, Any]) -> Row:
    return row(
        EventType.USER_EMAIL_VERIFIED,
        "user",
        str(user.id),
        visibility=f"user:{user.id}",
        actor=actor,
    )


class EmailMarkedVerified(Case):
    def __init__(self, *, by_user: bool, label: str) -> None:
        self.id = f"user.email_verified-mark-{label}"
        self._by_user = by_user

    async def mutate(self, db: AsyncSession, w: World) -> list[Row]:
        member = await get_user(db, w.member_id)
        await email_verification_service.mark_verified(db, member, by_user=self._by_user)
        actor = (
            actor_for_user(member, org_id=w.org_id)
            if self._by_user
            else actor_system(email_verification_service.SYSTEM_ACTOR)
        )
        return [_verified_row(member, actor=actor)]


class EmailTokenConsumed(Case):
    id = "user.email_verified-consume-token-records-the-holder"

    async def prepare(self, db: AsyncSession, w: World) -> None:
        fresh, _password = await make_member(db, org_id=w.org_id, verified=False)
        w.state["user_id"] = fresh.id
        w.state["token"] = await email_verification_service.issue_token(db, fresh)

    async def mutate(self, db: AsyncSession, w: World) -> list[Row]:
        user = await email_verification_service.consume_token(db, w.state["token"])
        assert user.id == w.state["user_id"] and user.email_verified_at is not None
        return [_verified_row(user, actor=actor_for_user(user, org_id=user.home_org_team_id))]


# --- memberships ----------------------------------------------------------------------------


def _membership_row(
    team_id: UUID,
    user_id: UUID,
    *,
    role: TeamRole,
    removed: bool,
    actor: dict[str, Any],
) -> Row:
    return row(
        EventType.MEMBERSHIP_CHANGED,
        "membership",
        f"{team_id}:{user_id}",
        payload={
            "team_id": str(team_id),
            "user_id": str(user_id),
            "role": role.value,
            "removed": removed,
        },
        actor=actor,
    )


class MemberAdded(Case):
    def __init__(self, *, actor: Callable[[World], dict[str, Any] | None], label: str) -> None:
        self.id = f"membership.changed-add-{label}"
        self._actor = actor

    async def mutate(self, db: AsyncSession, w: World) -> list[Row]:
        actor = self._actor(w)
        await membership_service.add_member(
            db, team_id=w.subteam_id, user_id=w.member_id, role=TeamRole.MEMBER, actor=actor
        )
        return [
            _membership_row(
                w.subteam_id,
                w.member_id,
                role=TeamRole.MEMBER,
                removed=False,
                actor=actor or actor_system(membership_service.SYSTEM_ACTOR),
            )
        ]


class _WithMembership(Case):
    async def prepare(self, db: AsyncSession, w: World) -> None:
        await membership_service.add_member(
            db, team_id=w.subteam_id, user_id=w.member_id, role=TeamRole.MEMBER, actor=w.admin_ctx
        )

    async def _membership(self, db: AsyncSession, w: World) -> Any:
        membership = await membership_service.get(db, team_id=w.subteam_id, user_id=w.member_id)
        assert membership is not None
        return membership


class RoleChanged(_WithMembership):
    id = "membership.changed-change-role"

    async def mutate(self, db: AsyncSession, w: World) -> list[Row]:
        await membership_service.change_role(
            db, await self._membership(db, w), TeamRole.ADMIN, actor=w.admin_ctx
        )
        return [
            _membership_row(
                w.subteam_id, w.member_id, role=TeamRole.ADMIN, removed=False, actor=w.admin_ctx
            )
        ]


class RoleUnchanged(_WithMembership):
    id = "membership.changed-same-role-is-silent"

    async def mutate(self, db: AsyncSession, w: World) -> list[Row]:
        await membership_service.change_role(
            db, await self._membership(db, w), TeamRole.MEMBER, actor=w.admin_ctx
        )
        return []


class MemberRemoved(_WithMembership):
    id = "membership.changed-remove"

    async def mutate(self, db: AsyncSession, w: World) -> list[Row]:
        await membership_service.remove_member(db, await self._membership(db, w), actor=w.admin_ctx)
        return [
            _membership_row(
                w.subteam_id, w.member_id, role=TeamRole.MEMBER, removed=True, actor=w.admin_ctx
            )
        ]


class MemberMoved(_WithMembership):
    id = "membership.changed-move-is-a-removal-then-an-addition"

    async def prepare(self, db: AsyncSession, w: World) -> None:
        await super().prepare(db, w)
        other = await team_service.create_subteam(
            db, org_team_id=w.org_id, name=f"other-{secrets.token_hex(3)}"
        )
        w.state["other_id"] = other.id

    async def mutate(self, db: AsyncSession, w: World) -> list[Row]:
        await membership_service.move_member(
            db,
            user_id=w.member_id,
            from_team_id=w.subteam_id,
            to_team_id=w.state["other_id"],
            actor=w.admin_ctx,
        )
        return [
            _membership_row(
                w.subteam_id, w.member_id, role=TeamRole.MEMBER, removed=True, actor=w.admin_ctx
            ),
            _membership_row(
                w.state["other_id"],
                w.member_id,
                role=TeamRole.MEMBER,
                removed=False,
                actor=w.admin_ctx,
            ),
        ]


class MemberDeactivated(Case):
    def __init__(self, *, actor: Callable[[World], dict[str, Any] | None], label: str) -> None:
        self.id = f"membership.changed-deactivate-{label}"
        self._actor = actor

    async def mutate(self, db: AsyncSession, w: World) -> list[Row]:
        member = await get_user(db, w.member_id)
        actor = self._actor(w)
        await user_service.set_active(db, member, False, actor=actor)
        assert member.is_active is False
        return [
            row(
                EventType.MEMBERSHIP_CHANGED,
                "membership",
                f"{w.org_id}:{w.member_id}",
                payload={"team_id": str(w.org_id), "user_id": str(w.member_id), "active": False},
                actor=actor or actor_system(user_service.SYSTEM_ACTOR),
            )
        ]


class MemberActivationUnchanged(Case):
    id = "membership.changed-already-active-is-silent"

    async def mutate(self, db: AsyncSession, w: World) -> list[Row]:
        member = await get_user(db, w.member_id)
        await user_service.set_active(db, member, True, actor=w.admin_ctx)
        return []


# --- invitations ----------------------------------------------------------------------------


def _invitation_row(invitation: Any, *, team_id: UUID, status: str, actor: dict[str, Any]) -> Row:
    return row(
        EventType.INVITATION_CHANGED,
        "invitation",
        str(invitation.id),
        payload={"team_id": str(team_id), "status": status},
        actor=actor,
    )


async def _invite(
    db: AsyncSession, w: World, *, email: str, actor: dict[str, Any] | None
) -> tuple[Any, bool]:
    team = await team_service.get_by_id(db, w.subteam_id)
    admin = await get_user(db, w.admin_id)
    assert team is not None
    invitation, auto_accepted, _token = await invitation_service.create_invitation(
        db,
        team=team,
        email=email,
        role=TeamRole.MEMBER,
        invited_by=admin,
        org_team_id=w.org_id,
        actor=actor,
    )
    return invitation, auto_accepted


def _fresh_email() -> str:
    return f"invitee-{secrets.token_hex(5)}@alkera.dev"


class InvitationCreated(Case):
    def __init__(self, *, actor: Callable[[World], dict[str, Any] | None], label: str) -> None:
        self.id = f"invitation.changed-create-{label}"
        self._actor = actor

    async def mutate(self, db: AsyncSession, w: World) -> list[Row]:
        actor = self._actor(w)
        invitation, auto_accepted = await _invite(db, w, email=_fresh_email(), actor=actor)
        assert not auto_accepted
        return [
            _invitation_row(
                invitation,
                team_id=w.subteam_id,
                status="pending",
                actor=actor or actor_system(invitation_service.SYSTEM_ACTOR),
            )
        ]


class InvitationAutoAccepted(Case):
    id = "invitation.changed-in-org-address-is-seated-and-accepted"

    async def mutate(self, db: AsyncSession, w: World) -> list[Row]:
        invitation, auto_accepted = await _invite(db, w, email=w.member_email, actor=w.admin_ctx)
        assert auto_accepted
        return [
            _membership_row(
                w.subteam_id, w.member_id, role=TeamRole.MEMBER, removed=False, actor=w.admin_ctx
            ),
            _invitation_row(invitation, team_id=w.subteam_id, status="accepted", actor=w.admin_ctx),
        ]


class _WithPendingInvitation(Case):
    async def prepare(self, db: AsyncSession, w: World) -> None:
        email = _fresh_email()
        invitation, _auto = await _invite(db, w, email=email, actor=w.admin_ctx)
        w.state["invitation_id"], w.state["email"] = invitation.id, email

    async def _invitation(self, db: AsyncSession, w: World) -> Any:
        invitation = await invitation_service.get_by_id(db, w.state["invitation_id"])
        assert invitation is not None
        return invitation


class InvitationAccepted(_WithPendingInvitation):
    id = "invitation.changed-accept-seats-the-recipient"

    async def prepare(self, db: AsyncSession, w: World) -> None:
        await super().prepare(db, w)
        recipient, _password = await make_member(
            db, org_id=w.org_id, email=w.state["email"], verified=True
        )
        w.state["recipient_id"] = recipient.id

    async def mutate(self, db: AsyncSession, w: World) -> list[Row]:
        recipient = await get_user(db, w.state["recipient_id"])
        invitation = await self._invitation(db, w)
        recipient_ctx = ActingContext.for_user(
            user_id=recipient.id, org_id=recipient.home_org_team_id, email=recipient.email
        ).audit_dict()
        joined = await invitation_service.accept_invitation(
            db, invitation, user=recipient, actor=recipient_ctx
        )
        assert w.subteam_id in joined
        return [
            _membership_row(
                w.subteam_id, recipient.id, role=TeamRole.MEMBER, removed=False, actor=recipient_ctx
            ),
            _invitation_row(
                invitation, team_id=w.subteam_id, status="accepted", actor=recipient_ctx
            ),
        ]


class InvitationRejected(_WithPendingInvitation):
    id = "invitation.changed-reject"

    async def mutate(self, db: AsyncSession, w: World) -> list[Row]:
        invitation = await self._invitation(db, w)
        await invitation_service.reject_invitation(db, invitation, actor=w.admin_ctx)
        return [
            _invitation_row(invitation, team_id=w.subteam_id, status="rejected", actor=w.admin_ctx)
        ]


class InvitationRevoked(_WithPendingInvitation):
    id = "invitation.changed-revoke"

    async def mutate(self, db: AsyncSession, w: World) -> list[Row]:
        invitation = await self._invitation(db, w)
        await invitation_service.revoke_invitation(db, invitation, actor=w.admin_ctx)
        return [
            _invitation_row(invitation, team_id=w.subteam_id, status="revoked", actor=w.admin_ctx)
        ]


class InvitationsRevokedByInviter(Case):
    id = "invitation.changed-revoke-every-pending-row-of-an-inviter"
    unordered = True

    async def prepare(self, db: AsyncSession, w: World) -> None:
        first, _ = await _invite(db, w, email=_fresh_email(), actor=w.admin_ctx)
        second, _ = await _invite(db, w, email=_fresh_email(), actor=w.admin_ctx)
        w.state["ids"] = [first.id, second.id]

    async def mutate(self, db: AsyncSession, w: World) -> list[Row]:
        count = await invitation_service.revoke_pending_by_inviter(
            db, w.admin_id, actor=w.admin_ctx
        )
        assert count == 2
        return [
            row(
                EventType.INVITATION_CHANGED,
                "invitation",
                str(invitation_id),
                payload={"team_id": str(w.subteam_id), "status": "revoked"},
                actor=w.admin_ctx,
            )
            for invitation_id in w.state["ids"]
        ]


# ---------------------------------------------------------------------------
# The table
# ---------------------------------------------------------------------------


CASES: list[Case] = [
    EmailMarkedVerified(by_user=True, label="records-the-person"),
    EmailMarkedVerified(by_user=False, label="defaults-to-the-system-actor"),
    EmailTokenConsumed(),
    MemberAdded(actor=by_caller, label="records-the-caller"),
    MemberAdded(actor=by_nobody, label="defaults-to-the-system-actor"),
    RoleChanged(),
    RoleUnchanged(),
    MemberRemoved(),
    MemberMoved(),
    MemberDeactivated(actor=by_caller, label="records-the-caller"),
    MemberDeactivated(actor=by_nobody, label="defaults-to-the-system-actor"),
    MemberActivationUnchanged(),
    InvitationCreated(actor=by_caller, label="records-the-caller"),
    InvitationCreated(actor=by_nobody, label="defaults-to-the-system-actor"),
    InvitationAutoAccepted(),
    InvitationAccepted(),
    InvitationRejected(),
    InvitationRevoked(),
    InvitationsRevokedByInviter(),
]


PARAMS = [pytest.param(case, id=case.id) for case in CASES]


@pytest.mark.parametrize("case", PARAMS)
async def test_the_mutation_leaves_exactly_the_expected_rows(case: Case, world: World) -> None:
    await assert_rows(case, world)


@pytest.mark.parametrize("case", PARAMS)
async def test_a_rolled_back_mutation_leaves_none(case: Case, world: World) -> None:
    await assert_rolled_back(case, world)


async def test_a_join_with_files_on_also_announces_the_home_folder(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With Files enabled a join is two announcements, not one: the membership
    the service records, and the home folder the Files bridge creates so a
    grant made before the member ever opens Files has somewhere to land. Both
    are org-visible; the node row carries ids only, never a name."""
    monkeypatch.setattr(settings, "files_enabled", True)
    head = await head_id(world.org_id)
    async with AsyncSessionLocal() as db:
        await membership_service.add_member(
            db,
            team_id=world.subteam_id,
            user_id=world.member_id,
            role=TeamRole.MEMBER,
            actor=world.admin_ctx,
        )
        await db.commit()
    rows = await rows_after(world.org_id, head)
    memberships = [r for r in rows if r[0] == EventType.MEMBERSHIP_CHANGED.value]
    nodes = [r for r in rows if r[0] == EventType.FILE_NODE_CHANGED.value]
    assert {r[0] for r in rows} == {
        EventType.MEMBERSHIP_CHANGED.value,
        EventType.FILE_NODE_CHANGED.value,
    }
    assert len(memberships) == 1
    assert memberships[0][5] == {
        "team_id": str(world.subteam_id),
        "user_id": str(world.member_id),
        "role": TeamRole.MEMBER.value,
        "removed": False,
    }
    # The bridge lands the home folder (and any drive or team folder it had to
    # create on the way), each announced as a node change carrying ids only.
    assert nodes, "a join with Files on lands the member's home folder"
    assert all(r[1] == "file_node" and r[4] == "org" for r in nodes)
    assert all("name" not in r[5] for r in nodes)


# ---------------------------------------------------------------------------
# The actor is the request principal, recorded through the real routes
# ---------------------------------------------------------------------------


async def test_the_member_activation_route_records_the_admin(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: AsyncSession
) -> None:
    member, _password = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    head = await head_id(org_admin.org_id)
    resp = await client.put(f"/api/v1/org/members/{member.id}/active", json={"active": False})
    assert resp.status_code == 200, resp.text
    rows = await rows_after(org_admin.org_id, head)
    assert [(r[0], r[5]["active"]) for r in rows] == [(EventType.MEMBERSHIP_CHANGED.value, False)]
    assert rows[0][6]["acting"]["id"] == str(org_admin.admin_id)
    assert rows[0][6]["acting"]["credential"] == "jwt"
    # The same call again changes nothing and announces nothing.
    again = await client.put(f"/api/v1/org/members/{member.id}/active", json={"active": False})
    assert again.status_code == 200, again.text
    assert len(await rows_after(org_admin.org_id, head)) == 1
