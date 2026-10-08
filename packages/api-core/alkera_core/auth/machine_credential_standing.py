"""Whether a box's machine credential still stands.

The one question two surfaces ask about a credential that is not on the
request: the model gateway, deciding whether a chat token minted under it is
still good, and any door that holds a reference to a credential rather than
the secret — the socket a box opened on a ticket, the event stream it holds.
It is answered here, in the core, so the gateway — which depends on nothing of
the backend — and the backend read the same rule: the row exists, it is
unrevoked, and the machine it holds is not gone. An unclaimed credential holds
no machine and so stands behind nothing. A personal box's credential stands
only while the person it was minted for is still an active, unbanned member of
the org it is bound to: leaving the org, or a ban, ends it at every door at
once. (Logout-all, a password reset and a ban also revoke the row itself, in
``alkera_core.auth.revocation``; this read is the line that holds while a ban
stands even for a row that predates that.)
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import ColumnElement, SQLColumnExpression, Uuid, and_, literal, select
from sqlalchemy.ext.asyncio import AsyncSession

from alkera_core.bans import banned_predicate
from alkera_core.models._enums import MembershipStatus
from alkera_core.models.compute import (
    COMPUTE_TERMINAL_STATES,
    PERSONAL_TENANCY,
    ComputeAllocation,
)
from alkera_core.models.machine_credential import MachineCredential
from alkera_core.models.org_membership import OrgMembership
from alkera_core.models.team_membership import TeamMembership
from alkera_core.models.user import User
from alkera_core.models.workspace_object import WorkspaceObject
from alkera_core.schemas.objects import ChatSpec


def owner_stands_clause(
    user_id: SQLColumnExpression[Any], org_id: SQLColumnExpression[Any]
) -> ColumnElement[bool]:
    """THE rule for whether a personal box's person still stands behind it, as
    SQL over the two columns that name them: the account is active and not
    banned, holds an active membership in that org (the person may belong to
    several) and its membership on the org root. Every door that reads a
    personal credential and every placement read asks this one clause."""
    active = (
        select(User.id)
        .join(
            OrgMembership,
            (OrgMembership.user_id == User.id)
            & (OrgMembership.org_team_id == org_id)
            & (OrgMembership.status == MembershipStatus.ACTIVE),
        )
        .where(User.id == user_id, User.is_active.is_(True), ~banned_predicate())
        .exists()
    )
    member = (
        select(TeamMembership.id)
        .where(
            TeamMembership.user_id == user_id,
            TeamMembership.org_team_id == org_id,
            TeamMembership.team_id == org_id,
        )
        .exists()
    )
    return and_(active, member)


async def owner_stands(db: AsyncSession, *, user_id: UUID | None, org_id: UUID) -> bool:
    """:func:`owner_stands_clause` for one person and org. ``False`` for
    nobody (a personal credential whose person was deleted)."""
    if user_id is None:
        return False
    clause = owner_stands_clause(literal(user_id, Uuid), literal(org_id, Uuid))
    return bool((await db.execute(select(clause))).scalar())


async def chat_is_bound_to(
    session: AsyncSession, chat_id: str, machine_id: str, *, org_id: UUID | None = None
) -> bool:
    """Whether ``chat_id`` is a live chat bound to ``machine_id`` (and, when
    ``org_id`` is named, one in that org): the one fact a box holds a chat by,
    asked by every door that lets a box act for a chat it names."""
    try:
        parsed = UUID(chat_id)
    except ValueError:
        return False
    stmt = select(WorkspaceObject.spec).where(
        WorkspaceObject.id == parsed,
        WorkspaceObject.type == "chat",
        WorkspaceObject.deleted_at == 0,
    )
    if org_id is not None:
        stmt = stmt.where(WorkspaceObject.org_team_id == org_id)
    spec = (await session.execute(stmt)).scalar_one_or_none()
    if spec is None:
        return False
    return ChatSpec.model_validate(spec or {}).machine_id == machine_id


async def live_machine_of(db: AsyncSession, credential_id: str | UUID) -> UUID | None:
    """The machine ``credential_id`` stands behind, or ``None``.

    ``None`` for a credential that is not there, is revoked, has claimed no
    machine, or whose machine is in a terminal state — one answer for every
    way the standing can be gone, so a caller cannot tell them apart.
    ``credential_id`` may arrive as the hex a token carries; anything that is
    not an id is no credential. One indexed read of the credential and its
    machine's state, and for a personal box one more of its owner's standing.
    """
    try:
        ident = credential_id if isinstance(credential_id, UUID) else UUID(hex=credential_id)
    except ValueError:
        return None
    row = (
        await db.execute(
            select(
                MachineCredential.revoked_at,
                MachineCredential.machine_id,
                ComputeAllocation.state,
                MachineCredential.tenancy,
                MachineCredential.created_by,
                MachineCredential.org_team_id,
            )
            .outerjoin(ComputeAllocation, ComputeAllocation.id == MachineCredential.machine_id)
            .where(MachineCredential.id == ident)
        )
    ).one_or_none()
    if row is None:
        return None
    revoked_at, machine_id, state, tenancy, created_by, org_id = row
    if revoked_at is not None or machine_id is None:
        return None
    if state is None or state in COMPUTE_TERMINAL_STATES:
        return None
    if tenancy == PERSONAL_TENANCY and not await owner_stands(
        db, user_id=created_by, org_id=org_id
    ):
        return None
    return UUID(str(machine_id))


async def machine_credential_live(db: AsyncSession, credential_id: str | UUID) -> bool:
    """``True`` while the credential is unrevoked and its machine is live."""
    return await live_machine_of(db, credential_id) is not None


__all__ = [
    "chat_is_bound_to",
    "live_machine_of",
    "machine_credential_live",
    "owner_stands",
    "owner_stands_clause",
]
