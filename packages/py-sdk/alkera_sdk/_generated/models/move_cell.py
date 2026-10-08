from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

T = TypeVar("T", bound="MoveCell")


@_attrs_define
class MoveCell:
    """
    Attributes:
        op (Literal['move']):
        cell_id (str):
        after (None | str | Unset):
        before (None | str | Unset):
    """

    op: Literal["move"]
    cell_id: str
    after: None | str | Unset = UNSET
    before: None | str | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        op = self.op

        cell_id = self.cell_id

        after: None | str | Unset
        if isinstance(self.after, Unset):
            after = UNSET
        else:
            after = self.after

        before: None | str | Unset
        if isinstance(self.before, Unset):
            before = UNSET
        else:
            before = self.before

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "op": op,
                "cell_id": cell_id,
            }
        )
        if after is not UNSET:
            field_dict["after"] = after
        if before is not UNSET:
            field_dict["before"] = before

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        op = cast(Literal["move"], d.pop("op"))
        if op != "move":
            raise ValueError(f"op must match const 'move', got '{op}'")

        cell_id = d.pop("cell_id")

        def _parse_after(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        after = _parse_after(d.pop("after", UNSET))

        def _parse_before(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        before = _parse_before(d.pop("before", UNSET))

        move_cell = cls(
            op=op,
            cell_id=cell_id,
            after=after,
            before=before,
        )

        return move_cell
