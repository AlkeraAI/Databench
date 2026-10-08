from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast
from uuid import UUID

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.invitation_status import InvitationStatus
from ..models.team_role import TeamRole
from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.invitation_refusal import InvitationRefusal


T = TypeVar("T", bound="RecipientInvitationRead")


@_attrs_define
class RecipientInvitationRead:
    """One of the caller's own pending invitations, as the recipient sees it.

    Carries the destination by name (a team in another organization is not in
    the caller's team list, so the client cannot resolve it), who sent it, and —
    when the account cannot accept it — the refusal the accept route would give.
    A refused invitation is still listed so its recipient can read why and
    decline it; only the owner of the address ever sees this shape.

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
            team_name (str):
            org_name (str):
            resolved_at (datetime.datetime | None | Unset):
            invited_by_id (None | Unset | UUID):
            role_display (str | Unset):  Default: ''.
            inviter_display_name (None | str | Unset):
            refusal (InvitationRefusal | None | Unset):
    """

    id: UUID
    team_id: UUID
    email: str
    role: TeamRole
    status: InvitationStatus
    expires_at: datetime.datetime
    created_at: datetime.datetime
    team_name: str
    org_name: str
    resolved_at: datetime.datetime | None | Unset = UNSET
    invited_by_id: None | Unset | UUID = UNSET
    role_display: str | Unset = ""
    inviter_display_name: None | str | Unset = UNSET
    refusal: InvitationRefusal | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.invitation_refusal import InvitationRefusal

        id = str(self.id)

        team_id = str(self.team_id)

        email = self.email

        role = self.role.value

        status = self.status.value

        expires_at = self.expires_at.isoformat()

        created_at = self.created_at.isoformat()

        team_name = self.team_name

        org_name = self.org_name

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

        inviter_display_name: None | str | Unset
        if isinstance(self.inviter_display_name, Unset):
            inviter_display_name = UNSET
        else:
            inviter_display_name = self.inviter_display_name

        refusal: dict[str, Any] | None | Unset
        if isinstance(self.refusal, Unset):
            refusal = UNSET
        elif isinstance(self.refusal, InvitationRefusal):
            refusal = self.refusal.to_dict()
        else:
            refusal = self.refusal

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
                "team_name": team_name,
                "org_name": org_name,
            }
        )
        if resolved_at is not UNSET:
            field_dict["resolved_at"] = resolved_at
        if invited_by_id is not UNSET:
            field_dict["invited_by_id"] = invited_by_id
        if role_display is not UNSET:
            field_dict["role_display"] = role_display
        if inviter_display_name is not UNSET:
            field_dict["inviter_display_name"] = inviter_display_name
        if refusal is not UNSET:
            field_dict["refusal"] = refusal

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.invitation_refusal import InvitationRefusal

        d = dict(src_dict)
        id = UUID(d.pop("id"))

        team_id = UUID(d.pop("team_id"))

        email = d.pop("email")

        role = TeamRole(d.pop("role"))

        status = InvitationStatus(d.pop("status"))

        expires_at = datetime.datetime.fromisoformat(d.pop("expires_at"))

        created_at = datetime.datetime.fromisoformat(d.pop("created_at"))

        team_name = d.pop("team_name")

        org_name = d.pop("org_name")

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

        def _parse_inviter_display_name(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        inviter_display_name = _parse_inviter_display_name(d.pop("inviter_display_name", UNSET))

        def _parse_refusal(data: object) -> InvitationRefusal | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                refusal_type_0 = InvitationRefusal.from_dict(data)

                return refusal_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(InvitationRefusal | None | Unset, data)

        refusal = _parse_refusal(d.pop("refusal", UNSET))

        recipient_invitation_read = cls(
            id=id,
            team_id=team_id,
            email=email,
            role=role,
            status=status,
            expires_at=expires_at,
            created_at=created_at,
            team_name=team_name,
            org_name=org_name,
            resolved_at=resolved_at,
            invited_by_id=invited_by_id,
            role_display=role_display,
            inviter_display_name=inviter_display_name,
            refusal=refusal,
        )

        recipient_invitation_read.additional_properties = d
        return recipient_invitation_read

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
