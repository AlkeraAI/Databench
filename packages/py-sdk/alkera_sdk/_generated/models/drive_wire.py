from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="DriveWire")


@_attrs_define
class DriveWire:
    """The org's drive as a client sees it.

    Attributes:
        id (str):
        org_id (str):
        root_id (str):
        home_id (None | str | Unset):
        quota_bytes (int | Unset):  Default: 0.
    """

    id: str
    org_id: str
    root_id: str
    home_id: None | str | Unset = UNSET
    quota_bytes: int | Unset = 0
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        id = self.id

        org_id = self.org_id

        root_id = self.root_id

        home_id: None | str | Unset
        if isinstance(self.home_id, Unset):
            home_id = UNSET
        else:
            home_id = self.home_id

        quota_bytes = self.quota_bytes

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "orgId": org_id,
                "rootId": root_id,
            }
        )
        if home_id is not UNSET:
            field_dict["homeId"] = home_id
        if quota_bytes is not UNSET:
            field_dict["quotaBytes"] = quota_bytes

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        id = d.pop("id")

        org_id = d.pop("orgId")

        root_id = d.pop("rootId")

        def _parse_home_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        home_id = _parse_home_id(d.pop("homeId", UNSET))

        quota_bytes = d.pop("quotaBytes", UNSET)

        drive_wire = cls(
            id=id,
            org_id=org_id,
            root_id=root_id,
            home_id=home_id,
            quota_bytes=quota_bytes,
        )

        drive_wire.additional_properties = d
        return drive_wire

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
