from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="OrgComputeAssignmentUpdate")


@_attrs_define
class OrgComputeAssignmentUpdate:
    """Point an org's chats at one dedicated box, or return it to the pool.

    Attributes:
        machine_id (None | str | Unset):
        fallback_to_pool (bool | Unset):  Default: False.
    """

    machine_id: None | str | Unset = UNSET
    fallback_to_pool: bool | Unset = False
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        machine_id: None | str | Unset
        if isinstance(self.machine_id, Unset):
            machine_id = UNSET
        else:
            machine_id = self.machine_id

        fallback_to_pool = self.fallback_to_pool

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if machine_id is not UNSET:
            field_dict["machine_id"] = machine_id
        if fallback_to_pool is not UNSET:
            field_dict["fallback_to_pool"] = fallback_to_pool

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)

        def _parse_machine_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        machine_id = _parse_machine_id(d.pop("machine_id", UNSET))

        fallback_to_pool = d.pop("fallback_to_pool", UNSET)

        org_compute_assignment_update = cls(
            machine_id=machine_id,
            fallback_to_pool=fallback_to_pool,
        )

        org_compute_assignment_update.additional_properties = d
        return org_compute_assignment_update

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
