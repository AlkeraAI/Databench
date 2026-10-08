from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="ErrorInfo")


@_attrs_define
class ErrorInfo:
    """
    Attributes:
        ename (str):
        evalue (str):
        traceback (list[str] | Unset):
    """

    ename: str
    evalue: str
    traceback: list[str] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        ename = self.ename

        evalue = self.evalue

        traceback: list[str] | Unset = UNSET
        if not isinstance(self.traceback, Unset):
            traceback = self.traceback

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "ename": ename,
                "evalue": evalue,
            }
        )
        if traceback is not UNSET:
            field_dict["traceback"] = traceback

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        ename = d.pop("ename")

        evalue = d.pop("evalue")

        traceback = cast(list[str], d.pop("traceback", UNSET))

        error_info = cls(
            ename=ename,
            evalue=evalue,
            traceback=traceback,
        )

        error_info.additional_properties = d
        return error_info

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
