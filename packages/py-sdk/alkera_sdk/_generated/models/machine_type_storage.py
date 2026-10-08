from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

T = TypeVar("T", bound="MachineTypeStorage")


@_attrs_define
class MachineTypeStorage:
    """
    Attributes:
        min_gb (int):
        max_gb (int):
        kind (str):
    """

    min_gb: int
    max_gb: int
    kind: str
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        min_gb = self.min_gb

        max_gb = self.max_gb

        kind = self.kind

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "min_gb": min_gb,
                "max_gb": max_gb,
                "kind": kind,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        min_gb = d.pop("min_gb")

        max_gb = d.pop("max_gb")

        kind = d.pop("kind")

        machine_type_storage = cls(
            min_gb=min_gb,
            max_gb=max_gb,
            kind=kind,
        )

        machine_type_storage.additional_properties = d
        return machine_type_storage

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
