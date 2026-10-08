from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar
from uuid import UUID

from attrs import define as _attrs_define
from attrs import field as _attrs_field

T = TypeVar("T", bound="SsoExemptMember")


@_attrs_define
class SsoExemptMember:
    """A break-glass member of this org: may sign in without the org's IdP even
    where the org enforces SSO. The flag is on their membership in this org and
    has no effect anywhere else.

        Attributes:
            user_id (UUID):
            email (str):
            sso_exempt (bool):
    """

    user_id: UUID
    email: str
    sso_exempt: bool
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        user_id = str(self.user_id)

        email = self.email

        sso_exempt = self.sso_exempt

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "user_id": user_id,
                "email": email,
                "sso_exempt": sso_exempt,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        user_id = UUID(d.pop("user_id"))

        email = d.pop("email")

        sso_exempt = d.pop("sso_exempt")

        sso_exempt_member = cls(
            user_id=user_id,
            email=email,
            sso_exempt=sso_exempt,
        )

        sso_exempt_member.additional_properties = d
        return sso_exempt_member

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
