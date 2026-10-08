from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="GraphErrorInfo")


@_attrs_define
class GraphErrorInfo:
    """A problem in the dependency graph: its code (``multiple_definitions``,
    ``cycle``, ``syntax``, ``delete_nonlocal``, ...), the name it is about,
    and the cells it involves. The one graph error model: ``GraphSummary``,
    ``GraphView``, the cells of a view and the ``graph`` event all carry it.

        Attributes:
            code (str):
            name (None | str | Unset):
            cells (list[str] | Unset):
    """

    code: str
    name: None | str | Unset = UNSET
    cells: list[str] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        code = self.code

        name: None | str | Unset
        if isinstance(self.name, Unset):
            name = UNSET
        else:
            name = self.name

        cells: list[str] | Unset = UNSET
        if not isinstance(self.cells, Unset):
            cells = self.cells

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "code": code,
            }
        )
        if name is not UNSET:
            field_dict["name"] = name
        if cells is not UNSET:
            field_dict["cells"] = cells

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        code = d.pop("code")

        def _parse_name(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        name = _parse_name(d.pop("name", UNSET))

        cells = cast(list[str], d.pop("cells", UNSET))

        graph_error_info = cls(
            code=code,
            name=name,
            cells=cells,
        )

        graph_error_info.additional_properties = d
        return graph_error_info

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
