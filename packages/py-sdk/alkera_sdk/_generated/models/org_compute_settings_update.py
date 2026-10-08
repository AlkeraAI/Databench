from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="OrgComputeSettingsUpdate")


@_attrs_define
class OrgComputeSettingsUpdate:
    """
    Attributes:
        shared_pool_fallback (bool | None | Unset):
        min_awake_pool (int | None | Unset):
        default_org_machine_id (None | str | Unset):
    """

    shared_pool_fallback: bool | None | Unset = UNSET
    min_awake_pool: int | None | Unset = UNSET
    default_org_machine_id: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        shared_pool_fallback: bool | None | Unset
        if isinstance(self.shared_pool_fallback, Unset):
            shared_pool_fallback = UNSET
        else:
            shared_pool_fallback = self.shared_pool_fallback

        min_awake_pool: int | None | Unset
        if isinstance(self.min_awake_pool, Unset):
            min_awake_pool = UNSET
        else:
            min_awake_pool = self.min_awake_pool

        default_org_machine_id: None | str | Unset
        if isinstance(self.default_org_machine_id, Unset):
            default_org_machine_id = UNSET
        else:
            default_org_machine_id = self.default_org_machine_id

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if shared_pool_fallback is not UNSET:
            field_dict["shared_pool_fallback"] = shared_pool_fallback
        if min_awake_pool is not UNSET:
            field_dict["min_awake_pool"] = min_awake_pool
        if default_org_machine_id is not UNSET:
            field_dict["default_org_machine_id"] = default_org_machine_id

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)

        def _parse_shared_pool_fallback(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        shared_pool_fallback = _parse_shared_pool_fallback(d.pop("shared_pool_fallback", UNSET))

        def _parse_min_awake_pool(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        min_awake_pool = _parse_min_awake_pool(d.pop("min_awake_pool", UNSET))

        def _parse_default_org_machine_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        default_org_machine_id = _parse_default_org_machine_id(
            d.pop("default_org_machine_id", UNSET)
        )

        org_compute_settings_update = cls(
            shared_pool_fallback=shared_pool_fallback,
            min_awake_pool=min_awake_pool,
            default_org_machine_id=default_org_machine_id,
        )

        org_compute_settings_update.additional_properties = d
        return org_compute_settings_update

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
