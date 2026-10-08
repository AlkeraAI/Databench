from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar
from uuid import UUID

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="HeartbeatBatchEntry")


@_attrs_define
class HeartbeatBatchEntry:
    """
    Attributes:
        node_id (UUID):
        epoch (int):
        instance_id (str):
        synced (bool | Unset):  Default: False.
    """

    node_id: UUID
    epoch: int
    instance_id: str
    synced: bool | Unset = False
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        node_id = str(self.node_id)

        epoch = self.epoch

        instance_id = self.instance_id

        synced = self.synced

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "nodeId": node_id,
                "epoch": epoch,
                "instanceId": instance_id,
            }
        )
        if synced is not UNSET:
            field_dict["synced"] = synced

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        node_id = UUID(d.pop("nodeId"))

        epoch = d.pop("epoch")

        instance_id = d.pop("instanceId")

        synced = d.pop("synced", UNSET)

        heartbeat_batch_entry = cls(
            node_id=node_id,
            epoch=epoch,
            instance_id=instance_id,
            synced=synced,
        )

        heartbeat_batch_entry.additional_properties = d
        return heartbeat_batch_entry

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
