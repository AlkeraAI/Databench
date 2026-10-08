from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.groups_mapping import GroupsMapping


T = TypeVar("T", bound="SsoConnectionRead")


@_attrs_define
class SsoConnectionRead:
    """The org's SSO config for the admin screen. The OIDC client secret + SAML
    cert are NEVER returned — only whether they're stored. For SAML, the SP
    values the org admin must register with their IdP are surfaced.

        Attributes:
            configured (bool):
            enabled (bool):
            enforced (bool):
            protocol (str):
            allowed_domains (str):
            oidc_issuer (None | str):
            oidc_client_id (None | str):
            has_client_secret (bool):
            saml_idp_entity_id (None | str):
            saml_sso_url (None | str):
            has_saml_cert (bool):
            session_max_age_seconds (int | Unset):  Default: 86400.
            saml_sp_entity_id (None | str | Unset):
            saml_acs_url (None | str | Unset):
            groups_mapping (GroupsMapping | Unset):
            scim_enabled (bool | Unset):  Default: False.
            has_scim_token (bool | Unset):  Default: False.
            scim_base_url (None | str | Unset):
    """

    configured: bool
    enabled: bool
    enforced: bool
    protocol: str
    allowed_domains: str
    oidc_issuer: None | str
    oidc_client_id: None | str
    has_client_secret: bool
    saml_idp_entity_id: None | str
    saml_sso_url: None | str
    has_saml_cert: bool
    session_max_age_seconds: int | Unset = 86400
    saml_sp_entity_id: None | str | Unset = UNSET
    saml_acs_url: None | str | Unset = UNSET
    groups_mapping: GroupsMapping | Unset = UNSET
    scim_enabled: bool | Unset = False
    has_scim_token: bool | Unset = False
    scim_base_url: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        configured = self.configured

        enabled = self.enabled

        enforced = self.enforced

        protocol = self.protocol

        allowed_domains = self.allowed_domains

        oidc_issuer: None | str
        oidc_issuer = self.oidc_issuer

        oidc_client_id: None | str
        oidc_client_id = self.oidc_client_id

        has_client_secret = self.has_client_secret

        saml_idp_entity_id: None | str
        saml_idp_entity_id = self.saml_idp_entity_id

        saml_sso_url: None | str
        saml_sso_url = self.saml_sso_url

        has_saml_cert = self.has_saml_cert

        session_max_age_seconds = self.session_max_age_seconds

        saml_sp_entity_id: None | str | Unset
        if isinstance(self.saml_sp_entity_id, Unset):
            saml_sp_entity_id = UNSET
        else:
            saml_sp_entity_id = self.saml_sp_entity_id

        saml_acs_url: None | str | Unset
        if isinstance(self.saml_acs_url, Unset):
            saml_acs_url = UNSET
        else:
            saml_acs_url = self.saml_acs_url

        groups_mapping: dict[str, Any] | Unset = UNSET
        if not isinstance(self.groups_mapping, Unset):
            groups_mapping = self.groups_mapping.to_dict()

        scim_enabled = self.scim_enabled

        has_scim_token = self.has_scim_token

        scim_base_url: None | str | Unset
        if isinstance(self.scim_base_url, Unset):
            scim_base_url = UNSET
        else:
            scim_base_url = self.scim_base_url

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "configured": configured,
                "enabled": enabled,
                "enforced": enforced,
                "protocol": protocol,
                "allowed_domains": allowed_domains,
                "oidc_issuer": oidc_issuer,
                "oidc_client_id": oidc_client_id,
                "has_client_secret": has_client_secret,
                "saml_idp_entity_id": saml_idp_entity_id,
                "saml_sso_url": saml_sso_url,
                "has_saml_cert": has_saml_cert,
            }
        )
        if session_max_age_seconds is not UNSET:
            field_dict["session_max_age_seconds"] = session_max_age_seconds
        if saml_sp_entity_id is not UNSET:
            field_dict["saml_sp_entity_id"] = saml_sp_entity_id
        if saml_acs_url is not UNSET:
            field_dict["saml_acs_url"] = saml_acs_url
        if groups_mapping is not UNSET:
            field_dict["groups_mapping"] = groups_mapping
        if scim_enabled is not UNSET:
            field_dict["scim_enabled"] = scim_enabled
        if has_scim_token is not UNSET:
            field_dict["has_scim_token"] = has_scim_token
        if scim_base_url is not UNSET:
            field_dict["scim_base_url"] = scim_base_url

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.groups_mapping import GroupsMapping

        d = dict(src_dict)
        configured = d.pop("configured")

        enabled = d.pop("enabled")

        enforced = d.pop("enforced")

        protocol = d.pop("protocol")

        allowed_domains = d.pop("allowed_domains")

        def _parse_oidc_issuer(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        oidc_issuer = _parse_oidc_issuer(d.pop("oidc_issuer"))

        def _parse_oidc_client_id(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        oidc_client_id = _parse_oidc_client_id(d.pop("oidc_client_id"))

        has_client_secret = d.pop("has_client_secret")

        def _parse_saml_idp_entity_id(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        saml_idp_entity_id = _parse_saml_idp_entity_id(d.pop("saml_idp_entity_id"))

        def _parse_saml_sso_url(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        saml_sso_url = _parse_saml_sso_url(d.pop("saml_sso_url"))

        has_saml_cert = d.pop("has_saml_cert")

        session_max_age_seconds = d.pop("session_max_age_seconds", UNSET)

        def _parse_saml_sp_entity_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        saml_sp_entity_id = _parse_saml_sp_entity_id(d.pop("saml_sp_entity_id", UNSET))

        def _parse_saml_acs_url(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        saml_acs_url = _parse_saml_acs_url(d.pop("saml_acs_url", UNSET))

        _groups_mapping = d.pop("groups_mapping", UNSET)
        groups_mapping: GroupsMapping | Unset
        if isinstance(_groups_mapping, Unset):
            groups_mapping = UNSET
        else:
            groups_mapping = GroupsMapping.from_dict(_groups_mapping)

        scim_enabled = d.pop("scim_enabled", UNSET)

        has_scim_token = d.pop("has_scim_token", UNSET)

        def _parse_scim_base_url(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        scim_base_url = _parse_scim_base_url(d.pop("scim_base_url", UNSET))

        sso_connection_read = cls(
            configured=configured,
            enabled=enabled,
            enforced=enforced,
            protocol=protocol,
            allowed_domains=allowed_domains,
            oidc_issuer=oidc_issuer,
            oidc_client_id=oidc_client_id,
            has_client_secret=has_client_secret,
            saml_idp_entity_id=saml_idp_entity_id,
            saml_sso_url=saml_sso_url,
            has_saml_cert=has_saml_cert,
            session_max_age_seconds=session_max_age_seconds,
            saml_sp_entity_id=saml_sp_entity_id,
            saml_acs_url=saml_acs_url,
            groups_mapping=groups_mapping,
            scim_enabled=scim_enabled,
            has_scim_token=has_scim_token,
            scim_base_url=scim_base_url,
        )

        sso_connection_read.additional_properties = d
        return sso_connection_read

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
