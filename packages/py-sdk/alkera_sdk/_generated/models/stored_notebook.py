from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.cell_notice import CellNotice
    from ..models.cell_state import CellState


T = TypeVar("T", bound="StoredNotebook")


@_attrs_define
class StoredNotebook:
    """A notebook file as it is stored: its cells as the file holds them, each
    with the outputs saved beside it. Read with no kernel and no live
    document, so a reader who may only see the file gets it as a notebook.

        Attributes:
            cells (list[CellState]):
            read_only_reason (None | str | Unset):
            notices (list[CellNotice] | Unset):
    """

    cells: list[CellState]
    read_only_reason: None | str | Unset = UNSET
    notices: list[CellNotice] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        cells = []
        for cells_item_data in self.cells:
            cells_item = cells_item_data.to_dict()
            cells.append(cells_item)

        read_only_reason: None | str | Unset
        if isinstance(self.read_only_reason, Unset):
            read_only_reason = UNSET
        else:
            read_only_reason = self.read_only_reason

        notices: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.notices, Unset):
            notices = []
            for notices_item_data in self.notices:
                notices_item = notices_item_data.to_dict()
                notices.append(notices_item)

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "cells": cells,
            }
        )
        if read_only_reason is not UNSET:
            field_dict["read_only_reason"] = read_only_reason
        if notices is not UNSET:
            field_dict["notices"] = notices

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.cell_notice import CellNotice
        from ..models.cell_state import CellState

        d = dict(src_dict)
        cells = []
        _cells = d.pop("cells")
        for cells_item_data in _cells:
            cells_item = CellState.from_dict(cells_item_data)

            cells.append(cells_item)

        def _parse_read_only_reason(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        read_only_reason = _parse_read_only_reason(d.pop("read_only_reason", UNSET))

        _notices = d.pop("notices", UNSET)
        notices: list[CellNotice] | Unset = UNSET
        if _notices is not UNSET:
            notices = []
            for notices_item_data in _notices:
                notices_item = CellNotice.from_dict(notices_item_data)

                notices.append(notices_item)

        stored_notebook = cls(
            cells=cells,
            read_only_reason=read_only_reason,
            notices=notices,
        )

        stored_notebook.additional_properties = d
        return stored_notebook

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
