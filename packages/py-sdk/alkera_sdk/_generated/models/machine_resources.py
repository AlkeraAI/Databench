from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.gpu_sample import GpuSample


T = TypeVar("T", bound="MachineResources")


@_attrs_define
class MachineResources:
    """
    Attributes:
        cpu_percent (float | Unset):  Default: 0.0.
        memory_used_bytes (int | Unset):  Default: 0.
        memory_limit_bytes (int | Unset):  Default: 0.
        disk_used_bytes (int | Unset):  Default: 0.
        disk_total_bytes (int | Unset):  Default: 0.
        org_workers (int | Unset):  Default: 0.
        org_worker_capacity (int | Unset):  Default: 0.
        org_worker_memory_bytes (int | Unset):  Default: 0.
        org_workers_failing (int | Unset):  Default: 0.
        org_workers_failing_ids (list[str] | Unset):
        gpus (list[GpuSample] | Unset):
        org_slots_free (int | None | Unset):
    """

    cpu_percent: float | Unset = 0.0
    memory_used_bytes: int | Unset = 0
    memory_limit_bytes: int | Unset = 0
    disk_used_bytes: int | Unset = 0
    disk_total_bytes: int | Unset = 0
    org_workers: int | Unset = 0
    org_worker_capacity: int | Unset = 0
    org_worker_memory_bytes: int | Unset = 0
    org_workers_failing: int | Unset = 0
    org_workers_failing_ids: list[str] | Unset = UNSET
    gpus: list[GpuSample] | Unset = UNSET
    org_slots_free: int | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        cpu_percent = self.cpu_percent

        memory_used_bytes = self.memory_used_bytes

        memory_limit_bytes = self.memory_limit_bytes

        disk_used_bytes = self.disk_used_bytes

        disk_total_bytes = self.disk_total_bytes

        org_workers = self.org_workers

        org_worker_capacity = self.org_worker_capacity

        org_worker_memory_bytes = self.org_worker_memory_bytes

        org_workers_failing = self.org_workers_failing

        org_workers_failing_ids: list[str] | Unset = UNSET
        if not isinstance(self.org_workers_failing_ids, Unset):
            org_workers_failing_ids = self.org_workers_failing_ids

        gpus: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.gpus, Unset):
            gpus = []
            for gpus_item_data in self.gpus:
                gpus_item = gpus_item_data.to_dict()
                gpus.append(gpus_item)

        org_slots_free: int | None | Unset
        if isinstance(self.org_slots_free, Unset):
            org_slots_free = UNSET
        else:
            org_slots_free = self.org_slots_free

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if cpu_percent is not UNSET:
            field_dict["cpu_percent"] = cpu_percent
        if memory_used_bytes is not UNSET:
            field_dict["memory_used_bytes"] = memory_used_bytes
        if memory_limit_bytes is not UNSET:
            field_dict["memory_limit_bytes"] = memory_limit_bytes
        if disk_used_bytes is not UNSET:
            field_dict["disk_used_bytes"] = disk_used_bytes
        if disk_total_bytes is not UNSET:
            field_dict["disk_total_bytes"] = disk_total_bytes
        if org_workers is not UNSET:
            field_dict["org_workers"] = org_workers
        if org_worker_capacity is not UNSET:
            field_dict["org_worker_capacity"] = org_worker_capacity
        if org_worker_memory_bytes is not UNSET:
            field_dict["org_worker_memory_bytes"] = org_worker_memory_bytes
        if org_workers_failing is not UNSET:
            field_dict["org_workers_failing"] = org_workers_failing
        if org_workers_failing_ids is not UNSET:
            field_dict["org_workers_failing_ids"] = org_workers_failing_ids
        if gpus is not UNSET:
            field_dict["gpus"] = gpus
        if org_slots_free is not UNSET:
            field_dict["org_slots_free"] = org_slots_free

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.gpu_sample import GpuSample

        d = dict(src_dict)
        cpu_percent = d.pop("cpu_percent", UNSET)

        memory_used_bytes = d.pop("memory_used_bytes", UNSET)

        memory_limit_bytes = d.pop("memory_limit_bytes", UNSET)

        disk_used_bytes = d.pop("disk_used_bytes", UNSET)

        disk_total_bytes = d.pop("disk_total_bytes", UNSET)

        org_workers = d.pop("org_workers", UNSET)

        org_worker_capacity = d.pop("org_worker_capacity", UNSET)

        org_worker_memory_bytes = d.pop("org_worker_memory_bytes", UNSET)

        org_workers_failing = d.pop("org_workers_failing", UNSET)

        org_workers_failing_ids = cast(list[str], d.pop("org_workers_failing_ids", UNSET))

        _gpus = d.pop("gpus", UNSET)
        gpus: list[GpuSample] | Unset = UNSET
        if _gpus is not UNSET:
            gpus = []
            for gpus_item_data in _gpus:
                gpus_item = GpuSample.from_dict(gpus_item_data)

                gpus.append(gpus_item)

        def _parse_org_slots_free(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        org_slots_free = _parse_org_slots_free(d.pop("org_slots_free", UNSET))

        machine_resources = cls(
            cpu_percent=cpu_percent,
            memory_used_bytes=memory_used_bytes,
            memory_limit_bytes=memory_limit_bytes,
            disk_used_bytes=disk_used_bytes,
            disk_total_bytes=disk_total_bytes,
            org_workers=org_workers,
            org_worker_capacity=org_worker_capacity,
            org_worker_memory_bytes=org_worker_memory_bytes,
            org_workers_failing=org_workers_failing,
            org_workers_failing_ids=org_workers_failing_ids,
            gpus=gpus,
            org_slots_free=org_slots_free,
        )

        machine_resources.additional_properties = d
        return machine_resources

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
