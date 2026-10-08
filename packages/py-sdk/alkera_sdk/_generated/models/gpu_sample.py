from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="GpuSample")


@_attrs_define
class GpuSample:
    """One GPU as the box sampled it on its last beat.

    Attributes:
        index (int):
        name (str | Unset):  Default: ''.
        memory_used_bytes (int | Unset):  Default: 0.
        memory_total_bytes (int | Unset):  Default: 0.
        utilization_percent (float | Unset):  Default: 0.0.
    """

    index: int
    name: str | Unset = ""
    memory_used_bytes: int | Unset = 0
    memory_total_bytes: int | Unset = 0
    utilization_percent: float | Unset = 0.0
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        index = self.index

        name = self.name

        memory_used_bytes = self.memory_used_bytes

        memory_total_bytes = self.memory_total_bytes

        utilization_percent = self.utilization_percent

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "index": index,
            }
        )
        if name is not UNSET:
            field_dict["name"] = name
        if memory_used_bytes is not UNSET:
            field_dict["memory_used_bytes"] = memory_used_bytes
        if memory_total_bytes is not UNSET:
            field_dict["memory_total_bytes"] = memory_total_bytes
        if utilization_percent is not UNSET:
            field_dict["utilization_percent"] = utilization_percent

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        index = d.pop("index")

        name = d.pop("name", UNSET)

        memory_used_bytes = d.pop("memory_used_bytes", UNSET)

        memory_total_bytes = d.pop("memory_total_bytes", UNSET)

        utilization_percent = d.pop("utilization_percent", UNSET)

        gpu_sample = cls(
            index=index,
            name=name,
            memory_used_bytes=memory_used_bytes,
            memory_total_bytes=memory_total_bytes,
            utilization_percent=utilization_percent,
        )

        gpu_sample.additional_properties = d
        return gpu_sample

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
