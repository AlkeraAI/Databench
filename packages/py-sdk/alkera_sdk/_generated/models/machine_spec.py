from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.gpu_spec import GpuSpec


T = TypeVar("T", bound="MachineSpec")


@_attrs_define
class MachineSpec:
    """The hardware and the customer's rates of one machine.

    Attributes:
        offering_name (str):
        provider (str):
        region (str):
        vcpu (int):
        memory_gb (int):
        disk_gb (int):
        gpu (GpuSpec | None | Unset):
        rate_per_minute_nanos (int | None | Unset):
        storage_rate_per_minute_nanos (int | None | Unset):
    """

    offering_name: str
    provider: str
    region: str
    vcpu: int
    memory_gb: int
    disk_gb: int
    gpu: GpuSpec | None | Unset = UNSET
    rate_per_minute_nanos: int | None | Unset = UNSET
    storage_rate_per_minute_nanos: int | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.gpu_spec import GpuSpec

        offering_name = self.offering_name

        provider = self.provider

        region = self.region

        vcpu = self.vcpu

        memory_gb = self.memory_gb

        disk_gb = self.disk_gb

        gpu: dict[str, Any] | None | Unset
        if isinstance(self.gpu, Unset):
            gpu = UNSET
        elif isinstance(self.gpu, GpuSpec):
            gpu = self.gpu.to_dict()
        else:
            gpu = self.gpu

        rate_per_minute_nanos: int | None | Unset
        if isinstance(self.rate_per_minute_nanos, Unset):
            rate_per_minute_nanos = UNSET
        else:
            rate_per_minute_nanos = self.rate_per_minute_nanos

        storage_rate_per_minute_nanos: int | None | Unset
        if isinstance(self.storage_rate_per_minute_nanos, Unset):
            storage_rate_per_minute_nanos = UNSET
        else:
            storage_rate_per_minute_nanos = self.storage_rate_per_minute_nanos

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "offering_name": offering_name,
                "provider": provider,
                "region": region,
                "vcpu": vcpu,
                "memory_gb": memory_gb,
                "disk_gb": disk_gb,
            }
        )
        if gpu is not UNSET:
            field_dict["gpu"] = gpu
        if rate_per_minute_nanos is not UNSET:
            field_dict["rate_per_minute_nanos"] = rate_per_minute_nanos
        if storage_rate_per_minute_nanos is not UNSET:
            field_dict["storage_rate_per_minute_nanos"] = storage_rate_per_minute_nanos

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.gpu_spec import GpuSpec

        d = dict(src_dict)
        offering_name = d.pop("offering_name")

        provider = d.pop("provider")

        region = d.pop("region")

        vcpu = d.pop("vcpu")

        memory_gb = d.pop("memory_gb")

        disk_gb = d.pop("disk_gb")

        def _parse_gpu(data: object) -> GpuSpec | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                gpu_type_0 = GpuSpec.from_dict(data)

                return gpu_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(GpuSpec | None | Unset, data)

        gpu = _parse_gpu(d.pop("gpu", UNSET))

        def _parse_rate_per_minute_nanos(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        rate_per_minute_nanos = _parse_rate_per_minute_nanos(d.pop("rate_per_minute_nanos", UNSET))

        def _parse_storage_rate_per_minute_nanos(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        storage_rate_per_minute_nanos = _parse_storage_rate_per_minute_nanos(
            d.pop("storage_rate_per_minute_nanos", UNSET)
        )

        machine_spec = cls(
            offering_name=offering_name,
            provider=provider,
            region=region,
            vcpu=vcpu,
            memory_gb=memory_gb,
            disk_gb=disk_gb,
            gpu=gpu,
            rate_per_minute_nanos=rate_per_minute_nanos,
            storage_rate_per_minute_nanos=storage_rate_per_minute_nanos,
        )

        machine_spec.additional_properties = d
        return machine_spec

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
