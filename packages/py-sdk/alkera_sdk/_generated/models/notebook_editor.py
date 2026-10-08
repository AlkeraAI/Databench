from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="NotebookEditor")


@_attrs_define
class NotebookEditor:
    """``GET .../editor``: where the notebook editor opens a notebook, or a
    new notebook in a folder. ``chat_id`` is the chat whose workspace pane
    runs it: the chat whose own folder holds it, or, in a workspace's folder,
    the chat a wake of that workspace goes through. ``None`` where no kernel
    can run (outside every workspace and chat folder, or in an ended one) or
    where the caller may send in no chat that would run it.

        Attributes:
            chat_id (None | str | Unset):
    """

    chat_id: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        chat_id: None | str | Unset
        if isinstance(self.chat_id, Unset):
            chat_id = UNSET
        else:
            chat_id = self.chat_id

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if chat_id is not UNSET:
            field_dict["chat_id"] = chat_id

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)

        def _parse_chat_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        chat_id = _parse_chat_id(d.pop("chat_id", UNSET))

        notebook_editor = cls(
            chat_id=chat_id,
        )

        notebook_editor.additional_properties = d
        return notebook_editor

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
