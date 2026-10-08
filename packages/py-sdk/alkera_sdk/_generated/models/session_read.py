from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.token_type import TokenType
from ..types import UNSET, Unset

T = TypeVar("T", bound="SessionRead")


@_attrs_define
class SessionRead:
    """One live session in `GET /auth/sessions`. Never includes a token value.

    A browser session is a refresh-token FAMILY: `jti` carries the family id,
    `issued_at` is when it signed in, `expires_at` its absolute expiry, and
    `client` what the browser said it was. A CLI token is its own row, keyed by
    its real `jti`. `current` flags the session making the request (so a UI can
    avoid offering to revoke the user out from under themselves without warning).

        Attributes:
            jti (str):
            token_type (TokenType): Kind of auth token recorded in `auth_tokens`.

                `session` = short-lived cookie JWT (SPA); `cli` = long-lived Bearer JWT
                (the `alkera` CLI + daemon). Same JWT shape; differ only in TTL + origin.
            issued_at (datetime.datetime):
            expires_at (datetime.datetime):
            last_used_at (datetime.datetime | None | Unset):
            label (None | str | Unset):
            client (None | str | Unset):
            current (bool | Unset):  Default: False.
    """

    jti: str
    token_type: TokenType
    issued_at: datetime.datetime
    expires_at: datetime.datetime
    last_used_at: datetime.datetime | None | Unset = UNSET
    label: None | str | Unset = UNSET
    client: None | str | Unset = UNSET
    current: bool | Unset = False
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        jti = self.jti

        token_type = self.token_type.value

        issued_at = self.issued_at.isoformat()

        expires_at = self.expires_at.isoformat()

        last_used_at: None | str | Unset
        if isinstance(self.last_used_at, Unset):
            last_used_at = UNSET
        elif isinstance(self.last_used_at, datetime.datetime):
            last_used_at = self.last_used_at.isoformat()
        else:
            last_used_at = self.last_used_at

        label: None | str | Unset
        if isinstance(self.label, Unset):
            label = UNSET
        else:
            label = self.label

        client: None | str | Unset
        if isinstance(self.client, Unset):
            client = UNSET
        else:
            client = self.client

        current = self.current

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "jti": jti,
                "token_type": token_type,
                "issued_at": issued_at,
                "expires_at": expires_at,
            }
        )
        if last_used_at is not UNSET:
            field_dict["last_used_at"] = last_used_at
        if label is not UNSET:
            field_dict["label"] = label
        if client is not UNSET:
            field_dict["client"] = client
        if current is not UNSET:
            field_dict["current"] = current

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        jti = d.pop("jti")

        token_type = TokenType(d.pop("token_type"))

        issued_at = datetime.datetime.fromisoformat(d.pop("issued_at"))

        expires_at = datetime.datetime.fromisoformat(d.pop("expires_at"))

        def _parse_last_used_at(data: object) -> datetime.datetime | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                last_used_at_type_0 = datetime.datetime.fromisoformat(data)

                return last_used_at_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None | Unset, data)

        last_used_at = _parse_last_used_at(d.pop("last_used_at", UNSET))

        def _parse_label(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        label = _parse_label(d.pop("label", UNSET))

        def _parse_client(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        client = _parse_client(d.pop("client", UNSET))

        current = d.pop("current", UNSET)

        session_read = cls(
            jti=jti,
            token_type=token_type,
            issued_at=issued_at,
            expires_at=expires_at,
            last_used_at=last_used_at,
            label=label,
            client=client,
            current=current,
        )

        session_read.additional_properties = d
        return session_read

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
