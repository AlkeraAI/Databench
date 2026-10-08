from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

T = TypeVar("T", bound="MachineTypeQuota")


@_attrs_define
class MachineTypeQuota:
    """
    Attributes:
        vcpu_limit (int):
        vcpu_in_use (int):
        vcpu_available (int):
    """

    vcpu_limit: int
    vcpu_in_use: int
    vcpu_available: int
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        vcpu_limit = self.vcpu_limit

        vcpu_in_use = self.vcpu_in_use

        vcpu_available = self.vcpu_available

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "vcpu_limit": vcpu_limit,
                "vcpu_in_use": vcpu_in_use,
                "vcpu_available": vcpu_available,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        vcpu_limit = d.pop("vcpu_limit")

        vcpu_in_use = d.pop("vcpu_in_use")

        vcpu_available = d.pop("vcpu_available")

        machine_type_quota = cls(
            vcpu_limit=vcpu_limit,
            vcpu_in_use=vcpu_in_use,
            vcpu_available=vcpu_available,
        )

        machine_type_quota.additional_properties = d
        return machine_type_quota

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
