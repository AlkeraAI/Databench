from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.chat_workspace_write_state import ChatWorkspaceWriteState


T = TypeVar("T", bound="ChatWorkspaceWrite")


@_attrs_define
class ChatWorkspaceWrite:
    """A full replacement. The document is taken raw and parsed by the route so
    a refusal can name what was wrong with it rather than leaking the shape of
    the model through a framework error.

        Attributes:
            state (ChatWorkspaceWriteState | Unset):
    """

    state: ChatWorkspaceWriteState | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        state: dict[str, Any] | Unset = UNSET
        if not isinstance(self.state, Unset):
            state = self.state.to_dict()

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if state is not UNSET:
            field_dict["state"] = state

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.chat_workspace_write_state import ChatWorkspaceWriteState

        d = dict(src_dict)
        _state = d.pop("state", UNSET)
        state: ChatWorkspaceWriteState | Unset
        if isinstance(_state, Unset):
            state = UNSET
        else:
            state = ChatWorkspaceWriteState.from_dict(_state)

        chat_workspace_write = cls(
            state=state,
        )

        chat_workspace_write.additional_properties = d
        return chat_workspace_write

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
