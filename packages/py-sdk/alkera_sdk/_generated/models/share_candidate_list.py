from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

if TYPE_CHECKING:
    from ..models.share_candidate import ShareCandidate


T = TypeVar("T", bound="ShareCandidateList")


@_attrs_define
class ShareCandidateList:
    """
    Attributes:
        value (list[ShareCandidate]):
    """

    value: list[ShareCandidate]
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        value = []
        for value_item_data in self.value:
            value_item = value_item_data.to_dict()
            value.append(value_item)

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "value": value,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.share_candidate import ShareCandidate

        d = dict(src_dict)
        value = []
        _value = d.pop("value")
        for value_item_data in _value:
            value_item = ShareCandidate.from_dict(value_item_data)

            value.append(value_item)

        share_candidate_list = cls(
            value=value,
        )

        share_candidate_list.additional_properties = d
        return share_candidate_list

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
