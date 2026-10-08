from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar
from uuid import UUID

from attrs import define as _attrs_define
from attrs import field as _attrs_field

T = TypeVar("T", bound="SwitchOrgRequest")


@_attrs_define
class SwitchOrgRequest:
    """`POST /auth/refresh/org`: the org to switch this browser into. Checked
    against the caller's own active memberships; it never selects anything a
    membership does not already grant.

        Attributes:
            org_team_id (UUID):
    """

    org_team_id: UUID
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        org_team_id = str(self.org_team_id)

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "org_team_id": org_team_id,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        org_team_id = UUID(d.pop("org_team_id"))

        switch_org_request = cls(
            org_team_id=org_team_id,
        )

        switch_org_request.additional_properties = d
        return switch_org_request

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
