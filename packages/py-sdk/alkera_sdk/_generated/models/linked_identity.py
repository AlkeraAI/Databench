from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="LinkedIdentity")


@_attrs_define
class LinkedIdentity:
    """One linked external-login provider for the current user.

    Attributes:
        provider (str):
        created_at (datetime.datetime):
        email_at_link (None | str | Unset):
        last_login_at (datetime.datetime | None | Unset):
    """

    provider: str
    created_at: datetime.datetime
    email_at_link: None | str | Unset = UNSET
    last_login_at: datetime.datetime | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        provider = self.provider

        created_at = self.created_at.isoformat()

        email_at_link: None | str | Unset
        if isinstance(self.email_at_link, Unset):
            email_at_link = UNSET
        else:
            email_at_link = self.email_at_link

        last_login_at: None | str | Unset
        if isinstance(self.last_login_at, Unset):
            last_login_at = UNSET
        elif isinstance(self.last_login_at, datetime.datetime):
            last_login_at = self.last_login_at.isoformat()
        else:
            last_login_at = self.last_login_at

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "provider": provider,
                "created_at": created_at,
            }
        )
        if email_at_link is not UNSET:
            field_dict["email_at_link"] = email_at_link
        if last_login_at is not UNSET:
            field_dict["last_login_at"] = last_login_at

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        provider = d.pop("provider")

        created_at = datetime.datetime.fromisoformat(d.pop("created_at"))

        def _parse_email_at_link(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        email_at_link = _parse_email_at_link(d.pop("email_at_link", UNSET))

        def _parse_last_login_at(data: object) -> datetime.datetime | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                last_login_at_type_0 = datetime.datetime.fromisoformat(data)

                return last_login_at_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None | Unset, data)

        last_login_at = _parse_last_login_at(d.pop("last_login_at", UNSET))

        linked_identity = cls(
            provider=provider,
            created_at=created_at,
            email_at_link=email_at_link,
            last_login_at=last_login_at,
        )

        linked_identity.additional_properties = d
        return linked_identity

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
