"""Whether a person may drive a chat's agent: the one send decision.

A message reaches a chat through two doors (``POST /chats/{id}/messages`` and a
``prompt`` relayed over the chat document), and the box asks once more, before
it starts the turn, whether the message's author still may (a share can be
revoked between the send and the pickup). All three are answered here, by the
chat policy's ``SEND`` over the facts resolved the same way: roles at the org
root, memberships, workspace rung and a verified email.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from alkera_core.auth.tenancy import member_stands
from alkera_core.authz import (
    ActingContext,
    Action,
    Decision,
    DecisionEvent,
    Resource,
    ResourceType,
    RoleResolver,
    audited_attrs,
    authorize,
    policy_for,
)
from alkera_core.models import User, WorkspaceObject
from alkera_core.schemas.objects import ChatSendAdmission
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.org import ancestor_chain
from backend.services.sharing import chat_attrs, object_resource, resolve_reader

#: The refusal code when the message's author is gone from the chat's org.
AUTHOR_GONE = "author_gone"


@dataclass(frozen=True, slots=True)
class SendDecision:
    """The decision and what it was made over, for a caller that files it."""

    ctx: ActingContext
    resource: Resource
    attrs: dict[str, Any]
    decision: Decision

    def event(self, method: str, path: str) -> DecisionEvent:
        """The decision as the audit row files it, with only the attributes
        the chat policy declares audited."""
        policy = policy_for(self.resource.type)
        return DecisionEvent(
            org_id=self.resource.org_id or self.ctx.org_id,
            actor=self.ctx.audit_dict(),
            action=Action.SEND,
            resource=self.resource,
            decision=self.decision,
            attrs=audited_attrs(self.attrs, policy.audited_attrs if policy else frozenset()),
            method=method,
            path=path,
        )


async def send_decision(db: AsyncSession, *, chat: WorkspaceObject, user: User) -> SendDecision:
    """Decide whether ``user`` may send in ``chat`` (``chat.access`` ``SEND``),
    acting in the chat's org (never the person's home org)."""
    ctx = ActingContext.for_user(user_id=user.id, org_id=chat.org_team_id, email=user.email)
    roles = RoleResolver(db, ctx, ancestor_chain=ancestor_chain)
    reader = await resolve_reader(db, ctx=ctx, roles=roles, user=user)
    resource = object_resource(chat, type=ResourceType.CHAT)
    attrs = dict(await chat_attrs(db, chat, reader))
    return SendDecision(ctx, resource, attrs, authorize(ctx, Action.SEND, resource, attrs))


async def turn_admission(
    db: AsyncSession, *, chat: WorkspaceObject, author_id: UUID
) -> tuple[ChatSendAdmission, SendDecision | None]:
    """Whether the message ``author_id`` sent may still start a turn in
    ``chat``, and the refused decision for the caller to file (``None`` on an
    allow, which files nothing: the send was filed when it was made, and on an
    author gone from the org, where no policy was asked)."""
    author = await db.get(User, author_id)
    if author is None or not await member_stands(db, user=author, org_team_id=chat.org_team_id):
        gone = ChatSendAdmission(
            allowed=False, code=AUTHOR_GONE, message="Its author is no longer in this org."
        )
        return gone, None
    decided = await send_decision(db, chat=chat, user=author)
    if decided.decision.allowed:
        return ChatSendAdmission(allowed=True), None
    refused = ChatSendAdmission(
        allowed=False, code=decided.decision.error_code, message=decided.decision.message
    )
    return refused, decided


__all__ = ["AUTHOR_GONE", "SendDecision", "send_decision", "turn_admission"]
