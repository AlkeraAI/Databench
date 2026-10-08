from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.cells import Cells


T = TypeVar("T", bound="GraphSummary")


@_attrs_define
class GraphSummary:
    """The document graph (current text): for diagnostics only.

    ``computed`` is false when the writer had no analysis of exactly the state
    it answers for (the platform analyses off the hot path, and the ``graph``
    event on the notebook channel carries the analysis once it is ready);
    ``cells`` and ``edges`` are empty then.

        Attributes:
            computed (bool | Unset):  Default: True.
            cells (Cells | Unset):
            edges (list[list[str]] | Unset):
    """

    computed: bool | Unset = True
    cells: Cells | Unset = UNSET
    edges: list[list[str]] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        computed = self.computed

        cells: dict[str, Any] | Unset = UNSET
        if not isinstance(self.cells, Unset):
            cells = self.cells.to_dict()

        edges: list[list[str]] | Unset = UNSET
        if not isinstance(self.edges, Unset):
            edges = []
            for edges_item_data in self.edges:
                edges_item = []
                for edges_item_item_data in edges_item_data:
                    edges_item_item: str
                    edges_item_item = edges_item_item_data
                    edges_item.append(edges_item_item)

                edges.append(edges_item)

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if computed is not UNSET:
            field_dict["computed"] = computed
        if cells is not UNSET:
            field_dict["cells"] = cells
        if edges is not UNSET:
            field_dict["edges"] = edges

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.cells import Cells

        d = dict(src_dict)
        computed = d.pop("computed", UNSET)

        _cells = d.pop("cells", UNSET)
        cells: Cells | Unset
        if isinstance(_cells, Unset):
            cells = UNSET
        else:
            cells = Cells.from_dict(_cells)

        _edges = d.pop("edges", UNSET)
        edges: list[list[str]] | Unset = UNSET
        if _edges is not UNSET:
            edges = []
            for edges_item_data in _edges:
                edges_item = []
                _edges_item = edges_item_data
                for edges_item_item_data in _edges_item:

                    def _parse_edges_item_item(data: object) -> str:
                        return cast(str, data)

                    edges_item_item = _parse_edges_item_item(edges_item_item_data)

                    edges_item.append(edges_item_item)

                edges.append(edges_item)

        graph_summary = cls(
            computed=computed,
            cells=cells,
            edges=edges,
        )

        graph_summary.additional_properties = d
        return graph_summary

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
