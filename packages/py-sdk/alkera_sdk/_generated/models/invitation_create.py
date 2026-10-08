from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.team_role import TeamRole
from ..types import UNSET, Unset

T = TypeVar("T", bound="InvitationCreate")


@_attrs_define
class InvitationCreate:
    """
    Attributes:
        email (str):
        role (TeamRole | Unset): Role of a user within a specific team (per `team_memberships`).

            `ADMIN` of the org's root team is what the spec calls 'Org Admin';
            `ADMIN` of any non-root team is 'Team Admin'.
    """

    email: str
    role: TeamRole | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        email = self.email

        role: str | Unset = UNSET
        if not isinstance(self.role, Unset):
            role = self.role.value

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "email": email,
            }
        )
        if role is not UNSET:
            field_dict["role"] = role

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        email = d.pop("email")

        _role = d.pop("role", UNSET)
        role: TeamRole | Unset
        if isinstance(_role, Unset):
            role = UNSET
        else:
            role = TeamRole(_role)

        invitation_create = cls(
            email=email,
            role=role,
        )

        invitation_create.additional_properties = d
        return invitation_create

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
