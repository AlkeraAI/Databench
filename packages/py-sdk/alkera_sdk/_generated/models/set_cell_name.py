from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, TypeVar, cast

from attrs import define as _attrs_define

T = TypeVar("T", bound="SetCellName")


@_attrs_define
class SetCellName:
    """
    Attributes:
        op (Literal['rename']):
        cell_id (str):
        name (str):
    """

    op: Literal["rename"]
    cell_id: str
    name: str

    def to_dict(self) -> dict[str, Any]:
        op = self.op

        cell_id = self.cell_id

        name = self.name

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "op": op,
                "cell_id": cell_id,
                "name": name,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        op = cast(Literal["rename"], d.pop("op"))
        if op != "rename":
            raise ValueError(f"op must match const 'rename', got '{op}'")

        cell_id = d.pop("cell_id")

        name = d.pop("name")

        set_cell_name = cls(
            op=op,
            cell_id=cell_id,
            name=name,
        )

        return set_cell_name
