from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="DeviceInfoResponse")


@_attrs_define
class DeviceInfoResponse:
    """Consent-screen data for GET /auth/device/info — what the SPA shows before
    the user approves a pending device authorization.

        Attributes:
            client_id (str):
            client_name (str):
            expires_at (datetime.datetime):
            user_code (str):
            scope (None | str | Unset):
    """

    client_id: str
    client_name: str
    expires_at: datetime.datetime
    user_code: str
    scope: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        client_id = self.client_id

        client_name = self.client_name

        expires_at = self.expires_at.isoformat()

        user_code = self.user_code

        scope: None | str | Unset
        if isinstance(self.scope, Unset):
            scope = UNSET
        else:
            scope = self.scope

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "client_id": client_id,
                "client_name": client_name,
                "expires_at": expires_at,
                "user_code": user_code,
            }
        )
        if scope is not UNSET:
            field_dict["scope"] = scope

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        client_id = d.pop("client_id")

        client_name = d.pop("client_name")

        expires_at = datetime.datetime.fromisoformat(d.pop("expires_at"))

        user_code = d.pop("user_code")

        def _parse_scope(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        scope = _parse_scope(d.pop("scope", UNSET))

        device_info_response = cls(
            client_id=client_id,
            client_name=client_name,
            expires_at=expires_at,
            user_code=user_code,
            scope=scope,
        )

        device_info_response.additional_properties = d
        return device_info_response

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
