from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

if TYPE_CHECKING:
    from ..models.frame_attached_opens_item import FrameAttachedOpensItem


T = TypeVar("T", bound="FrameAttached")


@_attrs_define
class FrameAttached:
    """The frame's id and the comm-open replays of its model closure.

    Attributes:
        frame_id (str):
        opens (list[FrameAttachedOpensItem]):
    """

    frame_id: str
    opens: list[FrameAttachedOpensItem]
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        frame_id = self.frame_id

        opens = []
        for opens_item_data in self.opens:
            opens_item = opens_item_data.to_dict()
            opens.append(opens_item)

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "frame_id": frame_id,
                "opens": opens,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.frame_attached_opens_item import FrameAttachedOpensItem

        d = dict(src_dict)
        frame_id = d.pop("frame_id")

        opens = []
        _opens = d.pop("opens")
        for opens_item_data in _opens:
            opens_item = FrameAttachedOpensItem.from_dict(opens_item_data)

            opens.append(opens_item)

        frame_attached = cls(
            frame_id=frame_id,
            opens=opens,
        )

        frame_attached.additional_properties = d
        return frame_attached

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
