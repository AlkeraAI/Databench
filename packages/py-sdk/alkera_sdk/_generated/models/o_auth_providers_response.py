from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

T = TypeVar("T", bound="OAuthProvidersResponse")


@_attrs_define
class OAuthProvidersResponse:
    """Platform-enabled external-login providers, for rendering sign-in buttons.

    Note: this is the *platform* configuration (which providers have credentials
    wired up). Per-org `allow_login_*` gating is enforced server-side at the
    callback — the login page can't know the org before sign-in.

        Attributes:
            providers (list[str]):
    """

    providers: list[str]
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        providers = self.providers

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "providers": providers,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        providers = cast(list[str], d.pop("providers"))

        o_auth_providers_response = cls(
            providers=providers,
        )

        o_auth_providers_response.additional_properties = d
        return o_auth_providers_response

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
