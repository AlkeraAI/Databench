from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="PlacesRead")


@_attrs_define
class PlacesRead:
    """The caller's own home and the named folders inside it.

    Every field is nullable because every one of them is answered by a *read*
    when ``ensure`` did not name it: a member who has never saved a template
    has no ``Chat Templates`` folder, and inventing one on a page load would
    put a folder in their drive that they did not make.

        Attributes:
            home_id (None | str | Unset):
            chats_id (None | str | Unset):
            chat_templates_id (None | str | Unset):
    """

    home_id: None | str | Unset = UNSET
    chats_id: None | str | Unset = UNSET
    chat_templates_id: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        home_id: None | str | Unset
        if isinstance(self.home_id, Unset):
            home_id = UNSET
        else:
            home_id = self.home_id

        chats_id: None | str | Unset
        if isinstance(self.chats_id, Unset):
            chats_id = UNSET
        else:
            chats_id = self.chats_id

        chat_templates_id: None | str | Unset
        if isinstance(self.chat_templates_id, Unset):
            chat_templates_id = UNSET
        else:
            chat_templates_id = self.chat_templates_id

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if home_id is not UNSET:
            field_dict["homeId"] = home_id
        if chats_id is not UNSET:
            field_dict["chatsId"] = chats_id
        if chat_templates_id is not UNSET:
            field_dict["chatTemplatesId"] = chat_templates_id

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)

        def _parse_home_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        home_id = _parse_home_id(d.pop("homeId", UNSET))

        def _parse_chats_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        chats_id = _parse_chats_id(d.pop("chatsId", UNSET))

        def _parse_chat_templates_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        chat_templates_id = _parse_chat_templates_id(d.pop("chatTemplatesId", UNSET))

        places_read = cls(
            home_id=home_id,
            chats_id=chats_id,
            chat_templates_id=chat_templates_id,
        )

        places_read.additional_properties = d
        return places_read

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
