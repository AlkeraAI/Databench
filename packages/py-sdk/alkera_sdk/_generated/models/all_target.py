from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, TypeVar, cast

from attrs import define as _attrs_define

T = TypeVar("T", bound="AllTarget")


@_attrs_define
class AllTarget:
    """
    Attributes:
        kind (Literal['all']):
    """

    kind: Literal["all"]

    def to_dict(self) -> dict[str, Any]:
        kind = self.kind

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "kind": kind,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        kind = cast(Literal["all"], d.pop("kind"))
        if kind != "all":
            raise ValueError(f"kind must match const 'all', got '{kind}'")

        all_target = cls(
            kind=kind,
        )

        return all_target
