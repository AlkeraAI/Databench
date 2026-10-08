from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.chat_workspace_read_state import ChatWorkspaceReadState


T = TypeVar("T", bound="ChatWorkspaceRead")


@_attrs_define
class ChatWorkspaceRead:
    """The layout, and when this reader last saved it (``null`` = never).

    ``state`` is a dumped workspace document. It is declared as a free-form
    object rather than the model itself because the persisted shape carries the
    ``metadata`` bag every versioned model has, and two schemas offering a bag
    under the same generic name collide in the generated Python client — the
    generator drops BOTH rather than naming one of them. The document's own
    model stays the single source of truth: every write is parsed through it
    and every answer is built from it.

        Attributes:
            state (ChatWorkspaceReadState | Unset):
            updated_at (datetime.datetime | None | Unset):
    """

    state: ChatWorkspaceReadState | Unset = UNSET
    updated_at: datetime.datetime | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        state: dict[str, Any] | Unset = UNSET
        if not isinstance(self.state, Unset):
            state = self.state.to_dict()

        updated_at: None | str | Unset
        if isinstance(self.updated_at, Unset):
            updated_at = UNSET
        elif isinstance(self.updated_at, datetime.datetime):
            updated_at = self.updated_at.isoformat()
        else:
            updated_at = self.updated_at

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if state is not UNSET:
            field_dict["state"] = state
        if updated_at is not UNSET:
            field_dict["updated_at"] = updated_at

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.chat_workspace_read_state import ChatWorkspaceReadState

        d = dict(src_dict)
        _state = d.pop("state", UNSET)
        state: ChatWorkspaceReadState | Unset
        if isinstance(_state, Unset):
            state = UNSET
        else:
            state = ChatWorkspaceReadState.from_dict(_state)

        def _parse_updated_at(data: object) -> datetime.datetime | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                updated_at_type_0 = datetime.datetime.fromisoformat(data)

                return updated_at_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None | Unset, data)

        updated_at = _parse_updated_at(d.pop("updated_at", UNSET))

        chat_workspace_read = cls(
            state=state,
            updated_at=updated_at,
        )

        chat_workspace_read.additional_properties = d
        return chat_workspace_read

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
