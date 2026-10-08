from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="SsoDiscoverResponse")


@_attrs_define
class SsoDiscoverResponse:
    """
    Attributes:
        sso (bool):
        login_url (None | str):
        enforced (bool | Unset):  Default: False.
    """

    sso: bool
    login_url: None | str
    enforced: bool | Unset = False
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        sso = self.sso

        login_url: None | str
        login_url = self.login_url

        enforced = self.enforced

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "sso": sso,
                "login_url": login_url,
            }
        )
        if enforced is not UNSET:
            field_dict["enforced"] = enforced

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        sso = d.pop("sso")

        def _parse_login_url(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        login_url = _parse_login_url(d.pop("login_url"))

        enforced = d.pop("enforced", UNSET)

        sso_discover_response = cls(
            sso=sso,
            login_url=login_url,
            enforced=enforced,
        )

        sso_discover_response.additional_properties = d
        return sso_discover_response

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
