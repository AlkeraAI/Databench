from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Literal, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.error_info import ErrorInfo


T = TypeVar("T", bound="ErrorOutput")


@_attrs_define
class ErrorOutput:
    """
    Attributes:
        output_id (str):
        error (ErrorInfo):
        type_ (Literal['error'] | Unset):  Default: 'error'.
    """

    output_id: str
    error: ErrorInfo
    type_: Literal["error"] | Unset = "error"
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        output_id = self.output_id

        error = self.error.to_dict()

        type_ = self.type_

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "output_id": output_id,
                "error": error,
            }
        )
        if type_ is not UNSET:
            field_dict["type"] = type_

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.error_info import ErrorInfo

        d = dict(src_dict)
        output_id = d.pop("output_id")

        error = ErrorInfo.from_dict(d.pop("error"))

        type_ = cast(Literal["error"] | Unset, d.pop("type", UNSET))
        if type_ != "error" and not isinstance(type_, Unset):
            raise ValueError(f"type must match const 'error', got '{type_}'")

        error_output = cls(
            output_id=output_id,
            error=error,
            type_=type_,
        )

        error_output.additional_properties = d
        return error_output

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
