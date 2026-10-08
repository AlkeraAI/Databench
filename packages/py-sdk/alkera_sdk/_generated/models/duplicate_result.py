from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast
from uuid import UUID

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.item import Item


T = TypeVar("T", bound="DuplicateResult")


@_attrs_define
class DuplicateResult:
    """What a duplicate answers: the new node, and the new object when the
    node stands for one — the chat id a client opens next.

        Attributes:
            item (Item): The one item payload every Files surface returns.
            chat_id (None | Unset | UUID):
            object_id (None | Unset | UUID):
    """

    item: Item
    chat_id: None | Unset | UUID = UNSET
    object_id: None | Unset | UUID = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        item = self.item.to_dict()

        chat_id: None | str | Unset
        if isinstance(self.chat_id, Unset):
            chat_id = UNSET
        elif isinstance(self.chat_id, UUID):
            chat_id = str(self.chat_id)
        else:
            chat_id = self.chat_id

        object_id: None | str | Unset
        if isinstance(self.object_id, Unset):
            object_id = UNSET
        elif isinstance(self.object_id, UUID):
            object_id = str(self.object_id)
        else:
            object_id = self.object_id

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "item": item,
            }
        )
        if chat_id is not UNSET:
            field_dict["chatId"] = chat_id
        if object_id is not UNSET:
            field_dict["objectId"] = object_id

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.item import Item

        d = dict(src_dict)
        item = Item.from_dict(d.pop("item"))

        def _parse_chat_id(data: object) -> None | Unset | UUID:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                chat_id_type_0 = UUID(data)

                return chat_id_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | Unset | UUID, data)

        chat_id = _parse_chat_id(d.pop("chatId", UNSET))

        def _parse_object_id(data: object) -> None | Unset | UUID:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                object_id_type_0 = UUID(data)

                return object_id_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | Unset | UUID, data)

        object_id = _parse_object_id(d.pop("objectId", UNSET))

        duplicate_result = cls(
            item=item,
            chat_id=chat_id,
            object_id=object_id,
        )

        duplicate_result.additional_properties = d
        return duplicate_result

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
