from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, TypeVar, cast

from attrs import define as _attrs_define

T = TypeVar("T", bound="CellsTarget")


@_attrs_define
class CellsTarget:
    """
    Attributes:
        kind (Literal['cells']):
        ids (list[str]):
    """

    kind: Literal["cells"]
    ids: list[str]

    def to_dict(self) -> dict[str, Any]:
        kind = self.kind

        ids = self.ids

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "kind": kind,
                "ids": ids,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        kind = cast(Literal["cells"], d.pop("kind"))
        if kind != "cells":
            raise ValueError(f"kind must match const 'cells', got '{kind}'")

        ids = cast(list[str], d.pop("ids"))

        cells_target = cls(
            kind=kind,
            ids=ids,
        )

        return cells_target
