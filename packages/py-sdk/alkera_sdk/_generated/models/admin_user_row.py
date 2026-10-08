from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import Any, TypeVar, cast
from uuid import UUID

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.platform_role import PlatformRole
from ..types import UNSET, Unset

T = TypeVar("T", bound="AdminUserRow")


@_attrs_define
class AdminUserRow:
    """
    Attributes:
        id (UUID):
        email (str):
        display_name (str):
        org_team_id (UUID):
        is_active (bool):
        created_at (datetime.datetime):
        org_name (None | str | Unset):
        platform_role (None | PlatformRole | Unset):
        email_verified_at (datetime.datetime | None | Unset):
        disposable_email (bool | Unset):  Default: False.
        signup_ip (None | str | Unset):
        last_login_ip (None | str | Unset):
        mtd_billed_nanos (int | Unset):  Default: 0.
        mtd_request_count (int | Unset):  Default: 0.
        banned (bool | Unset):  Default: False.
        ban_reason (None | str | Unset):
    """

    id: UUID
    email: str
    display_name: str
    org_team_id: UUID
    is_active: bool
    created_at: datetime.datetime
    org_name: None | str | Unset = UNSET
    platform_role: None | PlatformRole | Unset = UNSET
    email_verified_at: datetime.datetime | None | Unset = UNSET
    disposable_email: bool | Unset = False
    signup_ip: None | str | Unset = UNSET
    last_login_ip: None | str | Unset = UNSET
    mtd_billed_nanos: int | Unset = 0
    mtd_request_count: int | Unset = 0
    banned: bool | Unset = False
    ban_reason: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        id = str(self.id)

        email = self.email

        display_name = self.display_name

        org_team_id = str(self.org_team_id)

        is_active = self.is_active

        created_at = self.created_at.isoformat()

        org_name: None | str | Unset
        if isinstance(self.org_name, Unset):
            org_name = UNSET
        else:
            org_name = self.org_name

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

        disposable_email = self.disposable_email

        signup_ip: None | str | Unset
        if isinstance(self.signup_ip, Unset):
            signup_ip = UNSET
        else:
            signup_ip = self.signup_ip

        last_login_ip: None | str | Unset
        if isinstance(self.last_login_ip, Unset):
            last_login_ip = UNSET
        else:
            last_login_ip = self.last_login_ip

        mtd_billed_nanos = self.mtd_billed_nanos

        mtd_request_count = self.mtd_request_count

        banned = self.banned

        ban_reason: None | str | Unset
        if isinstance(self.ban_reason, Unset):
            ban_reason = UNSET
        else:
            ban_reason = self.ban_reason

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "email": email,
                "display_name": display_name,
                "org_team_id": org_team_id,
                "is_active": is_active,
                "created_at": created_at,
            }
        )
        if org_name is not UNSET:
            field_dict["org_name"] = org_name
        if platform_role is not UNSET:
            field_dict["platform_role"] = platform_role
        if email_verified_at is not UNSET:
            field_dict["email_verified_at"] = email_verified_at
        if disposable_email is not UNSET:
            field_dict["disposable_email"] = disposable_email
        if signup_ip is not UNSET:
            field_dict["signup_ip"] = signup_ip
        if last_login_ip is not UNSET:
            field_dict["last_login_ip"] = last_login_ip
        if mtd_billed_nanos is not UNSET:
            field_dict["mtd_billed_nanos"] = mtd_billed_nanos
        if mtd_request_count is not UNSET:
            field_dict["mtd_request_count"] = mtd_request_count
        if banned is not UNSET:
            field_dict["banned"] = banned
        if ban_reason is not UNSET:
            field_dict["ban_reason"] = ban_reason

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        id = UUID(d.pop("id"))

        email = d.pop("email")

        display_name = d.pop("display_name")

        org_team_id = UUID(d.pop("org_team_id"))

        is_active = d.pop("is_active")

        created_at = datetime.datetime.fromisoformat(d.pop("created_at"))

        def _parse_org_name(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        org_name = _parse_org_name(d.pop("org_name", UNSET))

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

        disposable_email = d.pop("disposable_email", UNSET)

        def _parse_signup_ip(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        signup_ip = _parse_signup_ip(d.pop("signup_ip", UNSET))

        def _parse_last_login_ip(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        last_login_ip = _parse_last_login_ip(d.pop("last_login_ip", UNSET))

        mtd_billed_nanos = d.pop("mtd_billed_nanos", UNSET)

        mtd_request_count = d.pop("mtd_request_count", UNSET)

        banned = d.pop("banned", UNSET)

        def _parse_ban_reason(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        ban_reason = _parse_ban_reason(d.pop("ban_reason", UNSET))

        admin_user_row = cls(
            id=id,
            email=email,
            display_name=display_name,
            org_team_id=org_team_id,
            is_active=is_active,
            created_at=created_at,
            org_name=org_name,
            platform_role=platform_role,
            email_verified_at=email_verified_at,
            disposable_email=disposable_email,
            signup_ip=signup_ip,
            last_login_ip=last_login_ip,
            mtd_billed_nanos=mtd_billed_nanos,
            mtd_request_count=mtd_request_count,
            banned=banned,
            ban_reason=ban_reason,
        )

        admin_user_row.additional_properties = d
        return admin_user_row

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
