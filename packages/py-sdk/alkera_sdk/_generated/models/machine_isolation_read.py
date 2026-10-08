from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.machine_isolation_read_profile import MachineIsolationReadProfile
from ..types import UNSET, Unset

T = TypeVar("T", bound="MachineIsolationRead")


@_attrs_define
class MachineIsolationRead:
    """How far apart the box can keep the orgs it serves, as its probe found
    (``alkera_core.compute.box_isolation``).

        Attributes:
            profile (MachineIsolationReadProfile | Unset):  Default: MachineIsolationReadProfile.SINGLE_ORG.
            mechanisms (list[str] | Unset):
    """

    profile: MachineIsolationReadProfile | Unset = MachineIsolationReadProfile.SINGLE_ORG
    mechanisms: list[str] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        profile: str | Unset = UNSET
        if not isinstance(self.profile, Unset):
            profile = self.profile.value

        mechanisms: list[str] | Unset = UNSET
        if not isinstance(self.mechanisms, Unset):
            mechanisms = self.mechanisms

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if profile is not UNSET:
            field_dict["profile"] = profile
        if mechanisms is not UNSET:
            field_dict["mechanisms"] = mechanisms

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        _profile = d.pop("profile", UNSET)
        profile: MachineIsolationReadProfile | Unset
        if isinstance(_profile, Unset):
            profile = UNSET
        else:
            profile = MachineIsolationReadProfile(_profile)

        mechanisms = cast(list[str], d.pop("mechanisms", UNSET))

        machine_isolation_read = cls(
            profile=profile,
            mechanisms=mechanisms,
        )

        machine_isolation_read.additional_properties = d
        return machine_isolation_read

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
