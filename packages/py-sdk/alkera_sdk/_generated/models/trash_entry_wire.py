from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.item import Item


T = TypeVar("T", bound="TrashEntryWire")


@_attrs_define
class TrashEntryWire:
    """One trashed root, with how long is left before the purge takes it.

    Attributes:
        trash_op_id (str):
        deleted_at (str):
        purge_after (str):
        time_left_seconds (int):
        item (Item): The one item payload every Files surface returns.
        original_parent_id (None | str | Unset):
        original_path (None | str | Unset):
        reason (None | str | Unset):
        reason_machine (None | str | Unset):
    """

    trash_op_id: str
    deleted_at: str
    purge_after: str
    time_left_seconds: int
    item: Item
    original_parent_id: None | str | Unset = UNSET
    original_path: None | str | Unset = UNSET
    reason: None | str | Unset = UNSET
    reason_machine: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        trash_op_id = self.trash_op_id

        deleted_at = self.deleted_at

        purge_after = self.purge_after

        time_left_seconds = self.time_left_seconds

        item = self.item.to_dict()

        original_parent_id: None | str | Unset
        if isinstance(self.original_parent_id, Unset):
            original_parent_id = UNSET
        else:
            original_parent_id = self.original_parent_id

        original_path: None | str | Unset
        if isinstance(self.original_path, Unset):
            original_path = UNSET
        else:
            original_path = self.original_path

        reason: None | str | Unset
        if isinstance(self.reason, Unset):
            reason = UNSET
        else:
            reason = self.reason

        reason_machine: None | str | Unset
        if isinstance(self.reason_machine, Unset):
            reason_machine = UNSET
        else:
            reason_machine = self.reason_machine

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "trashOpId": trash_op_id,
                "deletedAt": deleted_at,
                "purgeAfter": purge_after,
                "timeLeftSeconds": time_left_seconds,
                "item": item,
            }
        )
        if original_parent_id is not UNSET:
            field_dict["originalParentId"] = original_parent_id
        if original_path is not UNSET:
            field_dict["originalPath"] = original_path
        if reason is not UNSET:
            field_dict["reason"] = reason
        if reason_machine is not UNSET:
            field_dict["reasonMachine"] = reason_machine

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.item import Item

        d = dict(src_dict)
        trash_op_id = d.pop("trashOpId")

        deleted_at = d.pop("deletedAt")

        purge_after = d.pop("purgeAfter")

        time_left_seconds = d.pop("timeLeftSeconds")

        item = Item.from_dict(d.pop("item"))

        def _parse_original_parent_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        original_parent_id = _parse_original_parent_id(d.pop("originalParentId", UNSET))

        def _parse_original_path(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        original_path = _parse_original_path(d.pop("originalPath", UNSET))

        def _parse_reason(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        reason = _parse_reason(d.pop("reason", UNSET))

        def _parse_reason_machine(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        reason_machine = _parse_reason_machine(d.pop("reasonMachine", UNSET))

        trash_entry_wire = cls(
            trash_op_id=trash_op_id,
            deleted_at=deleted_at,
            purge_after=purge_after,
            time_left_seconds=time_left_seconds,
            item=item,
            original_parent_id=original_parent_id,
            original_path=original_path,
            reason=reason,
            reason_machine=reason_machine,
        )

        trash_entry_wire.additional_properties = d
        return trash_entry_wire

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
