from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar
from uuid import UUID

from attrs import define as _attrs_define

if TYPE_CHECKING:
    from ..models.tree_entry import TreeEntry


T = TypeVar("T", bound="TreeBatch")


@_attrs_define
class TreeBatch:
    """One flush of the holder's metadata queue.

    Attributes:
        batch_id (UUID):
        entries (list[TreeEntry]):
    """

    batch_id: UUID
    entries: list[TreeEntry]

    def to_dict(self) -> dict[str, Any]:
        batch_id = str(self.batch_id)

        entries = []
        for entries_item_data in self.entries:
            entries_item = entries_item_data.to_dict()
            entries.append(entries_item)

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "batch_id": batch_id,
                "entries": entries,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.tree_entry import TreeEntry

        d = dict(src_dict)
        batch_id = UUID(d.pop("batch_id"))

        entries = []
        _entries = d.pop("entries")
        for entries_item_data in _entries:
            entries_item = TreeEntry.from_dict(entries_item_data)

            entries.append(entries_item)

        tree_batch = cls(
            batch_id=batch_id,
            entries=entries,
        )

        return tree_batch
