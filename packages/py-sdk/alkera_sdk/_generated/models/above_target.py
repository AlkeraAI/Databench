from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, TypeVar, cast

from attrs import define as _attrs_define

T = TypeVar("T", bound="AboveTarget")


@_attrs_define
class AboveTarget:
    """
    Attributes:
        kind (Literal['above']):
        id (str):
    """

    kind: Literal["above"]
    id: str

    def to_dict(self) -> dict[str, Any]:
        kind = self.kind

        id = self.id

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "kind": kind,
                "id": id,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        kind = cast(Literal["above"], d.pop("kind"))
        if kind != "above":
            raise ValueError(f"kind must match const 'above', got '{kind}'")

        id = d.pop("id")

        above_target = cls(
            kind=kind,
            id=id,
        )

        return above_target
