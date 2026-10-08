from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

if TYPE_CHECKING:
    from ..models.heartbeat_batch_verdict import HeartbeatBatchVerdict


T = TypeVar("T", bound="HeartbeatBatchAnswer")


@_attrs_define
class HeartbeatBatchAnswer:
    """
    Attributes:
        leases (list[HeartbeatBatchVerdict]):
    """

    leases: list[HeartbeatBatchVerdict]
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        leases = []
        for leases_item_data in self.leases:
            leases_item = leases_item_data.to_dict()
            leases.append(leases_item)

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "leases": leases,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.heartbeat_batch_verdict import HeartbeatBatchVerdict

        d = dict(src_dict)
        leases = []
        _leases = d.pop("leases")
        for leases_item_data in _leases:
            leases_item = HeartbeatBatchVerdict.from_dict(leases_item_data)

            leases.append(leases_item)

        heartbeat_batch_answer = cls(
            leases=leases,
        )

        heartbeat_batch_answer.additional_properties = d
        return heartbeat_batch_answer

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
