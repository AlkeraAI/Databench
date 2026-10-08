from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import Any, TypeVar, cast
from uuid import UUID

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="UserBanRead")


@_attrs_define
class UserBanRead:
    """
    Attributes:
        id (UUID):
        user_id (UUID):
        user_email (str):
        user_display_name (str):
        reason (str):
        created_at (datetime.datetime):
        active (bool):
        created_by_id (None | Unset | UUID):
        created_by_email (None | str | Unset):
        lifted_at (datetime.datetime | None | Unset):
        lifted_by_id (None | Unset | UUID):
        lifted_by_email (None | str | Unset):
    """

    id: UUID
    user_id: UUID
    user_email: str
    user_display_name: str
    reason: str
    created_at: datetime.datetime
    active: bool
    created_by_id: None | Unset | UUID = UNSET
    created_by_email: None | str | Unset = UNSET
    lifted_at: datetime.datetime | None | Unset = UNSET
    lifted_by_id: None | Unset | UUID = UNSET
    lifted_by_email: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        id = str(self.id)

        user_id = str(self.user_id)

        user_email = self.user_email

        user_display_name = self.user_display_name

        reason = self.reason

        created_at = self.created_at.isoformat()

        active = self.active

        created_by_id: None | str | Unset
        if isinstance(self.created_by_id, Unset):
            created_by_id = UNSET
        elif isinstance(self.created_by_id, UUID):
            created_by_id = str(self.created_by_id)
        else:
            created_by_id = self.created_by_id

        created_by_email: None | str | Unset
        if isinstance(self.created_by_email, Unset):
            created_by_email = UNSET
        else:
            created_by_email = self.created_by_email

        lifted_at: None | str | Unset
        if isinstance(self.lifted_at, Unset):
            lifted_at = UNSET
        elif isinstance(self.lifted_at, datetime.datetime):
            lifted_at = self.lifted_at.isoformat()
        else:
            lifted_at = self.lifted_at

        lifted_by_id: None | str | Unset
        if isinstance(self.lifted_by_id, Unset):
            lifted_by_id = UNSET
        elif isinstance(self.lifted_by_id, UUID):
            lifted_by_id = str(self.lifted_by_id)
        else:
            lifted_by_id = self.lifted_by_id

        lifted_by_email: None | str | Unset
        if isinstance(self.lifted_by_email, Unset):
            lifted_by_email = UNSET
        else:
            lifted_by_email = self.lifted_by_email

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "user_id": user_id,
                "user_email": user_email,
                "user_display_name": user_display_name,
                "reason": reason,
                "created_at": created_at,
                "active": active,
            }
        )
        if created_by_id is not UNSET:
            field_dict["created_by_id"] = created_by_id
        if created_by_email is not UNSET:
            field_dict["created_by_email"] = created_by_email
        if lifted_at is not UNSET:
            field_dict["lifted_at"] = lifted_at
        if lifted_by_id is not UNSET:
            field_dict["lifted_by_id"] = lifted_by_id
        if lifted_by_email is not UNSET:
            field_dict["lifted_by_email"] = lifted_by_email

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        id = UUID(d.pop("id"))

        user_id = UUID(d.pop("user_id"))

        user_email = d.pop("user_email")

        user_display_name = d.pop("user_display_name")

        reason = d.pop("reason")

        created_at = datetime.datetime.fromisoformat(d.pop("created_at"))

        active = d.pop("active")

        def _parse_created_by_id(data: object) -> None | Unset | UUID:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                created_by_id_type_0 = UUID(data)

                return created_by_id_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | Unset | UUID, data)

        created_by_id = _parse_created_by_id(d.pop("created_by_id", UNSET))

        def _parse_created_by_email(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        created_by_email = _parse_created_by_email(d.pop("created_by_email", UNSET))

        def _parse_lifted_at(data: object) -> datetime.datetime | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                lifted_at_type_0 = datetime.datetime.fromisoformat(data)

                return lifted_at_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None | Unset, data)

        lifted_at = _parse_lifted_at(d.pop("lifted_at", UNSET))

        def _parse_lifted_by_id(data: object) -> None | Unset | UUID:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                lifted_by_id_type_0 = UUID(data)

                return lifted_by_id_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | Unset | UUID, data)

        lifted_by_id = _parse_lifted_by_id(d.pop("lifted_by_id", UNSET))

        def _parse_lifted_by_email(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        lifted_by_email = _parse_lifted_by_email(d.pop("lifted_by_email", UNSET))

        user_ban_read = cls(
            id=id,
            user_id=user_id,
            user_email=user_email,
            user_display_name=user_display_name,
            reason=reason,
            created_at=created_at,
            active=active,
            created_by_id=created_by_id,
            created_by_email=created_by_email,
            lifted_at=lifted_at,
            lifted_by_id=lifted_by_id,
            lifted_by_email=lifted_by_email,
        )

        user_ban_read.additional_properties = d
        return user_ban_read

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
