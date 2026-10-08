"""Org audit-trail shapes: admin read surface + member agent-activity ingest."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field


class OrgAuditEventRead(BaseModel):
    id: UUID
    actor_email: str
    action: str
    target: str | None
    detail: dict[str, Any] | None
    created_at: datetime


class OrgAuditPage(BaseModel):
    events: list[OrgAuditEventRead]
    total: int
    offset: int
    limit: int


AgentAuditAction = Literal[
    "agent.session_started",
    "agent.session_finished",
    "agent.decision_denied",
    "agent.decision_escalated",
    "agent.data_access",
    "agent.cost",
    "agent.audit_gap",
]
"""The only actions a member's daemon may write. Everything else in the audit
vocabulary is server-emitted; a client claiming e.g. ``billing.budget_set``
must be rejected at the schema layer."""


class AgentAuditEventIn(BaseModel):
    """One agent-activity event as reported by the daemon.

    Carries who/what/when/outcome. Trace content never leaves the member's
    machine and is redacted server-side if sent anyway."""

    action: AgentAuditAction
    session_id: str = Field(min_length=1, max_length=128)
    # The client's clock at the event, kept for display: the row's created_at is
    # server receipt time (spooled offline events can arrive much later).
    occurred_at: datetime
    target: str | None = Field(default=None, max_length=320)
    detail: dict[str, Any] = Field(default_factory=dict)


class AgentAuditBatch(BaseModel):
    events: list[AgentAuditEventIn] = Field(min_length=1, max_length=100)


class AgentAuditAccepted(BaseModel):
    accepted: int


class AuditChainVerification(BaseModel):
    """Result of verifying the org's tamper-evident audit hash chain."""

    ok: bool
    checked: int
    # When ``ok`` is False, the first event whose hash/link doesn't validate
    # (i.e. it or something before it was edited, deleted, re-ordered, or inserted).
    broken_event_id: UUID | None = None
    broken_at: datetime | None = None
    # The latest entry_hash — export this as an external checkpoint so even a full
    # chain rewrite is detectable.
    head_hash: str | None = None
