from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="PublicConfigResponse")


@_attrs_define
class PublicConfigResponse:
    """Branding the SPA needs before a user is authenticated.

    Attributes:
        product_name (str):
        support_email (None | str):
        telemetry_enabled (bool):
        sales_email (None | str | Unset):
        self_hosted (bool | Unset):  Default: False.
        sso_enforced_login_url (None | str | Unset):
        workspaces_multi_chat (bool | Unset):  Default: False.
        multi_org_enabled (bool | Unset):  Default: False.
    """

    product_name: str
    support_email: None | str
    telemetry_enabled: bool
    sales_email: None | str | Unset = UNSET
    self_hosted: bool | Unset = False
    sso_enforced_login_url: None | str | Unset = UNSET
    workspaces_multi_chat: bool | Unset = False
    multi_org_enabled: bool | Unset = False
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        product_name = self.product_name

        support_email: None | str
        support_email = self.support_email

        telemetry_enabled = self.telemetry_enabled

        sales_email: None | str | Unset
        if isinstance(self.sales_email, Unset):
            sales_email = UNSET
        else:
            sales_email = self.sales_email

        self_hosted = self.self_hosted

        sso_enforced_login_url: None | str | Unset
        if isinstance(self.sso_enforced_login_url, Unset):
            sso_enforced_login_url = UNSET
        else:
            sso_enforced_login_url = self.sso_enforced_login_url

        workspaces_multi_chat = self.workspaces_multi_chat

        multi_org_enabled = self.multi_org_enabled

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "product_name": product_name,
                "support_email": support_email,
                "telemetry_enabled": telemetry_enabled,
            }
        )
        if sales_email is not UNSET:
            field_dict["sales_email"] = sales_email
        if self_hosted is not UNSET:
            field_dict["self_hosted"] = self_hosted
        if sso_enforced_login_url is not UNSET:
            field_dict["sso_enforced_login_url"] = sso_enforced_login_url
        if workspaces_multi_chat is not UNSET:
            field_dict["workspaces_multi_chat"] = workspaces_multi_chat
        if multi_org_enabled is not UNSET:
            field_dict["multi_org_enabled"] = multi_org_enabled

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        product_name = d.pop("product_name")

        def _parse_support_email(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        support_email = _parse_support_email(d.pop("support_email"))

        telemetry_enabled = d.pop("telemetry_enabled")

        def _parse_sales_email(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        sales_email = _parse_sales_email(d.pop("sales_email", UNSET))

        self_hosted = d.pop("self_hosted", UNSET)

        def _parse_sso_enforced_login_url(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        sso_enforced_login_url = _parse_sso_enforced_login_url(
            d.pop("sso_enforced_login_url", UNSET)
        )

        workspaces_multi_chat = d.pop("workspaces_multi_chat", UNSET)

        multi_org_enabled = d.pop("multi_org_enabled", UNSET)

        public_config_response = cls(
            product_name=product_name,
            support_email=support_email,
            telemetry_enabled=telemetry_enabled,
            sales_email=sales_email,
            self_hosted=self_hosted,
            sso_enforced_login_url=sso_enforced_login_url,
            workspaces_multi_chat=workspaces_multi_chat,
            multi_org_enabled=multi_org_enabled,
        )

        public_config_response.additional_properties = d
        return public_config_response

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
