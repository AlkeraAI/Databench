from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="MachineTypeInfo")


@_attrs_define
class MachineTypeInfo:
    """One provisionable machine type from the catalog, priced for the caller.

    Attributes:
        id (str):
        provider_type_id (str):
        display_name (str):
        provider (str | Unset):  Default: 'runpod'.
        compute_class (str | Unset):  Default: 'cpu'.
        gpu_count (int | Unset):  Default: 0.
        vcpu (int | Unset):  Default: 0.
        memory_gb (int | Unset):  Default: 0.
        active (bool | Unset):  Default: True.
        availability (str | Unset):  Default: 'unknown'.
        available_for_new (bool | Unset):  Default: True.
        rate_per_minute_nanos (int | None | Unset):
    """

    id: str
    provider_type_id: str
    display_name: str
    provider: str | Unset = "runpod"
    compute_class: str | Unset = "cpu"
    gpu_count: int | Unset = 0
    vcpu: int | Unset = 0
    memory_gb: int | Unset = 0
    active: bool | Unset = True
    availability: str | Unset = "unknown"
    available_for_new: bool | Unset = True
    rate_per_minute_nanos: int | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        id = self.id

        provider_type_id = self.provider_type_id

        display_name = self.display_name

        provider = self.provider

        compute_class = self.compute_class

        gpu_count = self.gpu_count

        vcpu = self.vcpu

        memory_gb = self.memory_gb

        active = self.active

        availability = self.availability

        available_for_new = self.available_for_new

        rate_per_minute_nanos: int | None | Unset
        if isinstance(self.rate_per_minute_nanos, Unset):
            rate_per_minute_nanos = UNSET
        else:
            rate_per_minute_nanos = self.rate_per_minute_nanos

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "provider_type_id": provider_type_id,
                "display_name": display_name,
            }
        )
        if provider is not UNSET:
            field_dict["provider"] = provider
        if compute_class is not UNSET:
            field_dict["compute_class"] = compute_class
        if gpu_count is not UNSET:
            field_dict["gpu_count"] = gpu_count
        if vcpu is not UNSET:
            field_dict["vcpu"] = vcpu
        if memory_gb is not UNSET:
            field_dict["memory_gb"] = memory_gb
        if active is not UNSET:
            field_dict["active"] = active
        if availability is not UNSET:
            field_dict["availability"] = availability
        if available_for_new is not UNSET:
            field_dict["available_for_new"] = available_for_new
        if rate_per_minute_nanos is not UNSET:
            field_dict["rate_per_minute_nanos"] = rate_per_minute_nanos

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        id = d.pop("id")

        provider_type_id = d.pop("provider_type_id")

        display_name = d.pop("display_name")

        provider = d.pop("provider", UNSET)

        compute_class = d.pop("compute_class", UNSET)

        gpu_count = d.pop("gpu_count", UNSET)

        vcpu = d.pop("vcpu", UNSET)

        memory_gb = d.pop("memory_gb", UNSET)

        active = d.pop("active", UNSET)

        availability = d.pop("availability", UNSET)

        available_for_new = d.pop("available_for_new", UNSET)

        def _parse_rate_per_minute_nanos(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        rate_per_minute_nanos = _parse_rate_per_minute_nanos(d.pop("rate_per_minute_nanos", UNSET))

        machine_type_info = cls(
            id=id,
            provider_type_id=provider_type_id,
            display_name=display_name,
            provider=provider,
            compute_class=compute_class,
            gpu_count=gpu_count,
            vcpu=vcpu,
            memory_gb=memory_gb,
            active=active,
            availability=availability,
            available_for_new=available_for_new,
            rate_per_minute_nanos=rate_per_minute_nanos,
        )

        machine_type_info.additional_properties = d
        return machine_type_info

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
