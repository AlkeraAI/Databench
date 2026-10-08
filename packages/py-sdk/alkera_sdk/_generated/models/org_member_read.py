from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar
from uuid import UUID

from attrs import define as _attrs_define
from attrs import field as _attrs_field

T = TypeVar("T", bound="OrgMemberRead")


@_attrs_define
class OrgMemberRead:
    """
    Attributes:
        user_id (UUID):
        email (str):
        display_name (str):
        is_admin (bool):
        is_active (bool):
        sso_exempt (bool):
    """

    user_id: UUID
    email: str
    display_name: str
    is_admin: bool
    is_active: bool
    sso_exempt: bool
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        user_id = str(self.user_id)

        email = self.email

        display_name = self.display_name

        is_admin = self.is_admin

        is_active = self.is_active

        sso_exempt = self.sso_exempt

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "user_id": user_id,
                "email": email,
                "display_name": display_name,
                "is_admin": is_admin,
                "is_active": is_active,
                "sso_exempt": sso_exempt,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        user_id = UUID(d.pop("user_id"))

        email = d.pop("email")

        display_name = d.pop("display_name")

        is_admin = d.pop("is_admin")

        is_active = d.pop("is_active")

        sso_exempt = d.pop("sso_exempt")

        org_member_read = cls(
            user_id=user_id,
            email=email,
            display_name=display_name,
            is_admin=is_admin,
            is_active=is_active,
            sso_exempt=sso_exempt,
        )

        org_member_read.additional_properties = d
        return org_member_read

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
