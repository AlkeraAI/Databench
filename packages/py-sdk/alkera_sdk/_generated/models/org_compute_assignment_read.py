from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.platform_machine_read import PlatformMachineRead


T = TypeVar("T", bound="OrgComputeAssignmentRead")


@_attrs_define
class OrgComputeAssignmentRead:
    """
    Attributes:
        org_team_id (str):
        machine_id (None | str | Unset):
        fallback_to_pool (bool | Unset):  Default: False.
        machine (None | PlatformMachineRead | Unset):
        assigned_at (datetime.datetime | None | Unset):
    """

    org_team_id: str
    machine_id: None | str | Unset = UNSET
    fallback_to_pool: bool | Unset = False
    machine: None | PlatformMachineRead | Unset = UNSET
    assigned_at: datetime.datetime | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.platform_machine_read import PlatformMachineRead

        org_team_id = self.org_team_id

        machine_id: None | str | Unset
        if isinstance(self.machine_id, Unset):
            machine_id = UNSET
        else:
            machine_id = self.machine_id

        fallback_to_pool = self.fallback_to_pool

        machine: dict[str, Any] | None | Unset
        if isinstance(self.machine, Unset):
            machine = UNSET
        elif isinstance(self.machine, PlatformMachineRead):
            machine = self.machine.to_dict()
        else:
            machine = self.machine

        assigned_at: None | str | Unset
        if isinstance(self.assigned_at, Unset):
            assigned_at = UNSET
        elif isinstance(self.assigned_at, datetime.datetime):
            assigned_at = self.assigned_at.isoformat()
        else:
            assigned_at = self.assigned_at

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "org_team_id": org_team_id,
            }
        )
        if machine_id is not UNSET:
            field_dict["machine_id"] = machine_id
        if fallback_to_pool is not UNSET:
            field_dict["fallback_to_pool"] = fallback_to_pool
        if machine is not UNSET:
            field_dict["machine"] = machine
        if assigned_at is not UNSET:
            field_dict["assigned_at"] = assigned_at

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.platform_machine_read import PlatformMachineRead

        d = dict(src_dict)
        org_team_id = d.pop("org_team_id")

        def _parse_machine_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        machine_id = _parse_machine_id(d.pop("machine_id", UNSET))

        fallback_to_pool = d.pop("fallback_to_pool", UNSET)

        def _parse_machine(data: object) -> None | PlatformMachineRead | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                machine_type_0 = PlatformMachineRead.from_dict(data)

                return machine_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | PlatformMachineRead | Unset, data)

        machine = _parse_machine(d.pop("machine", UNSET))

        def _parse_assigned_at(data: object) -> datetime.datetime | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                assigned_at_type_0 = datetime.datetime.fromisoformat(data)

                return assigned_at_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None | Unset, data)

        assigned_at = _parse_assigned_at(d.pop("assigned_at", UNSET))

        org_compute_assignment_read = cls(
            org_team_id=org_team_id,
            machine_id=machine_id,
            fallback_to_pool=fallback_to_pool,
            machine=machine,
            assigned_at=assigned_at,
        )

        org_compute_assignment_read.additional_properties = d
        return org_compute_assignment_read

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
