from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.workspace_machine_move_read_state import WorkspaceMachineMoveReadState
from ..types import UNSET, Unset

T = TypeVar("T", bound="WorkspaceMachineMoveRead")


@_attrs_define
class WorkspaceMachineMoveRead:
    """
    Attributes:
        id (str):
        workspace_id (str):
        state (WorkspaceMachineMoveReadState):
        requested_at (datetime.datetime):
        from_org_machine_id (None | str | Unset):
        to_org_machine_id (None | str | Unset):
        error_code (str | Unset):  Default: ''.
        error (str | Unset):  Default: ''.
        finished_at (datetime.datetime | None | Unset):
        flushed_before_switch (bool | None | Unset):
    """

    id: str
    workspace_id: str
    state: WorkspaceMachineMoveReadState
    requested_at: datetime.datetime
    from_org_machine_id: None | str | Unset = UNSET
    to_org_machine_id: None | str | Unset = UNSET
    error_code: str | Unset = ""
    error: str | Unset = ""
    finished_at: datetime.datetime | None | Unset = UNSET
    flushed_before_switch: bool | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        id = self.id

        workspace_id = self.workspace_id

        state = self.state.value

        requested_at = self.requested_at.isoformat()

        from_org_machine_id: None | str | Unset
        if isinstance(self.from_org_machine_id, Unset):
            from_org_machine_id = UNSET
        else:
            from_org_machine_id = self.from_org_machine_id

        to_org_machine_id: None | str | Unset
        if isinstance(self.to_org_machine_id, Unset):
            to_org_machine_id = UNSET
        else:
            to_org_machine_id = self.to_org_machine_id

        error_code = self.error_code

        error = self.error

        finished_at: None | str | Unset
        if isinstance(self.finished_at, Unset):
            finished_at = UNSET
        elif isinstance(self.finished_at, datetime.datetime):
            finished_at = self.finished_at.isoformat()
        else:
            finished_at = self.finished_at

        flushed_before_switch: bool | None | Unset
        if isinstance(self.flushed_before_switch, Unset):
            flushed_before_switch = UNSET
        else:
            flushed_before_switch = self.flushed_before_switch

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "workspace_id": workspace_id,
                "state": state,
                "requested_at": requested_at,
            }
        )
        if from_org_machine_id is not UNSET:
            field_dict["from_org_machine_id"] = from_org_machine_id
        if to_org_machine_id is not UNSET:
            field_dict["to_org_machine_id"] = to_org_machine_id
        if error_code is not UNSET:
            field_dict["error_code"] = error_code
        if error is not UNSET:
            field_dict["error"] = error
        if finished_at is not UNSET:
            field_dict["finished_at"] = finished_at
        if flushed_before_switch is not UNSET:
            field_dict["flushed_before_switch"] = flushed_before_switch

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        id = d.pop("id")

        workspace_id = d.pop("workspace_id")

        state = WorkspaceMachineMoveReadState(d.pop("state"))

        requested_at = datetime.datetime.fromisoformat(d.pop("requested_at"))

        def _parse_from_org_machine_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        from_org_machine_id = _parse_from_org_machine_id(d.pop("from_org_machine_id", UNSET))

        def _parse_to_org_machine_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        to_org_machine_id = _parse_to_org_machine_id(d.pop("to_org_machine_id", UNSET))

        error_code = d.pop("error_code", UNSET)

        error = d.pop("error", UNSET)

        def _parse_finished_at(data: object) -> datetime.datetime | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                finished_at_type_0 = datetime.datetime.fromisoformat(data)

                return finished_at_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None | Unset, data)

        finished_at = _parse_finished_at(d.pop("finished_at", UNSET))

        def _parse_flushed_before_switch(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        flushed_before_switch = _parse_flushed_before_switch(d.pop("flushed_before_switch", UNSET))

        workspace_machine_move_read = cls(
            id=id,
            workspace_id=workspace_id,
            state=state,
            requested_at=requested_at,
            from_org_machine_id=from_org_machine_id,
            to_org_machine_id=to_org_machine_id,
            error_code=error_code,
            error=error,
            finished_at=finished_at,
            flushed_before_switch=flushed_before_switch,
        )

        workspace_machine_move_read.additional_properties = d
        return workspace_machine_move_read

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
