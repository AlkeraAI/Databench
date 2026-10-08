from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import Any, TypeVar, cast
from uuid import UUID

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.membership_read_role import MembershipReadRole
from ..models.membership_read_status import MembershipReadStatus
from ..types import UNSET, Unset

T = TypeVar("T", bound="MembershipRead")


@_attrs_define
class MembershipRead:
    """One org the caller belongs to and can switch into, or, when ``status``
    is ``pending``, one that provisioned them and waits for them to join.

        Attributes:
            org_team_id (UUID):
            org_name (str):
            role (MembershipReadRole):
            sso_required (bool):
            last_active_at (datetime.datetime | None | Unset):
            status (MembershipReadStatus | Unset):  Default: MembershipReadStatus.ACTIVE.
    """

    org_team_id: UUID
    org_name: str
    role: MembershipReadRole
    sso_required: bool
    last_active_at: datetime.datetime | None | Unset = UNSET
    status: MembershipReadStatus | Unset = MembershipReadStatus.ACTIVE
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        org_team_id = str(self.org_team_id)

        org_name = self.org_name

        role = self.role.value

        sso_required = self.sso_required

        last_active_at: None | str | Unset
        if isinstance(self.last_active_at, Unset):
            last_active_at = UNSET
        elif isinstance(self.last_active_at, datetime.datetime):
            last_active_at = self.last_active_at.isoformat()
        else:
            last_active_at = self.last_active_at

        status: str | Unset = UNSET
        if not isinstance(self.status, Unset):
            status = self.status.value

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "org_team_id": org_team_id,
                "org_name": org_name,
                "role": role,
                "sso_required": sso_required,
            }
        )
        if last_active_at is not UNSET:
            field_dict["last_active_at"] = last_active_at
        if status is not UNSET:
            field_dict["status"] = status

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        org_team_id = UUID(d.pop("org_team_id"))

        org_name = d.pop("org_name")

        role = MembershipReadRole(d.pop("role"))

        sso_required = d.pop("sso_required")

        def _parse_last_active_at(data: object) -> datetime.datetime | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                last_active_at_type_0 = datetime.datetime.fromisoformat(data)

                return last_active_at_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None | Unset, data)

        last_active_at = _parse_last_active_at(d.pop("last_active_at", UNSET))

        _status = d.pop("status", UNSET)
        status: MembershipReadStatus | Unset
        if isinstance(_status, Unset):
            status = UNSET
        else:
            status = MembershipReadStatus(_status)

        membership_read = cls(
            org_team_id=org_team_id,
            org_name=org_name,
            role=role,
            sso_required=sso_required,
            last_active_at=last_active_at,
            status=status,
        )

        membership_read.additional_properties = d
        return membership_read

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
