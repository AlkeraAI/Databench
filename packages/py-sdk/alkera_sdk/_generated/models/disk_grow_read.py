from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

T = TypeVar("T", bound="DiskGrowRead")


@_attrs_define
class DiskGrowRead:
    """How far a manager may grow a machine's disk now, as the server decided
    it. ``None`` on the machine where it cannot grow.

        Attributes:
            min_gb (int):
            max_gb (int):
            restarts (bool):
    """

    min_gb: int
    max_gb: int
    restarts: bool
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        min_gb = self.min_gb

        max_gb = self.max_gb

        restarts = self.restarts

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "min_gb": min_gb,
                "max_gb": max_gb,
                "restarts": restarts,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        min_gb = d.pop("min_gb")

        max_gb = d.pop("max_gb")

        restarts = d.pop("restarts")

        disk_grow_read = cls(
            min_gb=min_gb,
            max_gb=max_gb,
            restarts=restarts,
        )

        disk_grow_read.additional_properties = d
        return disk_grow_read

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
