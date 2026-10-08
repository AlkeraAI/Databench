from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.model_provider_update_request_bedrock_auth_mode_type_0 import (
    ModelProviderUpdateRequestBedrockAuthModeType0,
)
from ..types import UNSET, Unset

T = TypeVar("T", bound="ModelProviderUpdateRequest")


@_attrs_define
class ModelProviderUpdateRequest:
    """Upsert one provider's config. Secret fields omitted/blank KEEP the stored
    value; changing ``base_url`` requires re-entering the key (anti-exfiltration
    — a stored valid key must never be silently repointed at a new endpoint).

        Attributes:
            enabled (bool | Unset):  Default: True.
            api_key (None | str | Unset):
            base_url (None | str | Unset):
            openai_organization_id (None | str | Unset):
            bedrock_region (None | str | Unset):
            bedrock_auth_mode (ModelProviderUpdateRequestBedrockAuthModeType0 | None | Unset):
            aws_access_key_id (None | str | Unset):
            aws_secret_access_key (None | str | Unset):
    """

    enabled: bool | Unset = True
    api_key: None | str | Unset = UNSET
    base_url: None | str | Unset = UNSET
    openai_organization_id: None | str | Unset = UNSET
    bedrock_region: None | str | Unset = UNSET
    bedrock_auth_mode: ModelProviderUpdateRequestBedrockAuthModeType0 | None | Unset = UNSET
    aws_access_key_id: None | str | Unset = UNSET
    aws_secret_access_key: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        enabled = self.enabled

        api_key: None | str | Unset
        if isinstance(self.api_key, Unset):
            api_key = UNSET
        else:
            api_key = self.api_key

        base_url: None | str | Unset
        if isinstance(self.base_url, Unset):
            base_url = UNSET
        else:
            base_url = self.base_url

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
        elif isinstance(self.bedrock_auth_mode, ModelProviderUpdateRequestBedrockAuthModeType0):
            bedrock_auth_mode = self.bedrock_auth_mode.value
        else:
            bedrock_auth_mode = self.bedrock_auth_mode

        aws_access_key_id: None | str | Unset
        if isinstance(self.aws_access_key_id, Unset):
            aws_access_key_id = UNSET
        else:
            aws_access_key_id = self.aws_access_key_id

        aws_secret_access_key: None | str | Unset
        if isinstance(self.aws_secret_access_key, Unset):
            aws_secret_access_key = UNSET
        else:
            aws_secret_access_key = self.aws_secret_access_key

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if enabled is not UNSET:
            field_dict["enabled"] = enabled
        if api_key is not UNSET:
            field_dict["api_key"] = api_key
        if base_url is not UNSET:
            field_dict["base_url"] = base_url
        if openai_organization_id is not UNSET:
            field_dict["openai_organization_id"] = openai_organization_id
        if bedrock_region is not UNSET:
            field_dict["bedrock_region"] = bedrock_region
        if bedrock_auth_mode is not UNSET:
            field_dict["bedrock_auth_mode"] = bedrock_auth_mode
        if aws_access_key_id is not UNSET:
            field_dict["aws_access_key_id"] = aws_access_key_id
        if aws_secret_access_key is not UNSET:
            field_dict["aws_secret_access_key"] = aws_secret_access_key

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        enabled = d.pop("enabled", UNSET)

        def _parse_api_key(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        api_key = _parse_api_key(d.pop("api_key", UNSET))

        def _parse_base_url(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        base_url = _parse_base_url(d.pop("base_url", UNSET))

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
        ) -> ModelProviderUpdateRequestBedrockAuthModeType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                bedrock_auth_mode_type_0 = ModelProviderUpdateRequestBedrockAuthModeType0(data)

                return bedrock_auth_mode_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(ModelProviderUpdateRequestBedrockAuthModeType0 | None | Unset, data)

        bedrock_auth_mode = _parse_bedrock_auth_mode(d.pop("bedrock_auth_mode", UNSET))

        def _parse_aws_access_key_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        aws_access_key_id = _parse_aws_access_key_id(d.pop("aws_access_key_id", UNSET))

        def _parse_aws_secret_access_key(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        aws_secret_access_key = _parse_aws_secret_access_key(d.pop("aws_secret_access_key", UNSET))

        model_provider_update_request = cls(
            enabled=enabled,
            api_key=api_key,
            base_url=base_url,
            openai_organization_id=openai_organization_id,
            bedrock_region=bedrock_region,
            bedrock_auth_mode=bedrock_auth_mode,
            aws_access_key_id=aws_access_key_id,
            aws_secret_access_key=aws_secret_access_key,
        )

        model_provider_update_request.additional_properties = d
        return model_provider_update_request

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
