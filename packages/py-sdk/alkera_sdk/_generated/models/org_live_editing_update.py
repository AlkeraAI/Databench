from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

T = TypeVar("T", bound="OrgLiveEditingUpdate")


@_attrs_define
class OrgLiveEditingUpdate:
    """The org's own setting: ``true`` or ``false`` wins over the deployment's
    in either direction; ``null`` clears it, so the org follows the
    deployment again. The key is required, so an empty body changes
    nothing by accident; only a JSON boolean is a setting (``"off"`` or
    ``0`` is a 422, never read as false).

        Attributes:
            enabled (bool | None):
    """

    enabled: bool | None
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        enabled: bool | None
        enabled = self.enabled

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "enabled": enabled,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)

        def _parse_enabled(data: object) -> bool | None:
            if data is None:
                return data
            return cast(bool | None, data)

        enabled = _parse_enabled(d.pop("enabled"))

        org_live_editing_update = cls(
            enabled=enabled,
        )

        org_live_editing_update.additional_properties = d
        return org_live_editing_update

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
