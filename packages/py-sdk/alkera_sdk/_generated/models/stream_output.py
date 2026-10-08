from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.stream_output_name import StreamOutputName
from ..types import UNSET, Unset

T = TypeVar("T", bound="StreamOutput")


@_attrs_define
class StreamOutput:
    """
    Attributes:
        output_id (str):
        name (StreamOutputName):
        text (str):
        type_ (Literal['stream'] | Unset):  Default: 'stream'.
    """

    output_id: str
    name: StreamOutputName
    text: str
    type_: Literal["stream"] | Unset = "stream"
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        output_id = self.output_id

        name = self.name.value

        text = self.text

        type_ = self.type_

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "output_id": output_id,
                "name": name,
                "text": text,
            }
        )
        if type_ is not UNSET:
            field_dict["type"] = type_

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        output_id = d.pop("output_id")

        name = StreamOutputName(d.pop("name"))

        text = d.pop("text")

        type_ = cast(Literal["stream"] | Unset, d.pop("type", UNSET))
        if type_ != "stream" and not isinstance(type_, Unset):
            raise ValueError(f"type must match const 'stream', got '{type_}'")

        stream_output = cls(
            output_id=output_id,
            name=name,
            text=text,
            type_=type_,
        )

        stream_output.additional_properties = d
        return stream_output

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
