from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import Any, TypeVar, cast
from uuid import UUID

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.admin_created_user_read_org_role import AdminCreatedUserReadOrgRole
from ..models.platform_role import PlatformRole
from ..types import UNSET, Unset

T = TypeVar("T", bound="AdminCreatedUserRead")


@_attrs_define
class AdminCreatedUserRead:
    """What the deprecated ``POST /api/v1/users`` answers: the ``UserRead`` it
    always answered, plus what happened that the caller did not ask for.

    ``org_team_id`` is the caller's org. ``notes`` names each departure from
    what the request asked (``password_ignored``, ``invitation_pending``).
    ``invitation_id`` is set (and ``id`` is the invitation's) when the address
    belongs to an identity elsewhere and an invitation was sent instead.

        Attributes:
            email (str):
            first_name (str):
            last_name (str):
            id (UUID):
            org_team_id (UUID):
            display_name (str):
            created_at (datetime.datetime):
            org_name (str | Unset):  Default: ''.
            org_role (AdminCreatedUserReadOrgRole | Unset):  Default: AdminCreatedUserReadOrgRole.MEMBER.
            membership_count (int | Unset):  Default: 1.
            platform_role (None | PlatformRole | Unset):
            email_verified_at (datetime.datetime | None | Unset):
            email_verification_required (bool | Unset):  Default: False.
            email_verification_deadline (datetime.datetime | None | Unset):
            verification_resend_available_at (datetime.datetime | None | Unset):
            admin_team_ids (list[UUID] | Unset):
            has_password (bool | Unset):  Default: False.
            mfa_enabled (bool | Unset):  Default: False.
            platform_role_display (None | str | Unset):
            notes (list[str] | Unset):
            invitation_id (None | Unset | UUID):
    """

    email: str
    first_name: str
    last_name: str
    id: UUID
    org_team_id: UUID
    display_name: str
    created_at: datetime.datetime
    org_name: str | Unset = ""
    org_role: AdminCreatedUserReadOrgRole | Unset = AdminCreatedUserReadOrgRole.MEMBER
    membership_count: int | Unset = 1
    platform_role: None | PlatformRole | Unset = UNSET
    email_verified_at: datetime.datetime | None | Unset = UNSET
    email_verification_required: bool | Unset = False
    email_verification_deadline: datetime.datetime | None | Unset = UNSET
    verification_resend_available_at: datetime.datetime | None | Unset = UNSET
    admin_team_ids: list[UUID] | Unset = UNSET
    has_password: bool | Unset = False
    mfa_enabled: bool | Unset = False
    platform_role_display: None | str | Unset = UNSET
    notes: list[str] | Unset = UNSET
    invitation_id: None | Unset | UUID = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        email = self.email

        first_name = self.first_name

        last_name = self.last_name

        id = str(self.id)

        org_team_id = str(self.org_team_id)

        display_name = self.display_name

        created_at = self.created_at.isoformat()

        org_name = self.org_name

        org_role: str | Unset = UNSET
        if not isinstance(self.org_role, Unset):
            org_role = self.org_role.value

        membership_count = self.membership_count

        platform_role: None | str | Unset
        if isinstance(self.platform_role, Unset):
            platform_role = UNSET
        elif isinstance(self.platform_role, PlatformRole):
            platform_role = self.platform_role.value
        else:
            platform_role = self.platform_role

        email_verified_at: None | str | Unset
        if isinstance(self.email_verified_at, Unset):
            email_verified_at = UNSET
        elif isinstance(self.email_verified_at, datetime.datetime):
            email_verified_at = self.email_verified_at.isoformat()
        else:
            email_verified_at = self.email_verified_at

        email_verification_required = self.email_verification_required

        email_verification_deadline: None | str | Unset
        if isinstance(self.email_verification_deadline, Unset):
            email_verification_deadline = UNSET
        elif isinstance(self.email_verification_deadline, datetime.datetime):
            email_verification_deadline = self.email_verification_deadline.isoformat()
        else:
            email_verification_deadline = self.email_verification_deadline

        verification_resend_available_at: None | str | Unset
        if isinstance(self.verification_resend_available_at, Unset):
            verification_resend_available_at = UNSET
        elif isinstance(self.verification_resend_available_at, datetime.datetime):
            verification_resend_available_at = self.verification_resend_available_at.isoformat()
        else:
            verification_resend_available_at = self.verification_resend_available_at

        admin_team_ids: list[str] | Unset = UNSET
        if not isinstance(self.admin_team_ids, Unset):
            admin_team_ids = []
            for admin_team_ids_item_data in self.admin_team_ids:
                admin_team_ids_item = str(admin_team_ids_item_data)
                admin_team_ids.append(admin_team_ids_item)

        has_password = self.has_password

        mfa_enabled = self.mfa_enabled

        platform_role_display: None | str | Unset
        if isinstance(self.platform_role_display, Unset):
            platform_role_display = UNSET
        else:
            platform_role_display = self.platform_role_display

        notes: list[str] | Unset = UNSET
        if not isinstance(self.notes, Unset):
            notes = self.notes

        invitation_id: None | str | Unset
        if isinstance(self.invitation_id, Unset):
            invitation_id = UNSET
        elif isinstance(self.invitation_id, UUID):
            invitation_id = str(self.invitation_id)
        else:
            invitation_id = self.invitation_id

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "email": email,
                "first_name": first_name,
                "last_name": last_name,
                "id": id,
                "org_team_id": org_team_id,
                "display_name": display_name,
                "created_at": created_at,
            }
        )
        if org_name is not UNSET:
            field_dict["org_name"] = org_name
        if org_role is not UNSET:
            field_dict["org_role"] = org_role
        if membership_count is not UNSET:
            field_dict["membership_count"] = membership_count
        if platform_role is not UNSET:
            field_dict["platform_role"] = platform_role
        if email_verified_at is not UNSET:
            field_dict["email_verified_at"] = email_verified_at
        if email_verification_required is not UNSET:
            field_dict["email_verification_required"] = email_verification_required
        if email_verification_deadline is not UNSET:
            field_dict["email_verification_deadline"] = email_verification_deadline
        if verification_resend_available_at is not UNSET:
            field_dict["verification_resend_available_at"] = verification_resend_available_at
        if admin_team_ids is not UNSET:
            field_dict["admin_team_ids"] = admin_team_ids
        if has_password is not UNSET:
            field_dict["has_password"] = has_password
        if mfa_enabled is not UNSET:
            field_dict["mfa_enabled"] = mfa_enabled
        if platform_role_display is not UNSET:
            field_dict["platform_role_display"] = platform_role_display
        if notes is not UNSET:
            field_dict["notes"] = notes
        if invitation_id is not UNSET:
            field_dict["invitation_id"] = invitation_id

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        email = d.pop("email")

        first_name = d.pop("first_name")

        last_name = d.pop("last_name")

        id = UUID(d.pop("id"))

        org_team_id = UUID(d.pop("org_team_id"))

        display_name = d.pop("display_name")

        created_at = datetime.datetime.fromisoformat(d.pop("created_at"))

        org_name = d.pop("org_name", UNSET)

        _org_role = d.pop("org_role", UNSET)
        org_role: AdminCreatedUserReadOrgRole | Unset
        if isinstance(_org_role, Unset):
            org_role = UNSET
        else:
            org_role = AdminCreatedUserReadOrgRole(_org_role)

        membership_count = d.pop("membership_count", UNSET)

        def _parse_platform_role(data: object) -> None | PlatformRole | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                platform_role_type_0 = PlatformRole(data)

                return platform_role_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | PlatformRole | Unset, data)

        platform_role = _parse_platform_role(d.pop("platform_role", UNSET))

        def _parse_email_verified_at(data: object) -> datetime.datetime | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                email_verified_at_type_0 = datetime.datetime.fromisoformat(data)

                return email_verified_at_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None | Unset, data)

        email_verified_at = _parse_email_verified_at(d.pop("email_verified_at", UNSET))

        email_verification_required = d.pop("email_verification_required", UNSET)

        def _parse_email_verification_deadline(data: object) -> datetime.datetime | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                email_verification_deadline_type_0 = datetime.datetime.fromisoformat(data)

                return email_verification_deadline_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None | Unset, data)

        email_verification_deadline = _parse_email_verification_deadline(
            d.pop("email_verification_deadline", UNSET)
        )

        def _parse_verification_resend_available_at(
            data: object,
        ) -> datetime.datetime | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                verification_resend_available_at_type_0 = datetime.datetime.fromisoformat(data)

                return verification_resend_available_at_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None | Unset, data)

        verification_resend_available_at = _parse_verification_resend_available_at(
            d.pop("verification_resend_available_at", UNSET)
        )

        _admin_team_ids = d.pop("admin_team_ids", UNSET)
        admin_team_ids: list[UUID] | Unset = UNSET
        if _admin_team_ids is not UNSET:
            admin_team_ids = []
            for admin_team_ids_item_data in _admin_team_ids:
                admin_team_ids_item = UUID(admin_team_ids_item_data)

                admin_team_ids.append(admin_team_ids_item)

        has_password = d.pop("has_password", UNSET)

        mfa_enabled = d.pop("mfa_enabled", UNSET)

        def _parse_platform_role_display(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        platform_role_display = _parse_platform_role_display(d.pop("platform_role_display", UNSET))

        notes = cast(list[str], d.pop("notes", UNSET))

        def _parse_invitation_id(data: object) -> None | Unset | UUID:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                invitation_id_type_0 = UUID(data)

                return invitation_id_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | Unset | UUID, data)

        invitation_id = _parse_invitation_id(d.pop("invitation_id", UNSET))

        admin_created_user_read = cls(
            email=email,
            first_name=first_name,
            last_name=last_name,
            id=id,
            org_team_id=org_team_id,
            display_name=display_name,
            created_at=created_at,
            org_name=org_name,
            org_role=org_role,
            membership_count=membership_count,
            platform_role=platform_role,
            email_verified_at=email_verified_at,
            email_verification_required=email_verification_required,
            email_verification_deadline=email_verification_deadline,
            verification_resend_available_at=verification_resend_available_at,
            admin_team_ids=admin_team_ids,
            has_password=has_password,
            mfa_enabled=mfa_enabled,
            platform_role_display=platform_role_display,
            notes=notes,
            invitation_id=invitation_id,
        )

        admin_created_user_read.additional_properties = d
        return admin_created_user_read

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
