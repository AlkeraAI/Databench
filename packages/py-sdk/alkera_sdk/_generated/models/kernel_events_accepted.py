from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

T = TypeVar("T", bound="KernelEventsAccepted")


@_attrs_define
class KernelEventsAccepted:
    """
    Attributes:
        kernel_id (str):
        accepted (int):
        seq (int):
    """

    kernel_id: str
    accepted: int
    seq: int
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        kernel_id = self.kernel_id

        accepted = self.accepted

        seq = self.seq

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "kernel_id": kernel_id,
                "accepted": accepted,
                "seq": seq,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        kernel_id = d.pop("kernel_id")

        accepted = d.pop("accepted")

        seq = d.pop("seq")

        kernel_events_accepted = cls(
            kernel_id=kernel_id,
            accepted=accepted,
            seq=seq,
        )

        kernel_events_accepted.additional_properties = d
        return kernel_events_accepted

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
