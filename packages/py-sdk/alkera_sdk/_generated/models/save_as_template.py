from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast
from uuid import UUID

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="SaveAsTemplate")


@_attrs_define
class SaveAsTemplate:
    """Save a chat as the starting point for the next one.

    Everything but the chat is optional because the server can answer for all
    of it: the title from the chat, the brief from its transcript, and the
    destination from the caller's own ``Chat Templates`` folder. A reader who
    wants none of those defaults overrides the one they care about.

        Attributes:
            source_chat_id (UUID):
            title (None | str | Unset):
            brief (None | str | Unset):
            destination_id (None | Unset | UUID):
            client_id (None | str | Unset):
    """

    source_chat_id: UUID
    title: None | str | Unset = UNSET
    brief: None | str | Unset = UNSET
    destination_id: None | Unset | UUID = UNSET
    client_id: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        source_chat_id = str(self.source_chat_id)

        title: None | str | Unset
        if isinstance(self.title, Unset):
            title = UNSET
        else:
            title = self.title

        brief: None | str | Unset
        if isinstance(self.brief, Unset):
            brief = UNSET
        else:
            brief = self.brief

        destination_id: None | str | Unset
        if isinstance(self.destination_id, Unset):
            destination_id = UNSET
        elif isinstance(self.destination_id, UUID):
            destination_id = str(self.destination_id)
        else:
            destination_id = self.destination_id

        client_id: None | str | Unset
        if isinstance(self.client_id, Unset):
            client_id = UNSET
        else:
            client_id = self.client_id

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "source_chat_id": source_chat_id,
            }
        )
        if title is not UNSET:
            field_dict["title"] = title
        if brief is not UNSET:
            field_dict["brief"] = brief
        if destination_id is not UNSET:
            field_dict["destination_id"] = destination_id
        if client_id is not UNSET:
            field_dict["client_id"] = client_id

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        source_chat_id = UUID(d.pop("source_chat_id"))

        def _parse_title(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        title = _parse_title(d.pop("title", UNSET))

        def _parse_brief(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        brief = _parse_brief(d.pop("brief", UNSET))

        def _parse_destination_id(data: object) -> None | Unset | UUID:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                destination_id_type_0 = UUID(data)

                return destination_id_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | Unset | UUID, data)

        destination_id = _parse_destination_id(d.pop("destination_id", UNSET))

        def _parse_client_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        client_id = _parse_client_id(d.pop("client_id", UNSET))

        save_as_template = cls(
            source_chat_id=source_chat_id,
            title=title,
            brief=brief,
            destination_id=destination_id,
            client_id=client_id,
        )

        save_as_template.additional_properties = d
        return save_as_template

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
