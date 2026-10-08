from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar
from uuid import UUID

from attrs import define as _attrs_define
from attrs import field as _attrs_field

T = TypeVar("T", bound="ChatReadStateRead")


@_attrs_define
class ChatReadStateRead:
    """The caller's own read state of one chat after a mark moved.

    Attributes:
        chat_id (UUID):
        unread (bool):
        needs_you (bool):
    """

    chat_id: UUID
    unread: bool
    needs_you: bool
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        chat_id = str(self.chat_id)

        unread = self.unread

        needs_you = self.needs_you

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "chat_id": chat_id,
                "unread": unread,
                "needs_you": needs_you,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        chat_id = UUID(d.pop("chat_id"))

        unread = d.pop("unread")

        needs_you = d.pop("needs_you")

        chat_read_state_read = cls(
            chat_id=chat_id,
            unread=unread,
            needs_you=needs_you,
        )

        chat_read_state_read.additional_properties = d
        return chat_read_state_read

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
