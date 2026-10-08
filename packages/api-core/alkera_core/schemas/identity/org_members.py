"""Org-admin member management: list members + deprovision (deactivate)."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel


class OrgMemberRead(BaseModel):
    user_id: UUID
    email: str
    display_name: str
    is_admin: bool
    is_active: bool
    sso_exempt: bool


class SetMemberActiveRequest(BaseModel):
    active: bool
