from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast
from uuid import UUID

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="LeaveOrgResponse")


@_attrs_define
class LeaveOrgResponse:
    """`POST /orgs/current/leave`: the org the client should switch into next
    (the most recently used one the person can still enter), or null when the
    person has none left. Nothing is signed in by this answer.

        Attributes:
            next_org_team_id (None | Unset | UUID):
    """

    next_org_team_id: None | Unset | UUID = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        next_org_team_id: None | str | Unset
        if isinstance(self.next_org_team_id, Unset):
            next_org_team_id = UNSET
        elif isinstance(self.next_org_team_id, UUID):
            next_org_team_id = str(self.next_org_team_id)
        else:
            next_org_team_id = self.next_org_team_id

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if next_org_team_id is not UNSET:
            field_dict["next_org_team_id"] = next_org_team_id

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)

        def _parse_next_org_team_id(data: object) -> None | Unset | UUID:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                next_org_team_id_type_0 = UUID(data)

                return next_org_team_id_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | Unset | UUID, data)

        next_org_team_id = _parse_next_org_team_id(d.pop("next_org_team_id", UNSET))

        leave_org_response = cls(
            next_org_team_id=next_org_team_id,
        )

        leave_org_response.additional_properties = d
        return leave_org_response

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
