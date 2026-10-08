from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.gpu_spec import GpuSpec


T = TypeVar("T", bound="BoxMachineCard")


@_attrs_define
class BoxMachineCard:
    """What a box is told about the machine it runs on, so its chats can say
    what they run on and whether the minutes are billed.

        Attributes:
            name (str):
            vcpu (int):
            memory_gb (int):
            disk_gb (int):
            billed_per_minute (bool):
            gpu (GpuSpec | None | Unset):
            idle_stop_minutes (int | None | Unset):
    """

    name: str
    vcpu: int
    memory_gb: int
    disk_gb: int
    billed_per_minute: bool
    gpu: GpuSpec | None | Unset = UNSET
    idle_stop_minutes: int | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.gpu_spec import GpuSpec

        name = self.name

        vcpu = self.vcpu

        memory_gb = self.memory_gb

        disk_gb = self.disk_gb

        billed_per_minute = self.billed_per_minute

        gpu: dict[str, Any] | None | Unset
        if isinstance(self.gpu, Unset):
            gpu = UNSET
        elif isinstance(self.gpu, GpuSpec):
            gpu = self.gpu.to_dict()
        else:
            gpu = self.gpu

        idle_stop_minutes: int | None | Unset
        if isinstance(self.idle_stop_minutes, Unset):
            idle_stop_minutes = UNSET
        else:
            idle_stop_minutes = self.idle_stop_minutes

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "name": name,
                "vcpu": vcpu,
                "memory_gb": memory_gb,
                "disk_gb": disk_gb,
                "billed_per_minute": billed_per_minute,
            }
        )
        if gpu is not UNSET:
            field_dict["gpu"] = gpu
        if idle_stop_minutes is not UNSET:
            field_dict["idle_stop_minutes"] = idle_stop_minutes

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.gpu_spec import GpuSpec

        d = dict(src_dict)
        name = d.pop("name")

        vcpu = d.pop("vcpu")

        memory_gb = d.pop("memory_gb")

        disk_gb = d.pop("disk_gb")

        billed_per_minute = d.pop("billed_per_minute")

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

        def _parse_idle_stop_minutes(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        idle_stop_minutes = _parse_idle_stop_minutes(d.pop("idle_stop_minutes", UNSET))

        box_machine_card = cls(
            name=name,
            vcpu=vcpu,
            memory_gb=memory_gb,
            disk_gb=disk_gb,
            billed_per_minute=billed_per_minute,
            gpu=gpu,
            idle_stop_minutes=idle_stop_minutes,
        )

        box_machine_card.additional_properties = d
        return box_machine_card

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
