from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.chat_model_pin_wire import ChatModelPinWire
from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.metadata import Metadata


T = TypeVar("T", bound="ChatModelPin")


@_attrs_define
class ChatModelPin:
    """The model a chat was started on, as the gateway catalog described it.

    A chat's model is chosen once, when it is created, and the box that runs the
    chat has to file it under the right provider — so the pin carries the
    catalog FACTS the box needs (``wire``, the offered ``efforts``, the context
    limits), not a model id the box would have to look up again on a gateway it
    may not be able to reach at open time.

    It is deliberately NOT the harness's manifest shape: the server records what
    the catalog said and the box translates it (``build_manifest_model``), so
    the pinning shape stays owned by the harness and the server never invents a
    provider id.

        Attributes:
            schema_version (str | Unset):
            metadata (Metadata | Unset):
            id (str | Unset):  Default: ''.
            display_name (str | Unset):  Default: ''.
            wire (ChatModelPinWire | Unset):  Default: ChatModelPinWire.ANTHROPIC.
            efforts (list[str] | Unset):
            effort (None | str | Unset):
            context_window (int | Unset):  Default: 0.
            max_output_tokens (int | Unset):  Default: 0.
            reasoning_format (None | str | Unset):
            reads_reasoning_formats (list[str] | Unset):
    """

    schema_version: str | Unset = UNSET
    metadata: Metadata | Unset = UNSET
    id: str | Unset = ""
    display_name: str | Unset = ""
    wire: ChatModelPinWire | Unset = ChatModelPinWire.ANTHROPIC
    efforts: list[str] | Unset = UNSET
    effort: None | str | Unset = UNSET
    context_window: int | Unset = 0
    max_output_tokens: int | Unset = 0
    reasoning_format: None | str | Unset = UNSET
    reads_reasoning_formats: list[str] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        schema_version = self.schema_version

        metadata: dict[str, Any] | Unset = UNSET
        if not isinstance(self.metadata, Unset):
            metadata = self.metadata.to_dict()

        id = self.id

        display_name = self.display_name

        wire: str | Unset = UNSET
        if not isinstance(self.wire, Unset):
            wire = self.wire.value

        efforts: list[str] | Unset = UNSET
        if not isinstance(self.efforts, Unset):
            efforts = self.efforts

        effort: None | str | Unset
        if isinstance(self.effort, Unset):
            effort = UNSET
        else:
            effort = self.effort

        context_window = self.context_window

        max_output_tokens = self.max_output_tokens

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
        field_dict.update({})
        if schema_version is not UNSET:
            field_dict["schema_version"] = schema_version
        if metadata is not UNSET:
            field_dict["metadata"] = metadata
        if id is not UNSET:
            field_dict["id"] = id
        if display_name is not UNSET:
            field_dict["display_name"] = display_name
        if wire is not UNSET:
            field_dict["wire"] = wire
        if efforts is not UNSET:
            field_dict["efforts"] = efforts
        if effort is not UNSET:
            field_dict["effort"] = effort
        if context_window is not UNSET:
            field_dict["context_window"] = context_window
        if max_output_tokens is not UNSET:
            field_dict["max_output_tokens"] = max_output_tokens
        if reasoning_format is not UNSET:
            field_dict["reasoning_format"] = reasoning_format
        if reads_reasoning_formats is not UNSET:
            field_dict["reads_reasoning_formats"] = reads_reasoning_formats

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.metadata import Metadata

        d = dict(src_dict)
        schema_version = d.pop("schema_version", UNSET)

        _metadata = d.pop("metadata", UNSET)
        metadata: Metadata | Unset
        if isinstance(_metadata, Unset):
            metadata = UNSET
        else:
            metadata = Metadata.from_dict(_metadata)

        id = d.pop("id", UNSET)

        display_name = d.pop("display_name", UNSET)

        _wire = d.pop("wire", UNSET)
        wire: ChatModelPinWire | Unset
        if isinstance(_wire, Unset):
            wire = UNSET
        else:
            wire = ChatModelPinWire(_wire)

        efforts = cast(list[str], d.pop("efforts", UNSET))

        def _parse_effort(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        effort = _parse_effort(d.pop("effort", UNSET))

        context_window = d.pop("context_window", UNSET)

        max_output_tokens = d.pop("max_output_tokens", UNSET)

        def _parse_reasoning_format(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        reasoning_format = _parse_reasoning_format(d.pop("reasoning_format", UNSET))

        reads_reasoning_formats = cast(list[str], d.pop("reads_reasoning_formats", UNSET))

        chat_model_pin = cls(
            schema_version=schema_version,
            metadata=metadata,
            id=id,
            display_name=display_name,
            wire=wire,
            efforts=efforts,
            effort=effort,
            context_window=context_window,
            max_output_tokens=max_output_tokens,
            reasoning_format=reasoning_format,
            reads_reasoning_formats=reads_reasoning_formats,
        )

        chat_model_pin.additional_properties = d
        return chat_model_pin

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
