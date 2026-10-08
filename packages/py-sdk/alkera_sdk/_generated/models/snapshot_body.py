from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.snapshot_body_changes_item import SnapshotBodyChangesItem


T = TypeVar("T", bound="SnapshotBody")


@_attrs_define
class SnapshotBody:
    """
    Attributes:
        epoch (int | None | Unset):
        instance_id (None | str | Unset):
        changes (list[SnapshotBodyChangesItem] | Unset):
    """

    epoch: int | None | Unset = UNSET
    instance_id: None | str | Unset = UNSET
    changes: list[SnapshotBodyChangesItem] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        epoch: int | None | Unset
        if isinstance(self.epoch, Unset):
            epoch = UNSET
        else:
            epoch = self.epoch

        instance_id: None | str | Unset
        if isinstance(self.instance_id, Unset):
            instance_id = UNSET
        else:
            instance_id = self.instance_id

        changes: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.changes, Unset):
            changes = []
            for changes_item_data in self.changes:
                changes_item = changes_item_data.to_dict()
                changes.append(changes_item)

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if epoch is not UNSET:
            field_dict["epoch"] = epoch
        if instance_id is not UNSET:
            field_dict["instanceId"] = instance_id
        if changes is not UNSET:
            field_dict["changes"] = changes

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.snapshot_body_changes_item import SnapshotBodyChangesItem

        d = dict(src_dict)

        def _parse_epoch(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        epoch = _parse_epoch(d.pop("epoch", UNSET))

        def _parse_instance_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        instance_id = _parse_instance_id(d.pop("instanceId", UNSET))

        _changes = d.pop("changes", UNSET)
        changes: list[SnapshotBodyChangesItem] | Unset = UNSET
        if _changes is not UNSET:
            changes = []
            for changes_item_data in _changes:
                changes_item = SnapshotBodyChangesItem.from_dict(changes_item_data)

                changes.append(changes_item)

        snapshot_body = cls(
            epoch=epoch,
            instance_id=instance_id,
            changes=changes,
        )

        snapshot_body.additional_properties = d
        return snapshot_body

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
