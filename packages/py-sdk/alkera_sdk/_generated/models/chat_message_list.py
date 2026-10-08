from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.chat_message_read import ChatMessageRead


T = TypeVar("T", bound="ChatMessageList")


@_attrs_define
class ChatMessageList:
    """A page of transcript, with an in-band reset marker.

    ``resync_from`` is set when the caller asked for messages older than the
    oldest one still held: rather than an error and a protocol branch, the page
    itself says "start again from here" and the client re-reads.

        Attributes:
            items (list[ChatMessageRead]):
            next_after_seq (int):
            resync_from (int | None | Unset):
            prev_before (int | None | Unset):
            has_older (bool | Unset):  Default: False.
            cut (bool | Unset):  Default: False.
    """

    items: list[ChatMessageRead]
    next_after_seq: int
    resync_from: int | None | Unset = UNSET
    prev_before: int | None | Unset = UNSET
    has_older: bool | Unset = False
    cut: bool | Unset = False
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        items = []
        for items_item_data in self.items:
            items_item = items_item_data.to_dict()
            items.append(items_item)

        next_after_seq = self.next_after_seq

        resync_from: int | None | Unset
        if isinstance(self.resync_from, Unset):
            resync_from = UNSET
        else:
            resync_from = self.resync_from

        prev_before: int | None | Unset
        if isinstance(self.prev_before, Unset):
            prev_before = UNSET
        else:
            prev_before = self.prev_before

        has_older = self.has_older

        cut = self.cut

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "items": items,
                "next_after_seq": next_after_seq,
            }
        )
        if resync_from is not UNSET:
            field_dict["resync_from"] = resync_from
        if prev_before is not UNSET:
            field_dict["prev_before"] = prev_before
        if has_older is not UNSET:
            field_dict["has_older"] = has_older
        if cut is not UNSET:
            field_dict["cut"] = cut

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.chat_message_read import ChatMessageRead

        d = dict(src_dict)
        items = []
        _items = d.pop("items")
        for items_item_data in _items:
            items_item = ChatMessageRead.from_dict(items_item_data)

            items.append(items_item)

        next_after_seq = d.pop("next_after_seq")

        def _parse_resync_from(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        resync_from = _parse_resync_from(d.pop("resync_from", UNSET))

        def _parse_prev_before(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        prev_before = _parse_prev_before(d.pop("prev_before", UNSET))

        has_older = d.pop("has_older", UNSET)

        cut = d.pop("cut", UNSET)

        chat_message_list = cls(
            items=items,
            next_after_seq=next_after_seq,
            resync_from=resync_from,
            prev_before=prev_before,
            has_older=has_older,
            cut=cut,
        )

        chat_message_list.additional_properties = d
        return chat_message_list

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
