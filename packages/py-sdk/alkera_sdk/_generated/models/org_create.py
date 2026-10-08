from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

T = TypeVar("T", bound="OrgCreate")


@_attrs_define
class OrgCreate:
    """
    Attributes:
        name (str):
        admin_email (str):
        admin_first_name (str):
        admin_last_name (str):
        admin_password (str):
    """

    name: str
    admin_email: str
    admin_first_name: str
    admin_last_name: str
    admin_password: str
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        name = self.name

        admin_email = self.admin_email

        admin_first_name = self.admin_first_name

        admin_last_name = self.admin_last_name

        admin_password = self.admin_password

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "name": name,
                "admin_email": admin_email,
                "admin_first_name": admin_first_name,
                "admin_last_name": admin_last_name,
                "admin_password": admin_password,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        name = d.pop("name")

        admin_email = d.pop("admin_email")

        admin_first_name = d.pop("admin_first_name")

        admin_last_name = d.pop("admin_last_name")

        admin_password = d.pop("admin_password")

        org_create = cls(
            name=name,
            admin_email=admin_email,
            admin_first_name=admin_first_name,
            admin_last_name=admin_last_name,
            admin_password=admin_password,
        )

        org_create.additional_properties = d
        return org_create

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
