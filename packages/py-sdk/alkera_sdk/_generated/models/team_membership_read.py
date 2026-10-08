from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import Any, TypeVar
from uuid import UUID

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.team_role import TeamRole
from ..types import UNSET, Unset

T = TypeVar("T", bound="TeamMembershipRead")


@_attrs_define
class TeamMembershipRead:
    """
    Attributes:
        user_id (UUID):
        team_id (UUID):
        id (UUID):
        created_at (datetime.datetime):
        role (TeamRole | Unset): Role of a user within a specific team (per `team_memberships`).

            `ADMIN` of the org's root team is what the spec calls 'Org Admin';
            `ADMIN` of any non-root team is 'Team Admin'.
        role_display (str | Unset):  Default: ''.
    """

    user_id: UUID
    team_id: UUID
    id: UUID
    created_at: datetime.datetime
    role: TeamRole | Unset = UNSET
    role_display: str | Unset = ""
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        user_id = str(self.user_id)

        team_id = str(self.team_id)

        id = str(self.id)

        created_at = self.created_at.isoformat()

        role: str | Unset = UNSET
        if not isinstance(self.role, Unset):
            role = self.role.value

        role_display = self.role_display

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "user_id": user_id,
                "team_id": team_id,
                "id": id,
                "created_at": created_at,
            }
        )
        if role is not UNSET:
            field_dict["role"] = role
        if role_display is not UNSET:
            field_dict["role_display"] = role_display

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        user_id = UUID(d.pop("user_id"))

        team_id = UUID(d.pop("team_id"))

        id = UUID(d.pop("id"))

        created_at = datetime.datetime.fromisoformat(d.pop("created_at"))

        _role = d.pop("role", UNSET)
        role: TeamRole | Unset
        if isinstance(_role, Unset):
            role = UNSET
        else:
            role = TeamRole(_role)

        role_display = d.pop("role_display", UNSET)

        team_membership_read = cls(
            user_id=user_id,
            team_id=team_id,
            id=id,
            created_at=created_at,
            role=role,
            role_display=role_display,
        )

        team_membership_read.additional_properties = d
        return team_membership_read

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
