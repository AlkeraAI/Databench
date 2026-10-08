from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Literal, TypeVar, cast

from attrs import define as _attrs_define

if TYPE_CHECKING:
    from ..models.text_edit import TextEdit


T = TypeVar("T", bound="EditCell")


@_attrs_define
class EditCell:
    """
    Attributes:
        op (Literal['edit']):
        cell_id (str):
        edits (list[TextEdit]):
    """

    op: Literal["edit"]
    cell_id: str
    edits: list[TextEdit]

    def to_dict(self) -> dict[str, Any]:
        op = self.op

        cell_id = self.cell_id

        edits = []
        for edits_item_data in self.edits:
            edits_item = edits_item_data.to_dict()
            edits.append(edits_item)

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "op": op,
                "cell_id": cell_id,
                "edits": edits,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.text_edit import TextEdit

        d = dict(src_dict)
        op = cast(Literal["edit"], d.pop("op"))
        if op != "edit":
            raise ValueError(f"op must match const 'edit', got '{op}'")

        cell_id = d.pop("cell_id")

        edits = []
        _edits = d.pop("edits")
        for edits_item_data in _edits:
            edits_item = TextEdit.from_dict(edits_item_data)

            edits.append(edits_item)

        edit_cell = cls(
            op=op,
            cell_id=cell_id,
            edits=edits,
        )

        return edit_cell
