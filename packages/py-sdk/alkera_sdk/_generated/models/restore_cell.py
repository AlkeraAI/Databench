from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

T = TypeVar("T", bound="RestoreCell")


@_attrs_define
class RestoreCell:
    """
    Attributes:
        op (Literal['restore']):
        cell_id (str):
        after (None | str | Unset):
    """

    op: Literal["restore"]
    cell_id: str
    after: None | str | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        op = self.op

        cell_id = self.cell_id

        after: None | str | Unset
        if isinstance(self.after, Unset):
            after = UNSET
        else:
            after = self.after

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "op": op,
                "cell_id": cell_id,
            }
        )
        if after is not UNSET:
            field_dict["after"] = after

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        op = cast(Literal["restore"], d.pop("op"))
        if op != "restore":
            raise ValueError(f"op must match const 'restore', got '{op}'")

        cell_id = d.pop("cell_id")

        def _parse_after(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        after = _parse_after(d.pop("after", UNSET))

        restore_cell = cls(
            op=op,
            cell_id=cell_id,
            after=after,
        )

        return restore_cell
