from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="OrgComputeSettingsRead")


@_attrs_define
class OrgComputeSettingsRead:
    """
    Attributes:
        shared_pool_fallback (bool):
        min_awake_pool (int):
        version (int):
        default_org_machine_id (None | str | Unset):
    """

    shared_pool_fallback: bool
    min_awake_pool: int
    version: int
    default_org_machine_id: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        shared_pool_fallback = self.shared_pool_fallback

        min_awake_pool = self.min_awake_pool

        version = self.version

        default_org_machine_id: None | str | Unset
        if isinstance(self.default_org_machine_id, Unset):
            default_org_machine_id = UNSET
        else:
            default_org_machine_id = self.default_org_machine_id

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "shared_pool_fallback": shared_pool_fallback,
                "min_awake_pool": min_awake_pool,
                "version": version,
            }
        )
        if default_org_machine_id is not UNSET:
            field_dict["default_org_machine_id"] = default_org_machine_id

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        shared_pool_fallback = d.pop("shared_pool_fallback")

        min_awake_pool = d.pop("min_awake_pool")

        version = d.pop("version")

        def _parse_default_org_machine_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        default_org_machine_id = _parse_default_org_machine_id(
            d.pop("default_org_machine_id", UNSET)
        )

        org_compute_settings_read = cls(
            shared_pool_fallback=shared_pool_fallback,
            min_awake_pool=min_awake_pool,
            version=version,
            default_org_machine_id=default_org_machine_id,
        )

        org_compute_settings_read.additional_properties = d
        return org_compute_settings_read

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
