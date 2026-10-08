from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

if TYPE_CHECKING:
    from ..models.cell_after_op import CellAfterOp
    from ..models.cell_notice import CellNotice
    from ..models.graph_summary import GraphSummary


T = TypeVar("T", bound="NotebookOpsResult")


@_attrs_define
class NotebookOpsResult:
    """
    Attributes:
        token (str):
        repeat (bool):
        cells (list[CellAfterOp]):
        created (list[str]):
        notices (list[CellNotice]):
        graph (GraphSummary): The document graph (current text): for diagnostics only.

            ``computed`` is false when the writer had no analysis of exactly the state
            it answers for (the platform analyses off the hot path, and the ``graph``
            event on the notebook channel carries the analysis once it is ready);
            ``cells`` and ``edges`` are empty then.
    """

    token: str
    repeat: bool
    cells: list[CellAfterOp]
    created: list[str]
    notices: list[CellNotice]
    graph: GraphSummary
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        token = self.token

        repeat = self.repeat

        cells = []
        for cells_item_data in self.cells:
            cells_item = cells_item_data.to_dict()
            cells.append(cells_item)

        created = self.created

        notices = []
        for notices_item_data in self.notices:
            notices_item = notices_item_data.to_dict()
            notices.append(notices_item)

        graph = self.graph.to_dict()

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "token": token,
                "repeat": repeat,
                "cells": cells,
                "created": created,
                "notices": notices,
                "graph": graph,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.cell_after_op import CellAfterOp
        from ..models.cell_notice import CellNotice
        from ..models.graph_summary import GraphSummary

        d = dict(src_dict)
        token = d.pop("token")

        repeat = d.pop("repeat")

        cells = []
        _cells = d.pop("cells")
        for cells_item_data in _cells:
            cells_item = CellAfterOp.from_dict(cells_item_data)

            cells.append(cells_item)

        created = cast(list[str], d.pop("created"))

        notices = []
        _notices = d.pop("notices")
        for notices_item_data in _notices:
            notices_item = CellNotice.from_dict(notices_item_data)

            notices.append(notices_item)

        graph = GraphSummary.from_dict(d.pop("graph"))

        notebook_ops_result = cls(
            token=token,
            repeat=repeat,
            cells=cells,
            created=created,
            notices=notices,
            graph=graph,
        )

        notebook_ops_result.additional_properties = d
        return notebook_ops_result

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
