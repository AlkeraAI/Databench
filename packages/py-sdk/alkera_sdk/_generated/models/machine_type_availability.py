from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.machine_type_availability_status import MachineTypeAvailabilityStatus
from ..types import UNSET, Unset

T = TypeVar("T", bound="MachineTypeAvailability")


@_attrs_define
class MachineTypeAvailability:
    """
    Attributes:
        status (MachineTypeAvailabilityStatus):
        checked_at (datetime.datetime):
        detail (str | Unset):  Default: ''.
    """

    status: MachineTypeAvailabilityStatus
    checked_at: datetime.datetime
    detail: str | Unset = ""
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        status = self.status.value

        checked_at = self.checked_at.isoformat()

        detail = self.detail

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "status": status,
                "checked_at": checked_at,
            }
        )
        if detail is not UNSET:
            field_dict["detail"] = detail

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        status = MachineTypeAvailabilityStatus(d.pop("status"))

        checked_at = datetime.datetime.fromisoformat(d.pop("checked_at"))

        detail = d.pop("detail", UNSET)

        machine_type_availability = cls(
            status=status,
            checked_at=checked_at,
            detail=detail,
        )

        machine_type_availability.additional_properties = d
        return machine_type_availability

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
