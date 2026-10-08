from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.complete_upload_request_conflictbehavior import CompleteUploadRequestConflictbehavior
from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.complete_part import CompletePart


T = TypeVar("T", bound="CompleteUploadRequest")


@_attrs_define
class CompleteUploadRequest:
    """
    Attributes:
        parts (list[CompletePart]):
        conflict_behavior (CompleteUploadRequestConflictbehavior | Unset):  Default:
            CompleteUploadRequestConflictbehavior.FAIL.
    """

    parts: list[CompletePart]
    conflict_behavior: CompleteUploadRequestConflictbehavior | Unset = (
        CompleteUploadRequestConflictbehavior.FAIL
    )
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        parts = []
        for parts_item_data in self.parts:
            parts_item = parts_item_data.to_dict()
            parts.append(parts_item)

        conflict_behavior: str | Unset = UNSET
        if not isinstance(self.conflict_behavior, Unset):
            conflict_behavior = self.conflict_behavior.value

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "parts": parts,
            }
        )
        if conflict_behavior is not UNSET:
            field_dict["conflictBehavior"] = conflict_behavior

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.complete_part import CompletePart

        d = dict(src_dict)
        parts = []
        _parts = d.pop("parts")
        for parts_item_data in _parts:
            parts_item = CompletePart.from_dict(parts_item_data)

            parts.append(parts_item)

        _conflict_behavior = d.pop("conflictBehavior", UNSET)
        conflict_behavior: CompleteUploadRequestConflictbehavior | Unset
        if isinstance(_conflict_behavior, Unset):
            conflict_behavior = UNSET
        else:
            conflict_behavior = CompleteUploadRequestConflictbehavior(_conflict_behavior)

        complete_upload_request = cls(
            parts=parts,
            conflict_behavior=conflict_behavior,
        )

        complete_upload_request.additional_properties = d
        return complete_upload_request

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
