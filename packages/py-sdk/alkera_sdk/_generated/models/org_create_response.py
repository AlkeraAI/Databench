from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

if TYPE_CHECKING:
    from ..models.org_read import OrgRead
    from ..models.user_read import UserRead


T = TypeVar("T", bound="OrgCreateResponse")


@_attrs_define
class OrgCreateResponse:
    """
    Attributes:
        org (OrgRead): Same shape as TeamRead — separate name communicates intent at the
            admin route surface — plus, on the detail read, the org's storage picture.
        admin (UserRead):
    """

    org: OrgRead
    admin: UserRead
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        org = self.org.to_dict()

        admin = self.admin.to_dict()

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "org": org,
                "admin": admin,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.org_read import OrgRead
        from ..models.user_read import UserRead

        d = dict(src_dict)
        org = OrgRead.from_dict(d.pop("org"))

        admin = UserRead.from_dict(d.pop("admin"))

        org_create_response = cls(
            org=org,
            admin=admin,
        )

        org_create_response.additional_properties = d
        return org_create_response

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
