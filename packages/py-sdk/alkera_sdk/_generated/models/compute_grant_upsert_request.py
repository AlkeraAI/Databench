from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import Any, TypeVar, cast
from uuid import UUID

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="ComputeGrantUpsertRequest")


@_attrs_define
class ComputeGrantUpsertRequest:
    """Grant an org compute, or bring its live grant up to date.

    Idempotent on ``(team, machine type)``: a second call with the same target
    updates the live grant instead of stacking a second one.

        Attributes:
            ceiling (int):
            expires_at (datetime.datetime):
            org_team_id (None | Unset | UUID):
            machine_type_id (None | Unset | UUID):
            per_user_max (int | None | Unset):
            rate_per_minute_nanos (int | Unset):  Default: 0.
            note (str | Unset):  Default: ''.
    """

    ceiling: int
    expires_at: datetime.datetime
    org_team_id: None | Unset | UUID = UNSET
    machine_type_id: None | Unset | UUID = UNSET
    per_user_max: int | None | Unset = UNSET
    rate_per_minute_nanos: int | Unset = 0
    note: str | Unset = ""
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        ceiling = self.ceiling

        expires_at = self.expires_at.isoformat()

        org_team_id: None | str | Unset
        if isinstance(self.org_team_id, Unset):
            org_team_id = UNSET
        elif isinstance(self.org_team_id, UUID):
            org_team_id = str(self.org_team_id)
        else:
            org_team_id = self.org_team_id

        machine_type_id: None | str | Unset
        if isinstance(self.machine_type_id, Unset):
            machine_type_id = UNSET
        elif isinstance(self.machine_type_id, UUID):
            machine_type_id = str(self.machine_type_id)
        else:
            machine_type_id = self.machine_type_id

        per_user_max: int | None | Unset
        if isinstance(self.per_user_max, Unset):
            per_user_max = UNSET
        else:
            per_user_max = self.per_user_max

        rate_per_minute_nanos = self.rate_per_minute_nanos

        note = self.note

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "ceiling": ceiling,
                "expires_at": expires_at,
            }
        )
        if org_team_id is not UNSET:
            field_dict["org_team_id"] = org_team_id
        if machine_type_id is not UNSET:
            field_dict["machine_type_id"] = machine_type_id
        if per_user_max is not UNSET:
            field_dict["per_user_max"] = per_user_max
        if rate_per_minute_nanos is not UNSET:
            field_dict["rate_per_minute_nanos"] = rate_per_minute_nanos
        if note is not UNSET:
            field_dict["note"] = note

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        ceiling = d.pop("ceiling")

        expires_at = datetime.datetime.fromisoformat(d.pop("expires_at"))

        def _parse_org_team_id(data: object) -> None | Unset | UUID:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                org_team_id_type_0 = UUID(data)

                return org_team_id_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | Unset | UUID, data)

        org_team_id = _parse_org_team_id(d.pop("org_team_id", UNSET))

        def _parse_machine_type_id(data: object) -> None | Unset | UUID:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                machine_type_id_type_0 = UUID(data)

                return machine_type_id_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | Unset | UUID, data)

        machine_type_id = _parse_machine_type_id(d.pop("machine_type_id", UNSET))

        def _parse_per_user_max(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        per_user_max = _parse_per_user_max(d.pop("per_user_max", UNSET))

        rate_per_minute_nanos = d.pop("rate_per_minute_nanos", UNSET)

        note = d.pop("note", UNSET)

        compute_grant_upsert_request = cls(
            ceiling=ceiling,
            expires_at=expires_at,
            org_team_id=org_team_id,
            machine_type_id=machine_type_id,
            per_user_max=per_user_max,
            rate_per_minute_nanos=rate_per_minute_nanos,
            note=note,
        )

        compute_grant_upsert_request.additional_properties = d
        return compute_grant_upsert_request

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
