from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.machine_type_availability import MachineTypeAvailability
    from ..models.machine_type_quota import MachineTypeQuota
    from ..models.machine_type_storage import MachineTypeStorage


T = TypeVar("T", bound="MachineTypeRow")


@_attrs_define
class MachineTypeRow:
    """
    Attributes:
        id (str):
        provider (str):
        code (str):
        vcpu (int):
        memory_gb (int):
        gpu (int):
        storage (MachineTypeStorage):
        price_per_minute_nanos (int):
        available (bool):
        can_offer (bool):
        availability (MachineTypeAvailability):
        quota (MachineTypeQuota | None | Unset):
    """

    id: str
    provider: str
    code: str
    vcpu: int
    memory_gb: int
    gpu: int
    storage: MachineTypeStorage
    price_per_minute_nanos: int
    available: bool
    can_offer: bool
    availability: MachineTypeAvailability
    quota: MachineTypeQuota | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.machine_type_quota import MachineTypeQuota

        id = self.id

        provider = self.provider

        code = self.code

        vcpu = self.vcpu

        memory_gb = self.memory_gb

        gpu = self.gpu

        storage = self.storage.to_dict()

        price_per_minute_nanos = self.price_per_minute_nanos

        available = self.available

        can_offer = self.can_offer

        availability = self.availability.to_dict()

        quota: dict[str, Any] | None | Unset
        if isinstance(self.quota, Unset):
            quota = UNSET
        elif isinstance(self.quota, MachineTypeQuota):
            quota = self.quota.to_dict()
        else:
            quota = self.quota

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "provider": provider,
                "code": code,
                "vcpu": vcpu,
                "memory_gb": memory_gb,
                "gpu": gpu,
                "storage": storage,
                "price_per_minute_nanos": price_per_minute_nanos,
                "available": available,
                "can_offer": can_offer,
                "availability": availability,
            }
        )
        if quota is not UNSET:
            field_dict["quota"] = quota

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.machine_type_availability import MachineTypeAvailability
        from ..models.machine_type_quota import MachineTypeQuota
        from ..models.machine_type_storage import MachineTypeStorage

        d = dict(src_dict)
        id = d.pop("id")

        provider = d.pop("provider")

        code = d.pop("code")

        vcpu = d.pop("vcpu")

        memory_gb = d.pop("memory_gb")

        gpu = d.pop("gpu")

        storage = MachineTypeStorage.from_dict(d.pop("storage"))

        price_per_minute_nanos = d.pop("price_per_minute_nanos")

        available = d.pop("available")

        can_offer = d.pop("can_offer")

        availability = MachineTypeAvailability.from_dict(d.pop("availability"))

        def _parse_quota(data: object) -> MachineTypeQuota | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                quota_type_0 = MachineTypeQuota.from_dict(data)

                return quota_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(MachineTypeQuota | None | Unset, data)

        quota = _parse_quota(d.pop("quota", UNSET))

        machine_type_row = cls(
            id=id,
            provider=provider,
            code=code,
            vcpu=vcpu,
            memory_gb=memory_gb,
            gpu=gpu,
            storage=storage,
            price_per_minute_nanos=price_per_minute_nanos,
            available=available,
            can_offer=can_offer,
            availability=availability,
            quota=quota,
        )

        machine_type_row.additional_properties = d
        return machine_type_row

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
