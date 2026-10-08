from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.lost_machine_read_reason import LostMachineReadReason

T = TypeVar("T", bound="LostMachineRead")


@_attrs_define
class LostMachineRead:
    """The org machine a workspace ran on that can no longer serve it: deleted,
    or out of reach of the person whose chat would run there. ``fell_back``:
    a waker that is not a person already moved the workspace to the default
    placement, which the workspace's card now shows.

        Attributes:
            org_machine_id (str):
            name (str):
            reason (LostMachineReadReason):
            fell_back (bool):
    """

    org_machine_id: str
    name: str
    reason: LostMachineReadReason
    fell_back: bool
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        org_machine_id = self.org_machine_id

        name = self.name

        reason = self.reason.value

        fell_back = self.fell_back

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "org_machine_id": org_machine_id,
                "name": name,
                "reason": reason,
                "fell_back": fell_back,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        org_machine_id = d.pop("org_machine_id")

        name = d.pop("name")

        reason = LostMachineReadReason(d.pop("reason"))

        fell_back = d.pop("fell_back")

        lost_machine_read = cls(
            org_machine_id=org_machine_id,
            name=name,
            reason=reason,
            fell_back=fell_back,
        )

        lost_machine_read.additional_properties = d
        return lost_machine_read

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
