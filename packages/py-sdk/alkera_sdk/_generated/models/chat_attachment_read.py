from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar
from uuid import UUID

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.chat_attachment_read_state import ChatAttachmentReadState
from ..types import UNSET, Unset

T = TypeVar("T", bound="ChatAttachmentRead")


@_attrs_define
class ChatAttachmentRead:
    """One linked node, as this caller sees it right now.

    ``state`` is what a reader can do with it *at this moment*: ``available``
    carries the node's name, size and type; ``unavailable`` carries none of
    them. A chat member already knows the chat holds this reference — it is in
    the chat's own spec — so withholding the row entirely would only make an
    attachment they cannot open look like one that was never there, while
    naming it would hand them a file's name they have no claim to.

        Attributes:
            node_id (UUID):
            name (str):
            size (int):
            mime (str):
            state (ChatAttachmentReadState | Unset):  Default: ChatAttachmentReadState.AVAILABLE.
    """

    node_id: UUID
    name: str
    size: int
    mime: str
    state: ChatAttachmentReadState | Unset = ChatAttachmentReadState.AVAILABLE
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        node_id = str(self.node_id)

        name = self.name

        size = self.size

        mime = self.mime

        state: str | Unset = UNSET
        if not isinstance(self.state, Unset):
            state = self.state.value

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "nodeId": node_id,
                "name": name,
                "size": size,
                "mime": mime,
            }
        )
        if state is not UNSET:
            field_dict["state"] = state

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        node_id = UUID(d.pop("nodeId"))

        name = d.pop("name")

        size = d.pop("size")

        mime = d.pop("mime")

        _state = d.pop("state", UNSET)
        state: ChatAttachmentReadState | Unset
        if isinstance(_state, Unset):
            state = UNSET
        else:
            state = ChatAttachmentReadState(_state)

        chat_attachment_read = cls(
            node_id=node_id,
            name=name,
            size=size,
            mime=mime,
            state=state,
        )

        chat_attachment_read.additional_properties = d
        return chat_attachment_read

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
