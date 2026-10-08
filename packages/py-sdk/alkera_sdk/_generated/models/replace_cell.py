from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, TypeVar, cast

from attrs import define as _attrs_define

T = TypeVar("T", bound="ReplaceCell")


@_attrs_define
class ReplaceCell:
    """Set a cell's whole text; applied as a diff, so concurrent typing in the
    parts it leaves alone survives.

        Attributes:
            op (Literal['replace']):
            cell_id (str):
            source (str):
    """

    op: Literal["replace"]
    cell_id: str
    source: str

    def to_dict(self) -> dict[str, Any]:
        op = self.op

        cell_id = self.cell_id

        source = self.source

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "op": op,
                "cell_id": cell_id,
                "source": source,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        op = cast(Literal["replace"], d.pop("op"))
        if op != "replace":
            raise ValueError(f"op must match const 'replace', got '{op}'")

        cell_id = d.pop("cell_id")

        source = d.pop("source")

        replace_cell = cls(
            op=op,
            cell_id=cell_id,
            source=source,
        )

        return replace_cell
