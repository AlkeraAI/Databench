from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar
from uuid import UUID

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.chat_message_read_role import ChatMessageReadRole

if TYPE_CHECKING:
    from ..models.chat_message_payload import ChatMessagePayload


T = TypeVar("T", bound="ChatMessageRead")


@_attrs_define
class ChatMessageRead:
    """One transcript entry. ``payload`` is the harness event as published —
    capped, so a large tool result arrives as a preview plus a handle.

        Attributes:
            id (UUID):
            chat_id (UUID):
            seq (int):
            role (ChatMessageReadRole):
            kind (str):
            event_id (str):
            payload (ChatMessagePayload):
            created_at (datetime.datetime):
    """

    id: UUID
    chat_id: UUID
    seq: int
    role: ChatMessageReadRole
    kind: str
    event_id: str
    payload: ChatMessagePayload
    created_at: datetime.datetime
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        id = str(self.id)

        chat_id = str(self.chat_id)

        seq = self.seq

        role = self.role.value

        kind = self.kind

        event_id = self.event_id

        payload = self.payload.to_dict()

        created_at = self.created_at.isoformat()

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "chat_id": chat_id,
                "seq": seq,
                "role": role,
                "kind": kind,
                "event_id": event_id,
                "payload": payload,
                "created_at": created_at,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.chat_message_payload import ChatMessagePayload

        d = dict(src_dict)
        id = UUID(d.pop("id"))

        chat_id = UUID(d.pop("chat_id"))

        seq = d.pop("seq")

        role = ChatMessageReadRole(d.pop("role"))

        kind = d.pop("kind")

        event_id = d.pop("event_id")

        payload = ChatMessagePayload.from_dict(d.pop("payload"))

        created_at = datetime.datetime.fromisoformat(d.pop("created_at"))

        chat_message_read = cls(
            id=id,
            chat_id=chat_id,
            seq=seq,
            role=role,
            kind=kind,
            event_id=event_id,
            payload=payload,
            created_at=created_at,
        )

        chat_message_read.additional_properties = d
        return chat_message_read

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
