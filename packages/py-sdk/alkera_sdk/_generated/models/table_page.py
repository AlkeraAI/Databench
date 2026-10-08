from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

if TYPE_CHECKING:
    from ..models.table_column import TableColumn


T = TypeVar("T", bound="TablePage")


@_attrs_define
class TablePage:
    """``GET .../cells/{cell_id}/table``: one page of a table output.

    The table page the kernel makes (``_alkera_kernel.tables``), unchanged:
    the same ``schema``, ``rows``, ``total_rows`` and ``offset`` a cell's
    table output carries for its first page, so a reader takes every page
    the same way. ``rows`` is one list per row in column order, each cell
    already plain JSON (a date as ISO text, a decimal as its text).

        Attributes:
            schema (list[TableColumn]):
            rows (list[list[Any]]):
            total_rows (int):
            offset (int):
            limit (int):
    """

    schema: list[TableColumn]
    rows: list[list[Any]]
    total_rows: int
    offset: int
    limit: int
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        schema = []
        for schema_item_data in self.schema:
            schema_item = schema_item_data.to_dict()
            schema.append(schema_item)

        rows = []
        for rows_item_data in self.rows:
            rows_item = rows_item_data

            rows.append(rows_item)

        total_rows = self.total_rows

        offset = self.offset

        limit = self.limit

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "schema": schema,
                "rows": rows,
                "total_rows": total_rows,
                "offset": offset,
                "limit": limit,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.table_column import TableColumn

        d = dict(src_dict)
        schema = []
        _schema = d.pop("schema")
        for schema_item_data in _schema:
            schema_item = TableColumn.from_dict(schema_item_data)

            schema.append(schema_item)

        rows = []
        _rows = d.pop("rows")
        for rows_item_data in _rows:
            rows_item = cast(list[Any], rows_item_data)

            rows.append(rows_item)

        total_rows = d.pop("total_rows")

        offset = d.pop("offset")

        limit = d.pop("limit")

        table_page = cls(
            schema=schema,
            rows=rows,
            total_rows=total_rows,
            offset=offset,
            limit=limit,
        )

        table_page.additional_properties = d
        return table_page

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
