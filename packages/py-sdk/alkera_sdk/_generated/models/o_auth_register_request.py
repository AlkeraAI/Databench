from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="OAuthRegisterRequest")


@_attrs_define
class OAuthRegisterRequest:
    """Complete an OAuth-initiated registration.

    `oauth_ticket` is the signed ticket minted by the OAuth callback; it carries
    the verified email/provider/subject. The user supplies (and may edit) their
    name, and chooses an org exactly like email/password signup: exactly one of
    `org_name` (new org, becomes Org Admin) or `invite_token` (join via invite).
    When the ticket itself carries an invite, that wins.

        Attributes:
            oauth_ticket (str):
            first_name (str):
            last_name (str):
            org_name (None | str | Unset):
            invite_token (None | str | Unset):
            allow_personal_email (bool | Unset):  Default: False.
    """

    oauth_ticket: str
    first_name: str
    last_name: str
    org_name: None | str | Unset = UNSET
    invite_token: None | str | Unset = UNSET
    allow_personal_email: bool | Unset = False
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        oauth_ticket = self.oauth_ticket

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

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "oauth_ticket": oauth_ticket,
                "first_name": first_name,
                "last_name": last_name,
            }
        )
        if org_name is not UNSET:
            field_dict["org_name"] = org_name
        if invite_token is not UNSET:
            field_dict["invite_token"] = invite_token
        if allow_personal_email is not UNSET:
            field_dict["allow_personal_email"] = allow_personal_email

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        oauth_ticket = d.pop("oauth_ticket")

        first_name = d.pop("first_name")

        last_name = d.pop("last_name")

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

        o_auth_register_request = cls(
            oauth_ticket=oauth_ticket,
            first_name=first_name,
            last_name=last_name,
            org_name=org_name,
            invite_token=invite_token,
            allow_personal_email=allow_personal_email,
        )

        o_auth_register_request.additional_properties = d
        return o_auth_register_request

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
