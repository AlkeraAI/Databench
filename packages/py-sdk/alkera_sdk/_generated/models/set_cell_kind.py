from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, TypeVar, cast

from attrs import define as _attrs_define

T = TypeVar("T", bound="SetCellKind")


@_attrs_define
class SetCellKind:
    """
    Attributes:
        op (Literal['set_kind']):
        cell_id (str):
        kind (str):
    """

    op: Literal["set_kind"]
    cell_id: str
    kind: str

    def to_dict(self) -> dict[str, Any]:
        op = self.op

        cell_id = self.cell_id

        kind = self.kind

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "op": op,
                "cell_id": cell_id,
                "kind": kind,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        op = cast(Literal["set_kind"], d.pop("op"))
        if op != "set_kind":
            raise ValueError(f"op must match const 'set_kind', got '{op}'")

        cell_id = d.pop("cell_id")

        kind = d.pop("kind")

        set_cell_kind = cls(
            op=op,
            cell_id=cell_id,
            kind=kind,
        )

        return set_cell_kind
