from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import Any, TypeVar, cast
from uuid import UUID

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.team_role import TeamRole
from ..types import UNSET, Unset

T = TypeVar("T", bound="TeamMemberRead")


@_attrs_define
class TeamMemberRead:
    """One person's standing on one team, with their identity and the team's
    name, so the member table needs no client-side join.

    A person stands on a team two ways, and the two are not exclusive: a
    membership row written on the team itself (``direct_role``), and admin
    reaching the team from a team above it (``descent_role``, carried from
    ``descent_from_team_*`` — the nearest ancestor holding the admin row). A
    person can hold both; a person can hold only one. ``role`` is what they
    hold here once descent is applied — admin when either side is admin, else
    the direct row's role — and ``effective_role`` names the same value for
    what it is. Descent is decided server-side so no client recomputes it.

        Attributes:
            user_id (UUID):
            display_name (str):
            email (str):
            first_name (str):
            last_name (str):
            role (TeamRole): Role of a user within a specific team (per `team_memberships`).

                `ADMIN` of the org's root team is what the spec calls 'Org Admin';
                `ADMIN` of any non-root team is 'Team Admin'.
            team_id (UUID):
            team_name (str):
            created_at (datetime.datetime):
            direct_role (None | TeamRole | Unset):
            descent_role (None | TeamRole | Unset):
            descent_from_team_id (None | Unset | UUID):
            descent_from_team_name (None | str | Unset):
            role_display (str | Unset):  Default: ''.
            effective_role (TeamRole | Unset): Role of a user within a specific team (per `team_memberships`).

                `ADMIN` of the org's root team is what the spec calls 'Org Admin';
                `ADMIN` of any non-root team is 'Team Admin'.
    """

    user_id: UUID
    display_name: str
    email: str
    first_name: str
    last_name: str
    role: TeamRole
    team_id: UUID
    team_name: str
    created_at: datetime.datetime
    direct_role: None | TeamRole | Unset = UNSET
    descent_role: None | TeamRole | Unset = UNSET
    descent_from_team_id: None | Unset | UUID = UNSET
    descent_from_team_name: None | str | Unset = UNSET
    role_display: str | Unset = ""
    effective_role: TeamRole | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        user_id = str(self.user_id)

        display_name = self.display_name

        email = self.email

        first_name = self.first_name

        last_name = self.last_name

        role = self.role.value

        team_id = str(self.team_id)

        team_name = self.team_name

        created_at = self.created_at.isoformat()

        direct_role: None | str | Unset
        if isinstance(self.direct_role, Unset):
            direct_role = UNSET
        elif isinstance(self.direct_role, TeamRole):
            direct_role = self.direct_role.value
        else:
            direct_role = self.direct_role

        descent_role: None | str | Unset
        if isinstance(self.descent_role, Unset):
            descent_role = UNSET
        elif isinstance(self.descent_role, TeamRole):
            descent_role = self.descent_role.value
        else:
            descent_role = self.descent_role

        descent_from_team_id: None | str | Unset
        if isinstance(self.descent_from_team_id, Unset):
            descent_from_team_id = UNSET
        elif isinstance(self.descent_from_team_id, UUID):
            descent_from_team_id = str(self.descent_from_team_id)
        else:
            descent_from_team_id = self.descent_from_team_id

        descent_from_team_name: None | str | Unset
        if isinstance(self.descent_from_team_name, Unset):
            descent_from_team_name = UNSET
        else:
            descent_from_team_name = self.descent_from_team_name

        role_display = self.role_display

        effective_role: str | Unset = UNSET
        if not isinstance(self.effective_role, Unset):
            effective_role = self.effective_role.value

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "user_id": user_id,
                "display_name": display_name,
                "email": email,
                "first_name": first_name,
                "last_name": last_name,
                "role": role,
                "team_id": team_id,
                "team_name": team_name,
                "created_at": created_at,
            }
        )
        if direct_role is not UNSET:
            field_dict["direct_role"] = direct_role
        if descent_role is not UNSET:
            field_dict["descent_role"] = descent_role
        if descent_from_team_id is not UNSET:
            field_dict["descent_from_team_id"] = descent_from_team_id
        if descent_from_team_name is not UNSET:
            field_dict["descent_from_team_name"] = descent_from_team_name
        if role_display is not UNSET:
            field_dict["role_display"] = role_display
        if effective_role is not UNSET:
            field_dict["effective_role"] = effective_role

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        user_id = UUID(d.pop("user_id"))

        display_name = d.pop("display_name")

        email = d.pop("email")

        first_name = d.pop("first_name")

        last_name = d.pop("last_name")

        role = TeamRole(d.pop("role"))

        team_id = UUID(d.pop("team_id"))

        team_name = d.pop("team_name")

        created_at = datetime.datetime.fromisoformat(d.pop("created_at"))

        def _parse_direct_role(data: object) -> None | TeamRole | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                direct_role_type_0 = TeamRole(data)

                return direct_role_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | TeamRole | Unset, data)

        direct_role = _parse_direct_role(d.pop("direct_role", UNSET))

        def _parse_descent_role(data: object) -> None | TeamRole | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                descent_role_type_0 = TeamRole(data)

                return descent_role_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | TeamRole | Unset, data)

        descent_role = _parse_descent_role(d.pop("descent_role", UNSET))

        def _parse_descent_from_team_id(data: object) -> None | Unset | UUID:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                descent_from_team_id_type_0 = UUID(data)

                return descent_from_team_id_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | Unset | UUID, data)

        descent_from_team_id = _parse_descent_from_team_id(d.pop("descent_from_team_id", UNSET))

        def _parse_descent_from_team_name(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        descent_from_team_name = _parse_descent_from_team_name(
            d.pop("descent_from_team_name", UNSET)
        )

        role_display = d.pop("role_display", UNSET)

        _effective_role = d.pop("effective_role", UNSET)
        effective_role: TeamRole | Unset
        if isinstance(_effective_role, Unset):
            effective_role = UNSET
        else:
            effective_role = TeamRole(_effective_role)

        team_member_read = cls(
            user_id=user_id,
            display_name=display_name,
            email=email,
            first_name=first_name,
            last_name=last_name,
            role=role,
            team_id=team_id,
            team_name=team_name,
            created_at=created_at,
            direct_role=direct_role,
            descent_role=descent_role,
            descent_from_team_id=descent_from_team_id,
            descent_from_team_name=descent_from_team_name,
            role_display=role_display,
            effective_role=effective_role,
        )

        team_member_read.additional_properties = d
        return team_member_read

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
