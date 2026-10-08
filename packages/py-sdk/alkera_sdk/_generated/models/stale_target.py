from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, TypeVar, cast

from attrs import define as _attrs_define

T = TypeVar("T", bound="StaleTarget")


@_attrs_define
class StaleTarget:
    """
    Attributes:
        kind (Literal['stale']):
    """

    kind: Literal["stale"]

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
        kind = cast(Literal["stale"], d.pop("kind"))
        if kind != "stale":
            raise ValueError(f"kind must match const 'stale', got '{kind}'")

        stale_target = cls(
            kind=kind,
        )

        return stale_target
