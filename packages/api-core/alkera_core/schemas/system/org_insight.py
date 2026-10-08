"""What Alkera staff see inside one org: its chats, what went wrong in them,
and its audit trail. Read-only shapes for the admin console."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel

from alkera_core.schemas.system.org_audit import OrgAuditEventRead


class OrgChatInsight(BaseModel):
    """One of the org's chats as the admin activity list shows it."""

    id: UUID
    title: str
    owner_email: str
    machine_id: str | None
    #: The bound machine's name; ``None`` when the chat never bound or the row
    #: is gone.
    machine_name: str | None
    #: The machine's state now, judged from its heartbeat (``none`` when the
    #: chat is not bound to a live machine).
    machine_status: str
    #: Whether the box holds a live session for the chat, as it last said.
    mirror_state: str | None
    #: Why the bound machine refused to publish the chat, if it did.
    machine_refusal: str | None
    last_activity_at: datetime | None
    created_at: datetime


class OrgChatInsightList(BaseModel):
    items: list[OrgChatInsight]


OrgIssueKind = Literal["error", "refusal", "denied", "machine_refused"]


class OrgIssue(BaseModel):
    """A thing that went wrong in one of the org's chats.

    ``error``: a turn that ended in an error, or a session that reported one.
    ``refusal``: the model refused. ``denied``: a person or policy rejected a
    permission ask. ``machine_refused``: the bound machine said it cannot
    publish the chat."""

    kind: OrgIssueKind
    chat_id: UUID
    chat_title: str
    detail: str
    at: datetime


class OrgIssueList(BaseModel):
    items: list[OrgIssue]


class OrgAuditList(BaseModel):
    items: list[OrgAuditEventRead]
    total: int
