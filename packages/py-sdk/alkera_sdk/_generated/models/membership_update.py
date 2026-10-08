from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.team_role import TeamRole

T = TypeVar("T", bound="MembershipUpdate")


@_attrs_define
class MembershipUpdate:
    """
    Attributes:
        role (TeamRole): Role of a user within a specific team (per `team_memberships`).

            `ADMIN` of the org's root team is what the spec calls 'Org Admin';
            `ADMIN` of any non-root team is 'Team Admin'.
    """

    role: TeamRole
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        role = self.role.value

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "role": role,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        role = TeamRole(d.pop("role"))

        membership_update = cls(
            role=role,
        )

        membership_update.additional_properties = d
        return membership_update

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
