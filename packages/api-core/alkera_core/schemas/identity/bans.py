"""Platform-ban shapes for the admin console (staff-only, cross-tenant).

A ban row is history: the read shape carries who was banned, by whom, and —
once lifted — by whom it was lifted. The person on the other side never sees
any of it: to them every path answers as if the account did not exist.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from alkera_core.bans import InvalidDomainError, normalize_domain

#: Longest reason the register keeps. Free text for other admins, never shown
#: to the banned person.
REASON_MAX_LENGTH = 2000


class UserBanCreate(BaseModel):
    user_id: UUID
    reason: str = Field(default="", max_length=REASON_MAX_LENGTH)


class UserBanRead(BaseModel):
    id: UUID
    user_id: UUID
    user_email: str
    user_display_name: str
    reason: str
    created_at: datetime
    created_by_id: UUID | None = None
    created_by_email: str | None = None
    lifted_at: datetime | None = None
    lifted_by_id: UUID | None = None
    lifted_by_email: str | None = None
    #: ``lifted_at is None`` — spelled out so a list can be filtered without
    #: every client re-deriving it.
    active: bool


class DomainBanCreate(BaseModel):
    """``domain`` is normalized on the way in — lower-cased, surrounding
    whitespace and a leading ``@`` dropped — and refused unless what remains is
    a bare hostname, so a full address or a URL is a 422 rather than a ban that
    matches nobody."""

    domain: str = Field(min_length=1, max_length=255)
    reason: str = Field(default="", max_length=REASON_MAX_LENGTH)

    @field_validator("domain")
    @classmethod
    def _normalize(cls, value: str) -> str:
        try:
            return normalize_domain(value)
        except InvalidDomainError as exc:
            raise ValueError("enter a bare domain such as example.com") from exc


class DomainBanRead(BaseModel):
    id: UUID
    domain: str
    reason: str
    created_at: datetime
    created_by_id: UUID | None = None
    created_by_email: str | None = None
    lifted_at: datetime | None = None
    lifted_by_id: UUID | None = None
    lifted_by_email: str | None = None
    active: bool


__all__ = [
    "REASON_MAX_LENGTH",
    "DomainBanCreate",
    "DomainBanRead",
    "UserBanCreate",
    "UserBanRead",
]
