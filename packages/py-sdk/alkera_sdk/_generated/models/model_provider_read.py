from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.model_provider_read_bedrock_auth_mode_type_0 import (
    ModelProviderReadBedrockAuthModeType0,
)
from ..models.model_provider_read_last_verified_status_type_0 import (
    ModelProviderReadLastVerifiedStatusType0,
)
from ..models.model_provider_read_provider import ModelProviderReadProvider
from ..types import UNSET, Unset

T = TypeVar("T", bound="ModelProviderRead")


@_attrs_define
class ModelProviderRead:
    """One provider card. NEVER carries a secret — only presence + a hint.

    Attributes:
        provider (ModelProviderReadProvider):
        configured (bool):
        enabled (bool):
        has_credentials (bool):
        secret_hint (None | str):
        base_url (None | str):
        env_fallback (bool):
        last_verified_at (datetime.datetime | None):
        last_verified_status (ModelProviderReadLastVerifiedStatusType0 | None):
        updated_at (datetime.datetime | None):
        openai_organization_id (None | str | Unset):
        bedrock_region (None | str | Unset):
        bedrock_auth_mode (ModelProviderReadBedrockAuthModeType0 | None | Unset):
    """

    provider: ModelProviderReadProvider
    configured: bool
    enabled: bool
    has_credentials: bool
    secret_hint: None | str
    base_url: None | str
    env_fallback: bool
    last_verified_at: datetime.datetime | None
    last_verified_status: ModelProviderReadLastVerifiedStatusType0 | None
    updated_at: datetime.datetime | None
    openai_organization_id: None | str | Unset = UNSET
    bedrock_region: None | str | Unset = UNSET
    bedrock_auth_mode: ModelProviderReadBedrockAuthModeType0 | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        provider = self.provider.value

        configured = self.configured

        enabled = self.enabled

        has_credentials = self.has_credentials

        secret_hint: None | str
        secret_hint = self.secret_hint

        base_url: None | str
        base_url = self.base_url

        env_fallback = self.env_fallback

        last_verified_at: None | str
        if isinstance(self.last_verified_at, datetime.datetime):
            last_verified_at = self.last_verified_at.isoformat()
        else:
            last_verified_at = self.last_verified_at

        last_verified_status: None | str
        if isinstance(self.last_verified_status, ModelProviderReadLastVerifiedStatusType0):
            last_verified_status = self.last_verified_status.value
        else:
            last_verified_status = self.last_verified_status

        updated_at: None | str
        if isinstance(self.updated_at, datetime.datetime):
            updated_at = self.updated_at.isoformat()
        else:
            updated_at = self.updated_at

        openai_organization_id: None | str | Unset
        if isinstance(self.openai_organization_id, Unset):
            openai_organization_id = UNSET
        else:
            openai_organization_id = self.openai_organization_id

        bedrock_region: None | str | Unset
        if isinstance(self.bedrock_region, Unset):
            bedrock_region = UNSET
        else:
            bedrock_region = self.bedrock_region

        bedrock_auth_mode: None | str | Unset
        if isinstance(self.bedrock_auth_mode, Unset):
            bedrock_auth_mode = UNSET
        elif isinstance(self.bedrock_auth_mode, ModelProviderReadBedrockAuthModeType0):
            bedrock_auth_mode = self.bedrock_auth_mode.value
        else:
            bedrock_auth_mode = self.bedrock_auth_mode

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "provider": provider,
                "configured": configured,
                "enabled": enabled,
                "has_credentials": has_credentials,
                "secret_hint": secret_hint,
                "base_url": base_url,
                "env_fallback": env_fallback,
                "last_verified_at": last_verified_at,
                "last_verified_status": last_verified_status,
                "updated_at": updated_at,
            }
        )
        if openai_organization_id is not UNSET:
            field_dict["openai_organization_id"] = openai_organization_id
        if bedrock_region is not UNSET:
            field_dict["bedrock_region"] = bedrock_region
        if bedrock_auth_mode is not UNSET:
            field_dict["bedrock_auth_mode"] = bedrock_auth_mode

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        provider = ModelProviderReadProvider(d.pop("provider"))

        configured = d.pop("configured")

        enabled = d.pop("enabled")

        has_credentials = d.pop("has_credentials")

        def _parse_secret_hint(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        secret_hint = _parse_secret_hint(d.pop("secret_hint"))

        def _parse_base_url(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        base_url = _parse_base_url(d.pop("base_url"))

        env_fallback = d.pop("env_fallback")

        def _parse_last_verified_at(data: object) -> datetime.datetime | None:
            if data is None:
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                last_verified_at_type_0 = datetime.datetime.fromisoformat(data)

                return last_verified_at_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None, data)

        last_verified_at = _parse_last_verified_at(d.pop("last_verified_at"))

        def _parse_last_verified_status(
            data: object,
        ) -> ModelProviderReadLastVerifiedStatusType0 | None:
            if data is None:
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                last_verified_status_type_0 = ModelProviderReadLastVerifiedStatusType0(data)

                return last_verified_status_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(ModelProviderReadLastVerifiedStatusType0 | None, data)

        last_verified_status = _parse_last_verified_status(d.pop("last_verified_status"))

        def _parse_updated_at(data: object) -> datetime.datetime | None:
            if data is None:
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                updated_at_type_0 = datetime.datetime.fromisoformat(data)

                return updated_at_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None, data)

        updated_at = _parse_updated_at(d.pop("updated_at"))

        def _parse_openai_organization_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        openai_organization_id = _parse_openai_organization_id(
            d.pop("openai_organization_id", UNSET)
        )

        def _parse_bedrock_region(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        bedrock_region = _parse_bedrock_region(d.pop("bedrock_region", UNSET))

        def _parse_bedrock_auth_mode(
            data: object,
        ) -> ModelProviderReadBedrockAuthModeType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                bedrock_auth_mode_type_0 = ModelProviderReadBedrockAuthModeType0(data)

                return bedrock_auth_mode_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(ModelProviderReadBedrockAuthModeType0 | None | Unset, data)

        bedrock_auth_mode = _parse_bedrock_auth_mode(d.pop("bedrock_auth_mode", UNSET))

        model_provider_read = cls(
            provider=provider,
            configured=configured,
            enabled=enabled,
            has_credentials=has_credentials,
            secret_hint=secret_hint,
            base_url=base_url,
            env_fallback=env_fallback,
            last_verified_at=last_verified_at,
            last_verified_status=last_verified_status,
            updated_at=updated_at,
            openai_organization_id=openai_organization_id,
            bedrock_region=bedrock_region,
            bedrock_auth_mode=bedrock_auth_mode,
        )

        model_provider_read.additional_properties = d
        return model_provider_read

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
