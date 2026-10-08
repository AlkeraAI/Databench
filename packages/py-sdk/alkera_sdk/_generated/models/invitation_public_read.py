from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import Any, TypeVar, cast
from uuid import UUID

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.team_role import TeamRole
from ..types import UNSET, Unset

T = TypeVar("T", bound="InvitationPublicRead")


@_attrs_define
class InvitationPublicRead:
    """No-auth preview shape used by the signup page when a token is in the
    URL. Returns enough to render 'Join {org}/{team}, invited by {inviter}'
    without leaking other invitations or the inviter's email.

    `email` is the invited address. Signup against this token only succeeds for
    that exact address, so the page prefills it rather than asking the invitee to
    retype it; the token was mailed there, so its holder already knows it.

        Attributes:
            email (str):
            team_id (UUID):
            team_name (str):
            org_team_id (UUID):
            org_name (str):
            role (TeamRole): Role of a user within a specific team (per `team_memberships`).

                `ADMIN` of the org's root team is what the spec calls 'Org Admin';
                `ADMIN` of any non-root team is 'Team Admin'.
            expires_at (datetime.datetime):
            inviter_display_name (None | str | Unset):
    """

    email: str
    team_id: UUID
    team_name: str
    org_team_id: UUID
    org_name: str
    role: TeamRole
    expires_at: datetime.datetime
    inviter_display_name: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        email = self.email

        team_id = str(self.team_id)

        team_name = self.team_name

        org_team_id = str(self.org_team_id)

        org_name = self.org_name

        role = self.role.value

        expires_at = self.expires_at.isoformat()

        inviter_display_name: None | str | Unset
        if isinstance(self.inviter_display_name, Unset):
            inviter_display_name = UNSET
        else:
            inviter_display_name = self.inviter_display_name

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "email": email,
                "team_id": team_id,
                "team_name": team_name,
                "org_team_id": org_team_id,
                "org_name": org_name,
                "role": role,
                "expires_at": expires_at,
            }
        )
        if inviter_display_name is not UNSET:
            field_dict["inviter_display_name"] = inviter_display_name

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        email = d.pop("email")

        team_id = UUID(d.pop("team_id"))

        team_name = d.pop("team_name")

        org_team_id = UUID(d.pop("org_team_id"))

        org_name = d.pop("org_name")

        role = TeamRole(d.pop("role"))

        expires_at = datetime.datetime.fromisoformat(d.pop("expires_at"))

        def _parse_inviter_display_name(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        inviter_display_name = _parse_inviter_display_name(d.pop("inviter_display_name", UNSET))

        invitation_public_read = cls(
            email=email,
            team_id=team_id,
            team_name=team_name,
            org_team_id=org_team_id,
            org_name=org_name,
            role=role,
            expires_at=expires_at,
            inviter_display_name=inviter_display_name,
        )

        invitation_public_read.additional_properties = d
        return invitation_public_read

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
