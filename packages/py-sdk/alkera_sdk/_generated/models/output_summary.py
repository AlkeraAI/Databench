from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.error_info import ErrorInfo


T = TypeVar("T", bound="OutputSummary")


@_attrs_define
class OutputSummary:
    """
    Attributes:
        kinds (list[str]):
        text (str):
        error (ErrorInfo | None | Unset):
        truncated (bool | Unset):  Default: False.
        has_image (bool | Unset):  Default: False.
        has_chart (bool | Unset):  Default: False.
        has_table (bool | Unset):  Default: False.
        has_widget (bool | Unset):  Default: False.
    """

    kinds: list[str]
    text: str
    error: ErrorInfo | None | Unset = UNSET
    truncated: bool | Unset = False
    has_image: bool | Unset = False
    has_chart: bool | Unset = False
    has_table: bool | Unset = False
    has_widget: bool | Unset = False
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.error_info import ErrorInfo

        kinds = self.kinds

        text = self.text

        error: dict[str, Any] | None | Unset
        if isinstance(self.error, Unset):
            error = UNSET
        elif isinstance(self.error, ErrorInfo):
            error = self.error.to_dict()
        else:
            error = self.error

        truncated = self.truncated

        has_image = self.has_image

        has_chart = self.has_chart

        has_table = self.has_table

        has_widget = self.has_widget

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "kinds": kinds,
                "text": text,
            }
        )
        if error is not UNSET:
            field_dict["error"] = error
        if truncated is not UNSET:
            field_dict["truncated"] = truncated
        if has_image is not UNSET:
            field_dict["has_image"] = has_image
        if has_chart is not UNSET:
            field_dict["has_chart"] = has_chart
        if has_table is not UNSET:
            field_dict["has_table"] = has_table
        if has_widget is not UNSET:
            field_dict["has_widget"] = has_widget

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.error_info import ErrorInfo

        d = dict(src_dict)
        kinds = cast(list[str], d.pop("kinds"))

        text = d.pop("text")

        def _parse_error(data: object) -> ErrorInfo | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                error_type_0 = ErrorInfo.from_dict(data)

                return error_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(ErrorInfo | None | Unset, data)

        error = _parse_error(d.pop("error", UNSET))

        truncated = d.pop("truncated", UNSET)

        has_image = d.pop("has_image", UNSET)

        has_chart = d.pop("has_chart", UNSET)

        has_table = d.pop("has_table", UNSET)

        has_widget = d.pop("has_widget", UNSET)

        output_summary = cls(
            kinds=kinds,
            text=text,
            error=error,
            truncated=truncated,
            has_image=has_image,
            has_chart=has_chart,
            has_table=has_table,
            has_widget=has_widget,
        )

        output_summary.additional_properties = d
        return output_summary

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
