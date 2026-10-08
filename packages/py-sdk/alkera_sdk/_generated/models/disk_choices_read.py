from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.disk_choices_read_grow import DiskChoicesReadGrow

T = TypeVar("T", bound="DiskChoicesRead")


@_attrs_define
class DiskChoicesRead:
    """What a buyer may choose for an offering's disks, as the server decided
    it (``alkera_core.compute.offering_disks``). Clients render these and
    never derive one.

        Attributes:
            volume_min_gb (int):
            volume_max_gb (int):
            volume_default_gb (int):
            container_gb (int):
            volume_billed_while_stopped (bool):
            grow (DiskChoicesReadGrow):
    """

    volume_min_gb: int
    volume_max_gb: int
    volume_default_gb: int
    container_gb: int
    volume_billed_while_stopped: bool
    grow: DiskChoicesReadGrow
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        volume_min_gb = self.volume_min_gb

        volume_max_gb = self.volume_max_gb

        volume_default_gb = self.volume_default_gb

        container_gb = self.container_gb

        volume_billed_while_stopped = self.volume_billed_while_stopped

        grow = self.grow.value

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "volume_min_gb": volume_min_gb,
                "volume_max_gb": volume_max_gb,
                "volume_default_gb": volume_default_gb,
                "container_gb": container_gb,
                "volume_billed_while_stopped": volume_billed_while_stopped,
                "grow": grow,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        volume_min_gb = d.pop("volume_min_gb")

        volume_max_gb = d.pop("volume_max_gb")

        volume_default_gb = d.pop("volume_default_gb")

        container_gb = d.pop("container_gb")

        volume_billed_while_stopped = d.pop("volume_billed_while_stopped")

        grow = DiskChoicesReadGrow(d.pop("grow"))

        disk_choices_read = cls(
            volume_min_gb=volume_min_gb,
            volume_max_gb=volume_max_gb,
            volume_default_gb=volume_default_gb,
            container_gb=container_gb,
            volume_billed_while_stopped=volume_billed_while_stopped,
            grow=grow,
        )

        disk_choices_read.additional_properties = d
        return disk_choices_read

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
