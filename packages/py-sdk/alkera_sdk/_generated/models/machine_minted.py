from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

if TYPE_CHECKING:
    from ..models.platform_machine_read import PlatformMachineRead


T = TypeVar("T", bound="MachineMinted")


@_attrs_define
class MachineMinted:
    """The one answer that carries the raw secret. It is never stored and
    never shown again.

        Attributes:
            credential (str):
            machine (PlatformMachineRead): One platform box: its credential, and its registration when it has one.
    """

    credential: str
    machine: PlatformMachineRead
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        credential = self.credential

        machine = self.machine.to_dict()

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "credential": credential,
                "machine": machine,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.platform_machine_read import PlatformMachineRead

        d = dict(src_dict)
        credential = d.pop("credential")

        machine = PlatformMachineRead.from_dict(d.pop("machine"))

        machine_minted = cls(
            credential=credential,
            machine=machine,
        )

        machine_minted.additional_properties = d
        return machine_minted

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
