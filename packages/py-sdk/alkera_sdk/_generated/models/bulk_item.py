from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.bulk_item_conflictbehavior import BulkItemConflictbehavior
from ..models.bulk_item_op import BulkItemOp
from ..types import UNSET, Unset

T = TypeVar("T", bound="BulkItem")


@_attrs_define
class BulkItem:
    """One change in a batch.

    ``id`` is the client's own correlation handle: the results come back
    carrying it, so a caller matches answers to requests without relying on
    order. It is never an id this server issued.

        Attributes:
            id (str):
            op (BulkItemOp):
            item_id (None | str | Unset):
            parent_id (None | str | Unset):
            name (None | str | Unset):
            if_match (int | None | Unset):
            conflict_behavior (BulkItemConflictbehavior | Unset):  Default: BulkItemConflictbehavior.FAIL.
    """

    id: str
    op: BulkItemOp
    item_id: None | str | Unset = UNSET
    parent_id: None | str | Unset = UNSET
    name: None | str | Unset = UNSET
    if_match: int | None | Unset = UNSET
    conflict_behavior: BulkItemConflictbehavior | Unset = BulkItemConflictbehavior.FAIL
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        id = self.id

        op = self.op.value

        item_id: None | str | Unset
        if isinstance(self.item_id, Unset):
            item_id = UNSET
        else:
            item_id = self.item_id

        parent_id: None | str | Unset
        if isinstance(self.parent_id, Unset):
            parent_id = UNSET
        else:
            parent_id = self.parent_id

        name: None | str | Unset
        if isinstance(self.name, Unset):
            name = UNSET
        else:
            name = self.name

        if_match: int | None | Unset
        if isinstance(self.if_match, Unset):
            if_match = UNSET
        else:
            if_match = self.if_match

        conflict_behavior: str | Unset = UNSET
        if not isinstance(self.conflict_behavior, Unset):
            conflict_behavior = self.conflict_behavior.value

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "op": op,
            }
        )
        if item_id is not UNSET:
            field_dict["itemId"] = item_id
        if parent_id is not UNSET:
            field_dict["parentId"] = parent_id
        if name is not UNSET:
            field_dict["name"] = name
        if if_match is not UNSET:
            field_dict["ifMatch"] = if_match
        if conflict_behavior is not UNSET:
            field_dict["conflictBehavior"] = conflict_behavior

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        id = d.pop("id")

        op = BulkItemOp(d.pop("op"))

        def _parse_item_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        item_id = _parse_item_id(d.pop("itemId", UNSET))

        def _parse_parent_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        parent_id = _parse_parent_id(d.pop("parentId", UNSET))

        def _parse_name(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        name = _parse_name(d.pop("name", UNSET))

        def _parse_if_match(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        if_match = _parse_if_match(d.pop("ifMatch", UNSET))

        _conflict_behavior = d.pop("conflictBehavior", UNSET)
        conflict_behavior: BulkItemConflictbehavior | Unset
        if isinstance(_conflict_behavior, Unset):
            conflict_behavior = UNSET
        else:
            conflict_behavior = BulkItemConflictbehavior(_conflict_behavior)

        bulk_item = cls(
            id=id,
            op=op,
            item_id=item_id,
            parent_id=parent_id,
            name=name,
            if_match=if_match,
            conflict_behavior=conflict_behavior,
        )

        bulk_item.additional_properties = d
        return bulk_item

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
