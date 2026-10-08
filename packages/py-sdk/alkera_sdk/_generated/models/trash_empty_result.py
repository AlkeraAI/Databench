from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="TrashEmptyResult")


@_attrs_define
class TrashEmptyResult:
    """What one sweep of the trash did, and what it left behind.

    ``skipped`` exists because emptying is decided per deletion: a root the
    caller may not delete stays, and an answer naming only what it removed reads
    as "the trash is empty" when it is not.

        Attributes:
            removed (int | Unset):  Default: 0.
            skipped (int | Unset):  Default: 0.
            skipped_reasons (list[str] | Unset):
    """

    removed: int | Unset = 0
    skipped: int | Unset = 0
    skipped_reasons: list[str] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        removed = self.removed

        skipped = self.skipped

        skipped_reasons: list[str] | Unset = UNSET
        if not isinstance(self.skipped_reasons, Unset):
            skipped_reasons = self.skipped_reasons

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if removed is not UNSET:
            field_dict["removed"] = removed
        if skipped is not UNSET:
            field_dict["skipped"] = skipped
        if skipped_reasons is not UNSET:
            field_dict["skippedReasons"] = skipped_reasons

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        removed = d.pop("removed", UNSET)

        skipped = d.pop("skipped", UNSET)

        skipped_reasons = cast(list[str], d.pop("skippedReasons", UNSET))

        trash_empty_result = cls(
            removed=removed,
            skipped=skipped,
            skipped_reasons=skipped_reasons,
        )

        trash_empty_result.additional_properties = d
        return trash_empty_result

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
