from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="MachineIsolationReport")


@_attrs_define
class MachineIsolationReport:
    """How far apart the box can keep its orgs, as its probe found. The
    profile placement reads is the ``org_isolation`` capability; this is the
    detail beside it. Names this side does not know are kept out on read.

        Attributes:
            profile (str):
            mechanisms (list[str] | Unset):
    """

    profile: str
    mechanisms: list[str] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        profile = self.profile

        mechanisms: list[str] | Unset = UNSET
        if not isinstance(self.mechanisms, Unset):
            mechanisms = self.mechanisms

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "profile": profile,
            }
        )
        if mechanisms is not UNSET:
            field_dict["mechanisms"] = mechanisms

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        profile = d.pop("profile")

        mechanisms = cast(list[str], d.pop("mechanisms", UNSET))

        machine_isolation_report = cls(
            profile=profile,
            mechanisms=mechanisms,
        )

        machine_isolation_report.additional_properties = d
        return machine_isolation_report

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
