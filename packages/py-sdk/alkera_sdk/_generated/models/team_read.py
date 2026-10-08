from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import Any, TypeVar, cast
from uuid import UUID

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="TeamRead")


@_attrs_define
class TeamRead:
    """
    Attributes:
        name (str):
        id (UUID):
        parent_team_id (None | UUID):
        is_root (bool):
        created_at (datetime.datetime):
        member_count (int | Unset):  Default: 0.
    """

    name: str
    id: UUID
    parent_team_id: None | UUID
    is_root: bool
    created_at: datetime.datetime
    member_count: int | Unset = 0
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

        team_read = cls(
            name=name,
            id=id,
            parent_team_id=parent_team_id,
            is_root=is_root,
            created_at=created_at,
            member_count=member_count,
        )

        team_read.additional_properties = d
        return team_read

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
