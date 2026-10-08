from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, TypeVar, cast

from attrs import define as _attrs_define

T = TypeVar("T", bound="DeleteCell")


@_attrs_define
class DeleteCell:
    """
    Attributes:
        op (Literal['delete']):
        cell_id (str):
    """

    op: Literal["delete"]
    cell_id: str

    def to_dict(self) -> dict[str, Any]:
        op = self.op

        cell_id = self.cell_id

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "op": op,
                "cell_id": cell_id,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        op = cast(Literal["delete"], d.pop("op"))
        if op != "delete":
            raise ValueError(f"op must match const 'delete', got '{op}'")

        cell_id = d.pop("cell_id")

        delete_cell = cls(
            op=op,
            cell_id=cell_id,
        )

        return delete_cell
