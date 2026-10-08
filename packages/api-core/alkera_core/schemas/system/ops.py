"""The admin ops summary: SaaS-wide health on one page."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class OpsMachine(BaseModel):
    id: str
    name: str
    org_id: str
    provider: str
    tenancy: str
    #: The banner state judged from the heartbeat (``ready``, ``unreachable``, ...).
    state: str
    #: Live chats bound to this machine.
    chats_served: int


class OpsCount(BaseModel):
    key: str
    count: int


class OpsSpend(BaseModel):
    today_nanos: int
    month_to_date_nanos: int
    #: The platform gateway cap for the month; ``None`` when there is none.
    monthly_cap_nanos: int | None
    #: What is left under the cap this month; ``None`` when there is no cap.
    cap_headroom_nanos: int | None


class OpsHealthCheck(BaseModel):
    key: str
    label: str
    status: Literal["ok", "warn", "fail", "skipped"]
    detail: str


class OpsHealth(BaseModel):
    overall: Literal["ok", "warn", "fail"]
    checks: list[OpsHealthCheck]


class OpsSummary(BaseModel):
    machines: list[OpsMachine]
    machines_by_state: list[OpsCount]
    machines_by_provider: list[OpsCount]
    chats_served_total: int
    spend: OpsSpend
    crash_reports_24h: int
    crash_reports_unread: int
    health: OpsHealth
    build_id: str | None
    app_version: str
