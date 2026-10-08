from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="ComputeAllocationCreateRequest")


@_attrs_define
class ComputeAllocationCreateRequest:
    """
    Attributes:
        machine_type_id (str):
        project_path (str | Unset):  Default: ''.
        session_id (str | Unset):  Default: ''.
        max_minutes (int | None | Unset):
    """

    machine_type_id: str
    project_path: str | Unset = ""
    session_id: str | Unset = ""
    max_minutes: int | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        machine_type_id = self.machine_type_id

        project_path = self.project_path

        session_id = self.session_id

        max_minutes: int | None | Unset
        if isinstance(self.max_minutes, Unset):
            max_minutes = UNSET
        else:
            max_minutes = self.max_minutes

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "machine_type_id": machine_type_id,
            }
        )
        if project_path is not UNSET:
            field_dict["project_path"] = project_path
        if session_id is not UNSET:
            field_dict["session_id"] = session_id
        if max_minutes is not UNSET:
            field_dict["max_minutes"] = max_minutes

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        machine_type_id = d.pop("machine_type_id")

        project_path = d.pop("project_path", UNSET)

        session_id = d.pop("session_id", UNSET)

        def _parse_max_minutes(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        max_minutes = _parse_max_minutes(d.pop("max_minutes", UNSET))

        compute_allocation_create_request = cls(
            machine_type_id=machine_type_id,
            project_path=project_path,
            session_id=session_id,
            max_minutes=max_minutes,
        )

        compute_allocation_create_request.additional_properties = d
        return compute_allocation_create_request

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
