from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="OrgStorageLimitUpdate")


@_attrs_define
class OrgStorageLimitUpdate:
    """``null`` is an explicit "unlimited"; a figure is a ceiling in bytes.

    Attributes:
        limit_bytes (int | None | Unset):
    """

    limit_bytes: int | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        limit_bytes: int | None | Unset
        if isinstance(self.limit_bytes, Unset):
            limit_bytes = UNSET
        else:
            limit_bytes = self.limit_bytes

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if limit_bytes is not UNSET:
            field_dict["limit_bytes"] = limit_bytes

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)

        def _parse_limit_bytes(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        limit_bytes = _parse_limit_bytes(d.pop("limit_bytes", UNSET))

        org_storage_limit_update = cls(
            limit_bytes=limit_bytes,
        )

        org_storage_limit_update.additional_properties = d
        return org_storage_limit_update

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
