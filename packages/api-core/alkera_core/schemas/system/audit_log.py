"""Read schemas for the admin audit log.

Plain `BaseModel` — these are in-flight HTTP shapes, not persisted documents
(the audit row is a Postgres table, not a Pydantic-on-disk model), so no
`VersionedModel` machinery applies.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class AuditLogRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    actor_id: UUID | None
    actor_email: str
    actor_platform_role: str | None
    action: str
    method: str
    path: str
    status_code: int
    target: str | None = Field(
        default=None,
        description="What the action was done to, named when it happened (an org, "
        "a user's email, a machine). None when the action names no single target.",
    )
    detail: dict[str, Any] | None
    created_at: datetime


class AuditLogPage(BaseModel):
    """One page of audit entries, newest-first."""

    items: list[AuditLogRead]
    total: int
    page: int
    page_size: int
    actions: list[str] = Field(
        default_factory=list,
        description="Every action the log holds (unfiltered), for the action filter.",
    )
