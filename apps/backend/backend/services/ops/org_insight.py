"""Reads behind the admin console's view into one org: its chats, the errors
and refusals in their transcripts, and its audit trail.

Read-only and cross-tenant by design: the callers are Alkera staff routes.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from alkera_core.models import ComputeAllocation, User, WorkspaceObject
from alkera_core.models.workspace_object import ChatMessage
from alkera_core.objects import chat_spares
from alkera_core.schemas.system.org_audit import OrgAuditEventRead
from alkera_core.schemas.system.org_insight import (
    OrgAuditList,
    OrgChatInsight,
    OrgIssue,
    OrgIssueKind,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.audit import org_audit as org_audit_service
from backend.services.chats import chat_service
from backend.services.compute.placement import chat_machine_status
from backend.utils.ids import uuid_or_none

#: The transcript kinds that can carry an error or a refusal. Read first by
#: kind (cheap), then judged one by one on the event inside.
ISSUE_KINDS: tuple[str, ...] = ("turn.finished", "session.status_changed", "permission.resolved")


async def org_chats(db: AsyncSession, *, org_id: UUID, limit: int) -> list[OrgChatInsight]:
    """The org's live chats, most recently active first. A spare (a chat warmed
    ahead of anyone's first message) is not a chat to anyone yet, so it is left
    out."""
    rows = (
        await db.execute(
            select(WorkspaceObject, User.email)
            .join(User, User.id == WorkspaceObject.owner_user_id)
            .where(
                WorkspaceObject.org_team_id == org_id,
                WorkspaceObject.type == "chat",
                WorkspaceObject.deleted_at == 0,
            )
            .order_by(WorkspaceObject.updated_at.desc())
            .limit(limit * 2)
        )
    ).all()
    chats = [(chat, email) for chat, email in rows if not chat_spares.is_spare(chat)]
    specs = {chat.id: chat_service.chat_spec_of(chat) for chat, _ in chats}
    machine_ids = {m for m in (uuid_or_none(spec.machine_id) for spec in specs.values()) if m}
    machines: dict[UUID, ComputeAllocation] = {}
    if machine_ids:
        found = await db.execute(
            select(ComputeAllocation).where(ComputeAllocation.id.in_(machine_ids))
        )
        machines = {row.id: row for row in found.scalars()}
    activity = await chat_service.chat_activity(db, [chat.id for chat, _ in chats])
    items = []
    for chat, email in chats:
        spec = specs[chat.id]
        bound = uuid_or_none(spec.machine_id)
        machine = machines.get(bound) if bound is not None else None
        seen = activity.get(chat.id)
        items.append(
            OrgChatInsight(
                id=chat.id,
                title=chat.title,
                owner_email=email,
                machine_id=spec.machine_id,
                machine_name=(machine.name or None) if machine is not None else None,
                machine_status=chat_machine_status(spec, machine),
                mirror_state=spec.mirror_state,
                machine_refusal=spec.publisher_refusal or None,
                last_activity_at=seen.last_activity_at if seen is not None else None,
                created_at=chat.created_at,
            )
        )
    items.sort(
        key=lambda item: item.last_activity_at or item.created_at,
        reverse=True,
    )
    return items[:limit]


def classify(kind: str, event: dict[str, Any]) -> tuple[OrgIssueKind, str] | None:
    """Whether a transcript event is an error or a refusal, and what it says.
    ``None`` for an event that went fine."""
    if kind == "turn.finished":
        reason = event.get("stop_reason")
        if reason == "error":
            return "error", str(event.get("error_detail") or "The turn ended in an error")
        if reason == "refusal":
            return "refusal", str(event.get("error_detail") or "The model refused")
        return None
    if kind == "session.status_changed":
        if event.get("status") == "error":
            return "error", str(event.get("detail") or "The session reported an error")
        return None
    if kind == "permission.resolved":
        option = event.get("option_id")
        if isinstance(option, str) and option.startswith("reject"):
            by = event.get("decided_by") or "user"
            return "denied", f"Permission rejected by {by}"
        return None
    return None


async def org_issues(
    db: AsyncSession, *, org_id: UUID, since: datetime, limit: int
) -> list[OrgIssue]:
    """The org's most recent errors and refusals since ``since``, newest first:
    transcript entries judged by :func:`classify`, plus every chat whose
    machine currently refuses to publish it."""
    scan = (
        await db.execute(
            select(ChatMessage, WorkspaceObject.title)
            .join(WorkspaceObject, WorkspaceObject.id == ChatMessage.chat_id)
            .where(
                ChatMessage.org_team_id == org_id,
                ChatMessage.kind.in_(ISSUE_KINDS),
                ChatMessage.created_at >= since,
            )
            .order_by(ChatMessage.created_at.desc())
            # Most scanned rows are uneventful turn ends; read a bounded window
            # wide enough to find ``limit`` issues in the usual mix.
            .limit(limit * 20)
        )
    ).all()
    issues: list[OrgIssue] = []
    for message, title in scan:
        judged = classify(message.kind, chat_service.event_payload(message.payload))
        if judged is None:
            continue
        kind, detail = judged
        issues.append(
            OrgIssue(
                kind=kind,
                chat_id=message.chat_id,
                chat_title=title,
                detail=detail,
                at=message.created_at,
            )
        )
    refused: Sequence[WorkspaceObject] = (
        (
            await db.execute(
                select(WorkspaceObject).where(
                    WorkspaceObject.org_team_id == org_id,
                    WorkspaceObject.type == "chat",
                    WorkspaceObject.deleted_at == 0,
                    WorkspaceObject.spec["publisher_refusal"].astext != "",
                    WorkspaceObject.updated_at >= since,
                )
            )
        )
        .scalars()
        .all()
    )
    for chat in refused:
        reason = chat_service.chat_spec_of(chat).publisher_refusal
        if reason:
            issues.append(
                OrgIssue(
                    kind="machine_refused",
                    chat_id=chat.id,
                    chat_title=chat.title,
                    detail=reason,
                    at=chat.updated_at,
                )
            )
    issues.sort(key=lambda issue: issue.at, reverse=True)
    return issues[:limit]


async def org_audit(db: AsyncSession, *, org_id: UUID, limit: int) -> OrgAuditList:
    rows, total = await org_audit_service.list_page(db, org_id=org_id, offset=0, limit=limit)
    return OrgAuditList(
        items=[
            OrgAuditEventRead(
                id=row.id,
                actor_email=row.actor_email,
                action=row.action,
                target=row.target,
                detail=row.detail,
                created_at=row.created_at,
            )
            for row in rows
        ],
        total=total,
    )


def default_since(now: datetime | None = None) -> datetime:
    """A week back from ``now``: the window the activity tab opens on."""
    return (now or datetime.now(UTC)) - timedelta(days=7)
