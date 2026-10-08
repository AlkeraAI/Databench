from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import Any, TypeVar, cast
from uuid import UUID

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.invitation_status import InvitationStatus
from ..models.team_role import TeamRole
from ..types import UNSET, Unset

T = TypeVar("T", bound="InvitationRead")


@_attrs_define
class InvitationRead:
    """Authenticated read shape.

    The raw `token` is intentionally NOT exposed — only its keyed hash is stored,
    so a DB read can't yield a usable invite link. The recipient's
    `/dashboard/invites` page accepts/rejects by `id` (`/{invitation_id}/...`);
    the raw token only ever travels in the emailed `/signup?invite=<token>` link.

        Attributes:
            id (UUID):
            team_id (UUID):
            email (str):
            role (TeamRole): Role of a user within a specific team (per `team_memberships`).

                `ADMIN` of the org's root team is what the spec calls 'Org Admin';
                `ADMIN` of any non-root team is 'Team Admin'.
            status (InvitationStatus): Lifecycle of an `invitations` row.

                `pending` is the only status with effect; others are terminal and exist
                for audit. `expired` may be marked lazily on read.
            expires_at (datetime.datetime):
            created_at (datetime.datetime):
            resolved_at (datetime.datetime | None | Unset):
            invited_by_id (None | Unset | UUID):
            role_display (str | Unset):  Default: ''.
    """

    id: UUID
    team_id: UUID
    email: str
    role: TeamRole
    status: InvitationStatus
    expires_at: datetime.datetime
    created_at: datetime.datetime
    resolved_at: datetime.datetime | None | Unset = UNSET
    invited_by_id: None | Unset | UUID = UNSET
    role_display: str | Unset = ""
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        id = str(self.id)

        team_id = str(self.team_id)

        email = self.email

        role = self.role.value

        status = self.status.value

        expires_at = self.expires_at.isoformat()

        created_at = self.created_at.isoformat()

        resolved_at: None | str | Unset
        if isinstance(self.resolved_at, Unset):
            resolved_at = UNSET
        elif isinstance(self.resolved_at, datetime.datetime):
            resolved_at = self.resolved_at.isoformat()
        else:
            resolved_at = self.resolved_at

        invited_by_id: None | str | Unset
        if isinstance(self.invited_by_id, Unset):
            invited_by_id = UNSET
        elif isinstance(self.invited_by_id, UUID):
            invited_by_id = str(self.invited_by_id)
        else:
            invited_by_id = self.invited_by_id

        role_display = self.role_display

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "team_id": team_id,
                "email": email,
                "role": role,
                "status": status,
                "expires_at": expires_at,
                "created_at": created_at,
            }
        )
        if resolved_at is not UNSET:
            field_dict["resolved_at"] = resolved_at
        if invited_by_id is not UNSET:
            field_dict["invited_by_id"] = invited_by_id
        if role_display is not UNSET:
            field_dict["role_display"] = role_display

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        id = UUID(d.pop("id"))

        team_id = UUID(d.pop("team_id"))

        email = d.pop("email")

        role = TeamRole(d.pop("role"))

        status = InvitationStatus(d.pop("status"))

        expires_at = datetime.datetime.fromisoformat(d.pop("expires_at"))

        created_at = datetime.datetime.fromisoformat(d.pop("created_at"))

        def _parse_resolved_at(data: object) -> datetime.datetime | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                resolved_at_type_0 = datetime.datetime.fromisoformat(data)

                return resolved_at_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None | Unset, data)

        resolved_at = _parse_resolved_at(d.pop("resolved_at", UNSET))

        def _parse_invited_by_id(data: object) -> None | Unset | UUID:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                invited_by_id_type_0 = UUID(data)

                return invited_by_id_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | Unset | UUID, data)

        invited_by_id = _parse_invited_by_id(d.pop("invited_by_id", UNSET))

        role_display = d.pop("role_display", UNSET)

        invitation_read = cls(
            id=id,
            team_id=team_id,
            email=email,
            role=role,
            status=status,
            expires_at=expires_at,
            created_at=created_at,
            resolved_at=resolved_at,
            invited_by_id=invited_by_id,
            role_display=role_display,
        )

        invitation_read.additional_properties = d
        return invitation_read

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
