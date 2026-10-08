from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="OAuthRegisterContext")


@_attrs_define
class OAuthRegisterContext:
    """Prefill data for the SPA register screen, derived from a signed ticket.

    The backend always trusts the ticket (not the client) for email/provider —
    these are display-only hints.

        Attributes:
            provider (str):
            email (str):
            first_name (str | Unset):  Default: ''.
            last_name (str | Unset):  Default: ''.
            has_invite (bool | Unset):  Default: False.
    """

    provider: str
    email: str
    first_name: str | Unset = ""
    last_name: str | Unset = ""
    has_invite: bool | Unset = False
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        provider = self.provider

        email = self.email

        first_name = self.first_name

        last_name = self.last_name

        has_invite = self.has_invite

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "provider": provider,
                "email": email,
            }
        )
        if first_name is not UNSET:
            field_dict["first_name"] = first_name
        if last_name is not UNSET:
            field_dict["last_name"] = last_name
        if has_invite is not UNSET:
            field_dict["has_invite"] = has_invite

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        provider = d.pop("provider")

        email = d.pop("email")

        first_name = d.pop("first_name", UNSET)

        last_name = d.pop("last_name", UNSET)

        has_invite = d.pop("has_invite", UNSET)

        o_auth_register_context = cls(
            provider=provider,
            email=email,
            first_name=first_name,
            last_name=last_name,
            has_invite=has_invite,
        )

        o_auth_register_context.additional_properties = d
        return o_auth_register_context

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
