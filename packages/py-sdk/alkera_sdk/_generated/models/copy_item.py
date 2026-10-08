from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.copy_item_conflictbehavior import CopyItemConflictbehavior
from ..types import UNSET, Unset

T = TypeVar("T", bound="CopyItem")


@_attrs_define
class CopyItem:
    """The `POST …/copy` body.

    Attributes:
        parent_id (str):
        name (None | str | Unset):
        conflict_behavior (CopyItemConflictbehavior | Unset):  Default: CopyItemConflictbehavior.RENAME.
    """

    parent_id: str
    name: None | str | Unset = UNSET
    conflict_behavior: CopyItemConflictbehavior | Unset = CopyItemConflictbehavior.RENAME
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        parent_id = self.parent_id

        name: None | str | Unset
        if isinstance(self.name, Unset):
            name = UNSET
        else:
            name = self.name

        conflict_behavior: str | Unset = UNSET
        if not isinstance(self.conflict_behavior, Unset):
            conflict_behavior = self.conflict_behavior.value

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "parentId": parent_id,
            }
        )
        if name is not UNSET:
            field_dict["name"] = name
        if conflict_behavior is not UNSET:
            field_dict["conflictBehavior"] = conflict_behavior

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        parent_id = d.pop("parentId")

        def _parse_name(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        name = _parse_name(d.pop("name", UNSET))

        _conflict_behavior = d.pop("conflictBehavior", UNSET)
        conflict_behavior: CopyItemConflictbehavior | Unset
        if isinstance(_conflict_behavior, Unset):
            conflict_behavior = UNSET
        else:
            conflict_behavior = CopyItemConflictbehavior(_conflict_behavior)

        copy_item = cls(
            parent_id=parent_id,
            name=name,
            conflict_behavior=conflict_behavior,
        )

        copy_item.additional_properties = d
        return copy_item

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
