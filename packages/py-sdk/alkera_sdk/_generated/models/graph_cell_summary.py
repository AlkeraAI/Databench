from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.graph_error_info import GraphErrorInfo


T = TypeVar("T", bound="GraphCellSummary")


@_attrs_define
class GraphCellSummary:
    """
    Attributes:
        defs (list[str] | Unset):
        refs (list[str] | Unset):
        errors (list[GraphErrorInfo] | Unset):
    """

    defs: list[str] | Unset = UNSET
    refs: list[str] | Unset = UNSET
    errors: list[GraphErrorInfo] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        defs: list[str] | Unset = UNSET
        if not isinstance(self.defs, Unset):
            defs = self.defs

        refs: list[str] | Unset = UNSET
        if not isinstance(self.refs, Unset):
            refs = self.refs

        errors: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.errors, Unset):
            errors = []
            for errors_item_data in self.errors:
                errors_item = errors_item_data.to_dict()
                errors.append(errors_item)

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if defs is not UNSET:
            field_dict["defs"] = defs
        if refs is not UNSET:
            field_dict["refs"] = refs
        if errors is not UNSET:
            field_dict["errors"] = errors

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.graph_error_info import GraphErrorInfo

        d = dict(src_dict)
        defs = cast(list[str], d.pop("defs", UNSET))

        refs = cast(list[str], d.pop("refs", UNSET))

        _errors = d.pop("errors", UNSET)
        errors: list[GraphErrorInfo] | Unset = UNSET
        if _errors is not UNSET:
            errors = []
            for errors_item_data in _errors:
                errors_item = GraphErrorInfo.from_dict(errors_item_data)

                errors.append(errors_item)

        graph_cell_summary = cls(
            defs=defs,
            refs=refs,
            errors=errors,
        )

        graph_cell_summary.additional_properties = d
        return graph_cell_summary

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
