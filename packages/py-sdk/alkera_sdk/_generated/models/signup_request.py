from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="SignupRequest")


@_attrs_define
class SignupRequest:
    """Minimal signup: only email + password are required. Name and org are
    finished on the complete-profile step.

    Org selection (mutually exclusive — never both):
    - `invite_token` set → join the inviter's org with the invitation's role.
    - `org_name` set → create a new org with that name (the caller becomes Org
      Admin). Optional: omitting it creates a new *unnamed* org the caller names
      on complete-profile.

    Names are optional (the empty-string "profile incomplete" sentinel) and
    collected on complete-profile; a caller that already knows them (apps/web,
    OAuth) may still send them here.

        Attributes:
            email (str):
            password (str):
            first_name (str | Unset):  Default: ''.
            last_name (str | Unset):  Default: ''.
            org_name (None | str | Unset):
            invite_token (None | str | Unset):
            allow_personal_email (bool | Unset):  Default: False.
            turnstile_token (None | str | Unset):
    """

    email: str
    password: str
    first_name: str | Unset = ""
    last_name: str | Unset = ""
    org_name: None | str | Unset = UNSET
    invite_token: None | str | Unset = UNSET
    allow_personal_email: bool | Unset = False
    turnstile_token: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        email = self.email

        password = self.password

        first_name = self.first_name

        last_name = self.last_name

        org_name: None | str | Unset
        if isinstance(self.org_name, Unset):
            org_name = UNSET
        else:
            org_name = self.org_name

        invite_token: None | str | Unset
        if isinstance(self.invite_token, Unset):
            invite_token = UNSET
        else:
            invite_token = self.invite_token

        allow_personal_email = self.allow_personal_email

        turnstile_token: None | str | Unset
        if isinstance(self.turnstile_token, Unset):
            turnstile_token = UNSET
        else:
            turnstile_token = self.turnstile_token

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "email": email,
                "password": password,
            }
        )
        if first_name is not UNSET:
            field_dict["first_name"] = first_name
        if last_name is not UNSET:
            field_dict["last_name"] = last_name
        if org_name is not UNSET:
            field_dict["org_name"] = org_name
        if invite_token is not UNSET:
            field_dict["invite_token"] = invite_token
        if allow_personal_email is not UNSET:
            field_dict["allow_personal_email"] = allow_personal_email
        if turnstile_token is not UNSET:
            field_dict["turnstile_token"] = turnstile_token

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        email = d.pop("email")

        password = d.pop("password")

        first_name = d.pop("first_name", UNSET)

        last_name = d.pop("last_name", UNSET)

        def _parse_org_name(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        org_name = _parse_org_name(d.pop("org_name", UNSET))

        def _parse_invite_token(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        invite_token = _parse_invite_token(d.pop("invite_token", UNSET))

        allow_personal_email = d.pop("allow_personal_email", UNSET)

        def _parse_turnstile_token(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        turnstile_token = _parse_turnstile_token(d.pop("turnstile_token", UNSET))

        signup_request = cls(
            email=email,
            password=password,
            first_name=first_name,
            last_name=last_name,
            org_name=org_name,
            invite_token=invite_token,
            allow_personal_email=allow_personal_email,
            turnstile_token=turnstile_token,
        )

        signup_request.additional_properties = d
        return signup_request

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
