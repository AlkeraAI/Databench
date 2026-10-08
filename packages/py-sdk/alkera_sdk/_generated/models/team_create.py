from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast
from uuid import UUID

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="TeamCreate")


@_attrs_define
class TeamCreate:
    """
    Attributes:
        name (str):
        parent_team_id (None | Unset | UUID):
    """

    name: str
    parent_team_id: None | Unset | UUID = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        name = self.name

        parent_team_id: None | str | Unset
        if isinstance(self.parent_team_id, Unset):
            parent_team_id = UNSET
        elif isinstance(self.parent_team_id, UUID):
            parent_team_id = str(self.parent_team_id)
        else:
            parent_team_id = self.parent_team_id

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "name": name,
            }
        )
        if parent_team_id is not UNSET:
            field_dict["parent_team_id"] = parent_team_id

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        name = d.pop("name")

        def _parse_parent_team_id(data: object) -> None | Unset | UUID:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                parent_team_id_type_0 = UUID(data)

                return parent_team_id_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | Unset | UUID, data)

        parent_team_id = _parse_parent_team_id(d.pop("parent_team_id", UNSET))

        team_create = cls(
            name=name,
            parent_team_id=parent_team_id,
        )

        team_create.additional_properties = d
        return team_create

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
