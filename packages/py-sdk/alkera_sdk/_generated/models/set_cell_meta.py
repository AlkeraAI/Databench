from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Literal, TypeVar, cast

from attrs import define as _attrs_define

if TYPE_CHECKING:
    from ..models.cell_meta_changes import CellMetaChanges


T = TypeVar("T", bound="SetCellMeta")


@_attrs_define
class SetCellMeta:
    """Change a SQL or Markdown cell's settings; a key set to ``None`` goes
    back to its default (a SQL cell with no ``connection`` runs in DuckDB).

        Attributes:
            op (Literal['set_meta']):
            cell_id (str):
            meta (CellMetaChanges):
    """

    op: Literal["set_meta"]
    cell_id: str
    meta: CellMetaChanges

    def to_dict(self) -> dict[str, Any]:
        op = self.op

        cell_id = self.cell_id

        meta = self.meta.to_dict()

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "op": op,
                "cell_id": cell_id,
                "meta": meta,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.cell_meta_changes import CellMetaChanges

        d = dict(src_dict)
        op = cast(Literal["set_meta"], d.pop("op"))
        if op != "set_meta":
            raise ValueError(f"op must match const 'set_meta', got '{op}'")

        cell_id = d.pop("cell_id")

        meta = CellMetaChanges.from_dict(d.pop("meta"))

        set_cell_meta = cls(
            op=op,
            cell_id=cell_id,
            meta=meta,
        )

        return set_cell_meta
