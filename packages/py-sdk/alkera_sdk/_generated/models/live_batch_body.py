from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

if TYPE_CHECKING:
    from ..models.live_report_body import LiveReportBody


T = TypeVar("T", bound="LiveBatchBody")


@_attrs_define
class LiveBatchBody:
    """One live batch, at most ``files_live_max_batch_entries`` entries long.

    The same number the grant hands the holder as ``maxBatchEntries``, read
    here rather than compiled in so a deployment that lowers it lowers what it
    will accept in the same breath. A batch is one statement per entry inside
    one transaction holding the lease row, so an unbounded list is an unbounded
    transaction — and the holder is told it sent too many rather than having
    them applied.

        Attributes:
            entries (list[LiveReportBody]):
    """

    entries: list[LiveReportBody]
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        entries = []
        for entries_item_data in self.entries:
            entries_item = entries_item_data.to_dict()
            entries.append(entries_item)

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "entries": entries,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.live_report_body import LiveReportBody

        d = dict(src_dict)
        entries = []
        _entries = d.pop("entries")
        for entries_item_data in _entries:
            entries_item = LiveReportBody.from_dict(entries_item_data)

            entries.append(entries_item)

        live_batch_body = cls(
            entries=entries,
        )

        live_batch_body.additional_properties = d
        return live_batch_body

    @property
    def additional_keys(self) -> list[str]:
        return list(self.additional_properties.keys())

    def __getitem__(self, key: str) -> Any:
        return self.additional_properties[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self.additional_properties[key] = value

    def __delitem__(self, key: str) -> None:
        del self.additional_properties[key]

    def __contains__(self, key: str) -> bool:
        return key in self.additional_properties
