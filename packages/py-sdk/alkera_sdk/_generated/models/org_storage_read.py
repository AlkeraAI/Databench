from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import Any, TypeVar, cast
from uuid import UUID

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.org_storage_read_storage_limit_source import OrgStorageReadStorageLimitSource
from ..types import UNSET, Unset

T = TypeVar("T", bound="OrgStorageRead")


@_attrs_define
class OrgStorageRead:
    """An org's storage picture as the platform sees it.

    Attributes:
        org_id (UUID):
        plan_tier (str):
        storage_limit_bytes (int | None):
        storage_limit_source (OrgStorageReadStorageLimitSource):
        storage_used_bytes (int):
        override_set (bool):
        updated_at (datetime.datetime | None | Unset):
    """

    org_id: UUID
    plan_tier: str
    storage_limit_bytes: int | None
    storage_limit_source: OrgStorageReadStorageLimitSource
    storage_used_bytes: int
    override_set: bool
    updated_at: datetime.datetime | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        org_id = str(self.org_id)

        plan_tier = self.plan_tier

        storage_limit_bytes: int | None
        storage_limit_bytes = self.storage_limit_bytes

        storage_limit_source = self.storage_limit_source.value

        storage_used_bytes = self.storage_used_bytes

        override_set = self.override_set

        updated_at: None | str | Unset
        if isinstance(self.updated_at, Unset):
            updated_at = UNSET
        elif isinstance(self.updated_at, datetime.datetime):
            updated_at = self.updated_at.isoformat()
        else:
            updated_at = self.updated_at

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "org_id": org_id,
                "plan_tier": plan_tier,
                "storage_limit_bytes": storage_limit_bytes,
                "storage_limit_source": storage_limit_source,
                "storage_used_bytes": storage_used_bytes,
                "override_set": override_set,
            }
        )
        if updated_at is not UNSET:
            field_dict["updated_at"] = updated_at

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        org_id = UUID(d.pop("org_id"))

        plan_tier = d.pop("plan_tier")

        def _parse_storage_limit_bytes(data: object) -> int | None:
            if data is None:
                return data
            return cast(int | None, data)

        storage_limit_bytes = _parse_storage_limit_bytes(d.pop("storage_limit_bytes"))

        storage_limit_source = OrgStorageReadStorageLimitSource(d.pop("storage_limit_source"))

        storage_used_bytes = d.pop("storage_used_bytes")

        override_set = d.pop("override_set")

        def _parse_updated_at(data: object) -> datetime.datetime | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                updated_at_type_0 = datetime.datetime.fromisoformat(data)

                return updated_at_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None | Unset, data)

        updated_at = _parse_updated_at(d.pop("updated_at", UNSET))

        org_storage_read = cls(
            org_id=org_id,
            plan_tier=plan_tier,
            storage_limit_bytes=storage_limit_bytes,
            storage_limit_source=storage_limit_source,
            storage_used_bytes=storage_used_bytes,
            override_set=override_set,
            updated_at=updated_at,
        )

        org_storage_read.additional_properties = d
        return org_storage_read

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
