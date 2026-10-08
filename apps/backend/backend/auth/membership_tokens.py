"""The one place a session, CLI, hop or chat gateway token is minted.

Every credential that reaches tenant data is minted for exactly one org
membership, and the org it names is the request's org on every door that later
reads it. So every mint goes through here: it checks that the membership
stands, applies the single-org guard, and stamps the org, the membership
(``mid``) and its credential epoch (``mep``) into the token. An architecture
gate (``apps/backend/tests/test_tenancy_architecture.py``) refuses a call to
the encoders from anywhere else.

Registering a token (``register_token``) stays with the caller, which knows
whether it is a browser session or a CLI token; a hop token is never
registered, because it never leaves this process except to the gateway.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from alkera_core.auth import (
    GatewayTokenClaims,
    SessionClaims,
    encode_cli_token,
    encode_session_token,
    mint_gateway_token,
    mint_machine_gateway_token,
)
from alkera_core.auth.tenancy import (
    MembershipRefused,
    assert_single_org,
    require_active_membership,
)
from alkera_core.authz.policies.chat import PAYER_LOST_ACCESS_CODE, PAYER_LOST_ACCESS_MESSAGE
from alkera_core.models import OrgMembership, User
from alkera_core.models.machine_credential import MachineCredential
from alkera_core.models.workspace_object import WorkspaceObject
from sqlalchemy.ext.asyncio import AsyncSession

#: What a membership token is for: a browser access token, a long-lived CLI
#: token, or the unregistered session JWT the backend sends on its own hop to
#: the gateway.
MintKind = Literal["session", "cli", "hop"]


async def mint_for_membership(
    db: AsyncSession,
    user: User,
    org_team_id: UUID,
    *,
    kind: MintKind,
) -> tuple[str, SessionClaims]:
    """Mint a ``kind`` token for ``user``'s membership in ``org_team_id``.

    Raises :class:`~alkera_core.auth.tenancy.MembershipRefused` when the
    membership is missing or not active, and, while multi-org is off, when
    ``org_team_id`` is not the user's home org. A session or CLI mint counts as
    activity in the org (``last_active_at``); a hop is the backend acting on a
    request the person already made, so it does not.
    """
    assert_single_org(user, org_team_id=org_team_id)
    membership = await require_active_membership(db, user_id=user.id, org_team_id=org_team_id)
    encode = encode_cli_token if kind == "cli" else encode_session_token
    token, claims = encode(
        user_id=user.id,
        email=user.email,
        org_team_id=org_team_id,
        platform_role=user.platform_role,
        membership_id=membership.id,
        membership_epoch=membership.credential_epoch,
    )
    if kind != "hop":
        membership.last_active_at = datetime.now(UTC)
        await db.flush()
    return token, claims


#: The refusal a chat's turns and its gateway token get when the person they
#: would bill can no longer read the chat: removed from its workspace, gone
#: from its org, or deactivated. The chat needs a new owner, or a new chat.
#: Spelled by the chat policy, whose SEND refuses it in the same words.
PAYER_LOST_ACCESS = PAYER_LOST_ACCESS_CODE


class PayerLostAccessError(ValueError):
    """The chat's payer may no longer be billed for it (:data:`PAYER_LOST_ACCESS`)."""

    code = PAYER_LOST_ACCESS

    def __init__(self) -> None:
        super().__init__(PAYER_LOST_ACCESS_MESSAGE)


def billed_user_of(chat: WorkspaceObject) -> UUID:
    """The chat's payer: whose plan and credits its model turns run on when no
    person is at the box (a pool or org box on its machine credential), and
    whose catalog a model switch is checked against. It is the chat's owner,
    the person who started it, whoever owns the workspace it sits in.

    The one place that answers it. A chat whose billing is carried by someone
    other than its owner changes this function alone. A payer who can no
    longer read the chat is never billed for it: sending is refused and so is
    the gateway token a machine would mint for them."""
    return chat.owner_user_id


async def _payer_membership(db: AsyncSession, chat: WorkspaceObject) -> OrgMembership:
    try:
        return await require_active_membership(
            db, user_id=billed_user_of(chat), org_team_id=chat.org_team_id
        )
    except MembershipRefused as exc:
        raise PayerLostAccessError from exc


async def mint_chat_gateway_token(
    db: AsyncSession,
    chat: WorkspaceObject,
    credential: MachineCredential | None = None,
    payer_reads: bool = False,
    *,
    session: SessionClaims | None = None,
) -> tuple[str, GatewayTokenClaims]:
    """A chat's gateway credential, under exactly one parent, which also
    decides who pays.

    Under a person's ``session`` (a box or device that person runs, serving
    their own chat or a teammate's) the token is the session's own: it bills
    that person in the session's org, which must be the chat's, and dies with
    the session and with their membership. The person at the box pays for
    the turns their box runs, as they always have.

    Under a machine ``credential`` (a pool or org box with no person behind
    it) the token bills the chat's payer (:func:`billed_user_of`) through
    their membership in the chat's org, and stands while the credential and
    that membership do. The payer must still read the chat: the caller
    resolves that (``payer_reads``) and a payer who cannot, or whose
    membership is gone, is refused with :class:`PayerLostAccessError` rather
    than billed for a chat they can no longer open, stop or delete.

    Raises ``ValueError`` when the token cannot be minted.
    """
    if session is not None:
        if credential is not None:
            raise ValueError("a chat's gateway token is minted under exactly one parent")
        if session.org_team_id != chat.org_team_id:
            raise ValueError("a box's session mints only for chats in its own org")
        return mint_gateway_token(
            user_id=session.user_id,
            org_team_id=session.org_team_id,
            chat_id=chat.id,
            session=session,
        )
    if credential is None:
        raise ValueError("a chat's gateway token is minted under a session or a machine credential")
    if not payer_reads:
        raise PayerLostAccessError
    membership = await _payer_membership(db, chat)
    return mint_machine_gateway_token(
        user_id=billed_user_of(chat),
        org_team_id=chat.org_team_id,
        chat_id=chat.id,
        credential_id=credential.id,
        credential_issued_at=int(credential.created_at.timestamp()),
        membership_id=membership.id,
        membership_epoch=membership.credential_epoch,
    )


__all__ = [
    "PAYER_LOST_ACCESS",
    "MintKind",
    "PayerLostAccessError",
    "billed_user_of",
    "mint_chat_gateway_token",
    "mint_for_membership",
]
