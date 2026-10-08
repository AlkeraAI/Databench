from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import Any, TypeVar, cast
from uuid import UUID

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.org_read_storage_limit_source_type_0 import OrgReadStorageLimitSourceType0
from ..types import UNSET, Unset

T = TypeVar("T", bound="OrgRead")


@_attrs_define
class OrgRead:
    """Same shape as TeamRead — separate name communicates intent at the
    admin route surface — plus, on the detail read, the org's storage picture.

        Attributes:
            name (str):
            id (UUID):
            parent_team_id (None | UUID):
            is_root (bool):
            created_at (datetime.datetime):
            member_count (int | Unset):  Default: 0.
            storage_limit_bytes (int | None | Unset):
            storage_limit_source (None | OrgReadStorageLimitSourceType0 | Unset):
            storage_used_bytes (int | None | Unset):
    """

    name: str
    id: UUID
    parent_team_id: None | UUID
    is_root: bool
    created_at: datetime.datetime
    member_count: int | Unset = 0
    storage_limit_bytes: int | None | Unset = UNSET
    storage_limit_source: None | OrgReadStorageLimitSourceType0 | Unset = UNSET
    storage_used_bytes: int | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        name = self.name

        id = str(self.id)

        parent_team_id: None | str
        if isinstance(self.parent_team_id, UUID):
            parent_team_id = str(self.parent_team_id)
        else:
            parent_team_id = self.parent_team_id

        is_root = self.is_root

        created_at = self.created_at.isoformat()

        member_count = self.member_count

        storage_limit_bytes: int | None | Unset
        if isinstance(self.storage_limit_bytes, Unset):
            storage_limit_bytes = UNSET
        else:
            storage_limit_bytes = self.storage_limit_bytes

        storage_limit_source: None | str | Unset
        if isinstance(self.storage_limit_source, Unset):
            storage_limit_source = UNSET
        elif isinstance(self.storage_limit_source, OrgReadStorageLimitSourceType0):
            storage_limit_source = self.storage_limit_source.value
        else:
            storage_limit_source = self.storage_limit_source

        storage_used_bytes: int | None | Unset
        if isinstance(self.storage_used_bytes, Unset):
            storage_used_bytes = UNSET
        else:
            storage_used_bytes = self.storage_used_bytes

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "name": name,
                "id": id,
                "parent_team_id": parent_team_id,
                "is_root": is_root,
                "created_at": created_at,
            }
        )
        if member_count is not UNSET:
            field_dict["member_count"] = member_count
        if storage_limit_bytes is not UNSET:
            field_dict["storage_limit_bytes"] = storage_limit_bytes
        if storage_limit_source is not UNSET:
            field_dict["storage_limit_source"] = storage_limit_source
        if storage_used_bytes is not UNSET:
            field_dict["storage_used_bytes"] = storage_used_bytes

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        name = d.pop("name")

        id = UUID(d.pop("id"))

        def _parse_parent_team_id(data: object) -> None | UUID:
            if data is None:
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                parent_team_id_type_0 = UUID(data)

                return parent_team_id_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | UUID, data)

        parent_team_id = _parse_parent_team_id(d.pop("parent_team_id"))

        is_root = d.pop("is_root")

        created_at = datetime.datetime.fromisoformat(d.pop("created_at"))

        member_count = d.pop("member_count", UNSET)

        def _parse_storage_limit_bytes(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        storage_limit_bytes = _parse_storage_limit_bytes(d.pop("storage_limit_bytes", UNSET))

        def _parse_storage_limit_source(
            data: object,
        ) -> None | OrgReadStorageLimitSourceType0 | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                storage_limit_source_type_0 = OrgReadStorageLimitSourceType0(data)

                return storage_limit_source_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | OrgReadStorageLimitSourceType0 | Unset, data)

        storage_limit_source = _parse_storage_limit_source(d.pop("storage_limit_source", UNSET))

        def _parse_storage_used_bytes(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        storage_used_bytes = _parse_storage_used_bytes(d.pop("storage_used_bytes", UNSET))

        org_read = cls(
            name=name,
            id=id,
            parent_team_id=parent_team_id,
            is_root=is_root,
            created_at=created_at,
            member_count=member_count,
            storage_limit_bytes=storage_limit_bytes,
            storage_limit_source=storage_limit_source,
            storage_used_bytes=storage_used_bytes,
        )

        org_read.additional_properties = d
        return org_read

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
