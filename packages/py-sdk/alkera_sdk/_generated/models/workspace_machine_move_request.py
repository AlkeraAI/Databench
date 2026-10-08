from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="WorkspaceMachineMoveRequest")


@_attrs_define
class WorkspaceMachineMoveRequest:
    """
    Attributes:
        to_org_machine_id (None | str | Unset):
        stop_running (bool | Unset):  Default: False.
    """

    to_org_machine_id: None | str | Unset = UNSET
    stop_running: bool | Unset = False
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        to_org_machine_id: None | str | Unset
        if isinstance(self.to_org_machine_id, Unset):
            to_org_machine_id = UNSET
        else:
            to_org_machine_id = self.to_org_machine_id

        stop_running = self.stop_running

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if to_org_machine_id is not UNSET:
            field_dict["to_org_machine_id"] = to_org_machine_id
        if stop_running is not UNSET:
            field_dict["stop_running"] = stop_running

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)

        def _parse_to_org_machine_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        to_org_machine_id = _parse_to_org_machine_id(d.pop("to_org_machine_id", UNSET))

        stop_running = d.pop("stop_running", UNSET)

        workspace_machine_move_request = cls(
            to_org_machine_id=to_org_machine_id,
            stop_running=stop_running,
        )

        workspace_machine_move_request.additional_properties = d
        return workspace_machine_move_request

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
