from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

T = TypeVar("T", bound="TextEdit")


@_attrs_define
class TextEdit:
    """Replace ``old`` with ``new`` in a cell's text. ``occurrence`` (1-based)
    picks one of several matches; without it, ``old`` must match once.

        Attributes:
            old (str):
            new (str):
            occurrence (int | None | Unset):
    """

    old: str
    new: str
    occurrence: int | None | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        old = self.old

        new = self.new

        occurrence: int | None | Unset
        if isinstance(self.occurrence, Unset):
            occurrence = UNSET
        else:
            occurrence = self.occurrence

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "old": old,
                "new": new,
            }
        )
        if occurrence is not UNSET:
            field_dict["occurrence"] = occurrence

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        old = d.pop("old")

        new = d.pop("new")

        def _parse_occurrence(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        occurrence = _parse_occurrence(d.pop("occurrence", UNSET))

        text_edit = cls(
            old=old,
            new=new,
            occurrence=occurrence,
        )

        return text_edit
