from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="ComputeGrantRead")


@_attrs_define
class ComputeGrantRead:
    """One live compute grant held by an org (or by a team inside it).

    Attributes:
        id (str):
        org_team_id (str):
        ceiling (int):
        rate_per_minute_nanos (int):
        expires_at (datetime.datetime):
        created_at (datetime.datetime):
        team_name (str | Unset):  Default: ''.
        machine_type_id (None | str | Unset):
        machine_type (str | Unset):  Default: 'any'.
        machine_type_display_name (str | Unset):  Default: 'Any machine type'.
        per_user_max (int | None | Unset):
        note (str | Unset):  Default: ''.
    """

    id: str
    org_team_id: str
    ceiling: int
    rate_per_minute_nanos: int
    expires_at: datetime.datetime
    created_at: datetime.datetime
    team_name: str | Unset = ""
    machine_type_id: None | str | Unset = UNSET
    machine_type: str | Unset = "any"
    machine_type_display_name: str | Unset = "Any machine type"
    per_user_max: int | None | Unset = UNSET
    note: str | Unset = ""
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        id = self.id

        org_team_id = self.org_team_id

        ceiling = self.ceiling

        rate_per_minute_nanos = self.rate_per_minute_nanos

        expires_at = self.expires_at.isoformat()

        created_at = self.created_at.isoformat()

        team_name = self.team_name

        machine_type_id: None | str | Unset
        if isinstance(self.machine_type_id, Unset):
            machine_type_id = UNSET
        else:
            machine_type_id = self.machine_type_id

        machine_type = self.machine_type

        machine_type_display_name = self.machine_type_display_name

        per_user_max: int | None | Unset
        if isinstance(self.per_user_max, Unset):
            per_user_max = UNSET
        else:
            per_user_max = self.per_user_max

        note = self.note

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "org_team_id": org_team_id,
                "ceiling": ceiling,
                "rate_per_minute_nanos": rate_per_minute_nanos,
                "expires_at": expires_at,
                "created_at": created_at,
            }
        )
        if team_name is not UNSET:
            field_dict["team_name"] = team_name
        if machine_type_id is not UNSET:
            field_dict["machine_type_id"] = machine_type_id
        if machine_type is not UNSET:
            field_dict["machine_type"] = machine_type
        if machine_type_display_name is not UNSET:
            field_dict["machine_type_display_name"] = machine_type_display_name
        if per_user_max is not UNSET:
            field_dict["per_user_max"] = per_user_max
        if note is not UNSET:
            field_dict["note"] = note

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        id = d.pop("id")

        org_team_id = d.pop("org_team_id")

        ceiling = d.pop("ceiling")

        rate_per_minute_nanos = d.pop("rate_per_minute_nanos")

        expires_at = datetime.datetime.fromisoformat(d.pop("expires_at"))

        created_at = datetime.datetime.fromisoformat(d.pop("created_at"))

        team_name = d.pop("team_name", UNSET)

        def _parse_machine_type_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        machine_type_id = _parse_machine_type_id(d.pop("machine_type_id", UNSET))

        machine_type = d.pop("machine_type", UNSET)

        machine_type_display_name = d.pop("machine_type_display_name", UNSET)

        def _parse_per_user_max(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        per_user_max = _parse_per_user_max(d.pop("per_user_max", UNSET))

        note = d.pop("note", UNSET)

        compute_grant_read = cls(
            id=id,
            org_team_id=org_team_id,
            ceiling=ceiling,
            rate_per_minute_nanos=rate_per_minute_nanos,
            expires_at=expires_at,
            created_at=created_at,
            team_name=team_name,
            machine_type_id=machine_type_id,
            machine_type=machine_type,
            machine_type_display_name=machine_type_display_name,
            per_user_max=per_user_max,
            note=note,
        )

        compute_grant_read.additional_properties = d
        return compute_grant_read

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
