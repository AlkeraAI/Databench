from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.grant_entry import GrantEntry
    from ..models.role_option import RoleOption


T = TypeVar("T", bound="GrantList")


@_attrs_define
class GrantList:
    """
    Attributes:
        value (list[GrantEntry]):
        assignable_roles (list[RoleOption] | Unset):
    """

    value: list[GrantEntry]
    assignable_roles: list[RoleOption] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        value = []
        for value_item_data in self.value:
            value_item = value_item_data.to_dict()
            value.append(value_item)

        assignable_roles: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.assignable_roles, Unset):
            assignable_roles = []
            for assignable_roles_item_data in self.assignable_roles:
                assignable_roles_item = assignable_roles_item_data.to_dict()
                assignable_roles.append(assignable_roles_item)

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "value": value,
            }
        )
        if assignable_roles is not UNSET:
            field_dict["assignableRoles"] = assignable_roles

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.grant_entry import GrantEntry
        from ..models.role_option import RoleOption

        d = dict(src_dict)
        value = []
        _value = d.pop("value")
        for value_item_data in _value:
            value_item = GrantEntry.from_dict(value_item_data)

            value.append(value_item)

        _assignable_roles = d.pop("assignableRoles", UNSET)
        assignable_roles: list[RoleOption] | Unset = UNSET
        if _assignable_roles is not UNSET:
            assignable_roles = []
            for assignable_roles_item_data in _assignable_roles:
                assignable_roles_item = RoleOption.from_dict(assignable_roles_item_data)

                assignable_roles.append(assignable_roles_item)

        grant_list = cls(
            value=value,
            assignable_roles=assignable_roles,
        )

        grant_list.additional_properties = d
        return grant_list

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
