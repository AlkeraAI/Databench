from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import Any, TypeVar
from uuid import UUID

from attrs import define as _attrs_define
from attrs import field as _attrs_field

T = TypeVar("T", bound="OrgUserRead")


@_attrs_define
class OrgUserRead:
    """One colleague as the organization directory shows them.

    Deliberately narrower than ``UserRead``. A read of SOMEONE ELSE must not
    carry their security posture: ``mfa_enabled`` and ``has_password`` together
    name exactly which colleagues have no second factor and which sign in with a
    password — a ranked target list for a phishing or credential-stuffing run —
    while ``platform_role`` marks who is Alkera staff inside the tenant and
    ``email_verified_at`` who never proved their address. A caller's own posture
    is still theirs to read, on ``GET /api/v1/auth/me``.

    ``display_name`` is the name this org shows for the person: the one its
    admin, IdP or SCIM set on their membership, else their own name. No field
    names an org or carries anything another org knows about them.

        Attributes:
            id (UUID):
            email (str):
            first_name (str):
            last_name (str):
            display_name (str):
            created_at (datetime.datetime):
    """

    id: UUID
    email: str
    first_name: str
    last_name: str
    display_name: str
    created_at: datetime.datetime
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        id = str(self.id)

        email = self.email

        first_name = self.first_name

        last_name = self.last_name

        display_name = self.display_name

        created_at = self.created_at.isoformat()

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "email": email,
                "first_name": first_name,
                "last_name": last_name,
                "display_name": display_name,
                "created_at": created_at,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        id = UUID(d.pop("id"))

        email = d.pop("email")

        first_name = d.pop("first_name")

        last_name = d.pop("last_name")

        display_name = d.pop("display_name")

        created_at = datetime.datetime.fromisoformat(d.pop("created_at"))

        org_user_read = cls(
            id=id,
            email=email,
            first_name=first_name,
            last_name=last_name,
            display_name=display_name,
            created_at=created_at,
        )

        org_user_read.additional_properties = d
        return org_user_read

    @property
    def additional_keys(self) -> list[str]:
        return list(self.additional_properties.keys())

    def __getitem__(self, key: str) -> Any:
        return self.additional_properties[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self.additional_properties[key] = value

    def __delitem__(self, key: str) -> None:
        del self.additional_properties[key]

    def __contains__(self, key: str) -> bool:
        return key in self.additional_properties
