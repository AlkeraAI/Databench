"""HTTP shapes for the observability endpoints (crash reports + client errors).

In-flight request/response models, so plain `BaseModel` (not `VersionedModel` —
nothing here is persisted as a serialized blob; the crash report is stored via
the `CrashReport` ORM row).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

# Which surface produced the event.
Component = Literal["cli", "daemon", "extension", "web", "backend", "gateway"]


class CrashReportCreate(BaseModel):
    """An opt-in crash report submitted by a user (with their consent)."""

    component: Component
    message: str = Field(min_length=1, max_length=4000)
    error_type: str | None = Field(default=None, max_length=255)
    stacktrace: str | None = Field(default=None, max_length=50_000)
    context: dict[str, Any] | None = None
    comment: str | None = Field(default=None, max_length=4000)
    app_version: str | None = Field(default=None, max_length=64)
    platform: str | None = Field(default=None, max_length=128)
    logs: str | None = Field(default=None, max_length=100_000)
    occurred_at: datetime | None = None


class CrashReportRead(BaseModel):
    """Full stored crash report (returned to the submitter on create / detail)."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    user_id: UUID
    org_team_id: UUID
    component: str
    error_type: str | None
    message: str
    stacktrace: str | None
    context: dict[str, Any] | None
    comment: str | None
    app_version: str | None
    platform: str | None
    occurred_at: datetime | None
    created_at: datetime


class CrashReportSummary(BaseModel):
    """Lightweight crash report for list views (no stacktrace / logs)."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    component: str
    error_type: str | None
    message: str
    app_version: str | None
    platform: str | None
    occurred_at: datetime | None
    created_at: datetime


class AdminCrashReportSummary(BaseModel):
    """Crash report row for the admin triage list (no heavy fields)."""

    id: UUID
    user_id: UUID
    user_email: str
    user_display_name: str
    org_team_id: UUID
    component: str
    error_type: str | None
    message: str
    app_version: str | None
    platform: str | None
    occurred_at: datetime | None
    created_at: datetime
    read_at: datetime | None
    read_by_user_id: UUID | None
    read_by_email: str | None = None


class AdminCrashReportDetail(AdminCrashReportSummary):
    """Full crash report for the admin detail view."""

    stacktrace: str | None
    context: dict[str, Any] | None
    comment: str | None
    logs: str | None


class CrashReportListResponse(BaseModel):
    """Paginated admin list + counts for the dashboard header."""

    items: list[AdminCrashReportSummary]
    total: int
    unread: int


class CrashReportReadUpdate(BaseModel):
    """Mark a crash report read (records the acting admin) or unread."""

    read: bool


class ClientErrorEvent(BaseModel):
    """A passively-captured client error (web / extension), logged not stored."""

    component: Literal["web", "extension"] = "web"
    message: str = Field(min_length=1, max_length=4000)
    error_type: str | None = Field(default=None, max_length=255)
    stack: str | None = Field(default=None, max_length=50_000)
    url: str | None = Field(default=None, max_length=2000)
    context: dict[str, Any] | None = None


class ErrorEventAck(BaseModel):
    """Acknowledgement for a logged client error — carries the trace id back."""

    received: bool = True
    trace_id: str
