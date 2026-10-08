from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, TypeVar, cast

from attrs import define as _attrs_define

T = TypeVar("T", bound="BelowTarget")


@_attrs_define
class BelowTarget:
    """
    Attributes:
        kind (Literal['below']):
        id (str):
    """

    kind: Literal["below"]
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
        kind = cast(Literal["below"], d.pop("kind"))
        if kind != "below":
            raise ValueError(f"kind must match const 'below', got '{kind}'")

        id = d.pop("id")

        below_target = cls(
            kind=kind,
            id=id,
        )

        return below_target
