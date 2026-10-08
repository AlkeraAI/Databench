from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.platform_role import PlatformRole
from ..types import UNSET, Unset

T = TypeVar("T", bound="PlatformRoleUpdate")


@_attrs_define
class PlatformRoleUpdate:
    """ADMIN-only — set or clear a user's platform_role.

    Attributes:
        platform_role (None | PlatformRole | Unset):
    """

    platform_role: None | PlatformRole | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        platform_role: None | str | Unset
        if isinstance(self.platform_role, Unset):
            platform_role = UNSET
        elif isinstance(self.platform_role, PlatformRole):
            platform_role = self.platform_role.value
        else:
            platform_role = self.platform_role

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if platform_role is not UNSET:
            field_dict["platform_role"] = platform_role

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)

        def _parse_platform_role(data: object) -> None | PlatformRole | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                platform_role_type_0 = PlatformRole(data)

                return platform_role_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | PlatformRole | Unset, data)

        platform_role = _parse_platform_role(d.pop("platform_role", UNSET))

        platform_role_update = cls(
            platform_role=platform_role,
        )

        platform_role_update.additional_properties = d
        return platform_role_update

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
