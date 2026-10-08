from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Literal, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.output_bundle import OutputBundle


T = TypeVar("T", bound="DisplayOutput")


@_attrs_define
class DisplayOutput:
    """A rich output: one MIME bundle.

    Attributes:
        output_id (str):
        data (OutputBundle):
        type_ (Literal['display'] | Unset):  Default: 'display'.
    """

    output_id: str
    data: OutputBundle
    type_: Literal["display"] | Unset = "display"
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        output_id = self.output_id

        data = self.data.to_dict()

        type_ = self.type_

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "output_id": output_id,
                "data": data,
            }
        )
        if type_ is not UNSET:
            field_dict["type"] = type_

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.output_bundle import OutputBundle

        d = dict(src_dict)
        output_id = d.pop("output_id")

        data = OutputBundle.from_dict(d.pop("data"))

        type_ = cast(Literal["display"] | Unset, d.pop("type", UNSET))
        if type_ != "display" and not isinstance(type_, Unset):
            raise ValueError(f"type must match const 'display', got '{type_}'")

        display_output = cls(
            output_id=output_id,
            data=data,
            type_=type_,
        )

        display_output.additional_properties = d
        return display_output

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
