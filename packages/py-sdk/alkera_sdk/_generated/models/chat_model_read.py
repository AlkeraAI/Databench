from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.chat_model_read_wire import ChatModelReadWire
from ..types import UNSET, Unset

T = TypeVar("T", bound="ChatModelRead")


@_attrs_define
class ChatModelRead:
    """One model the gateway can serve this caller, as the picker reads it.

    The fields are the gateway catalog's own. ``efforts`` is the set of
    reasoning-effort variants the model offers (empty = no variant choice), and
    ``default_effort`` is the one the catalog prefers.

        Attributes:
            id (str):
            display_name (str):
            wire (ChatModelReadWire):
            efforts (list[str] | Unset):
            default_effort (None | str | Unset):
            family (str | Unset):  Default: ''.
            context_window (int | Unset):  Default: 0.
            reasoning_format (None | str | Unset):
            reads_reasoning_formats (list[str] | Unset):
    """

    id: str
    display_name: str
    wire: ChatModelReadWire
    efforts: list[str] | Unset = UNSET
    default_effort: None | str | Unset = UNSET
    family: str | Unset = ""
    context_window: int | Unset = 0
    reasoning_format: None | str | Unset = UNSET
    reads_reasoning_formats: list[str] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        id = self.id

        display_name = self.display_name

        wire = self.wire.value

        efforts: list[str] | Unset = UNSET
        if not isinstance(self.efforts, Unset):
            efforts = self.efforts

        default_effort: None | str | Unset
        if isinstance(self.default_effort, Unset):
            default_effort = UNSET
        else:
            default_effort = self.default_effort

        family = self.family

        context_window = self.context_window

        reasoning_format: None | str | Unset
        if isinstance(self.reasoning_format, Unset):
            reasoning_format = UNSET
        else:
            reasoning_format = self.reasoning_format

        reads_reasoning_formats: list[str] | Unset = UNSET
        if not isinstance(self.reads_reasoning_formats, Unset):
            reads_reasoning_formats = self.reads_reasoning_formats

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "display_name": display_name,
                "wire": wire,
            }
        )
        if efforts is not UNSET:
            field_dict["efforts"] = efforts
        if default_effort is not UNSET:
            field_dict["default_effort"] = default_effort
        if family is not UNSET:
            field_dict["family"] = family
        if context_window is not UNSET:
            field_dict["context_window"] = context_window
        if reasoning_format is not UNSET:
            field_dict["reasoning_format"] = reasoning_format
        if reads_reasoning_formats is not UNSET:
            field_dict["reads_reasoning_formats"] = reads_reasoning_formats

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        id = d.pop("id")

        display_name = d.pop("display_name")

        wire = ChatModelReadWire(d.pop("wire"))

        efforts = cast(list[str], d.pop("efforts", UNSET))

        def _parse_default_effort(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        default_effort = _parse_default_effort(d.pop("default_effort", UNSET))

        family = d.pop("family", UNSET)

        context_window = d.pop("context_window", UNSET)

        def _parse_reasoning_format(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        reasoning_format = _parse_reasoning_format(d.pop("reasoning_format", UNSET))

        reads_reasoning_formats = cast(list[str], d.pop("reads_reasoning_formats", UNSET))

        chat_model_read = cls(
            id=id,
            display_name=display_name,
            wire=wire,
            efforts=efforts,
            default_effort=default_effort,
            family=family,
            context_window=context_window,
            reasoning_format=reasoning_format,
            reads_reasoning_formats=reads_reasoning_formats,
        )

        chat_model_read.additional_properties = d
        return chat_model_read

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
