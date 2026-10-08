from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

T = TypeVar("T", bound="TreeBatchAnswer")


@_attrs_define
class TreeBatchAnswer:
    """What a batch did.

    Attributes:
        live_seq (int):
        applied (int):
        landing_count (int):
    """

    live_seq: int
    applied: int
    landing_count: int
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        live_seq = self.live_seq

        applied = self.applied

        landing_count = self.landing_count

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "live_seq": live_seq,
                "applied": applied,
                "landing_count": landing_count,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        live_seq = d.pop("live_seq")

        applied = d.pop("applied")

        landing_count = d.pop("landing_count")

        tree_batch_answer = cls(
            live_seq=live_seq,
            applied=applied,
            landing_count=landing_count,
        )

        tree_batch_answer.additional_properties = d
        return tree_batch_answer

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
