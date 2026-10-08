from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.provider_note import ProviderNote
    from ..models.unmanaged_machine import UnmanagedMachine


T = TypeVar("T", bound="UnmanagedMachineList")


@_attrs_define
class UnmanagedMachineList:
    """
    Attributes:
        items (list[UnmanagedMachine]):
        unavailable (list[ProviderNote] | Unset):
    """

    items: list[UnmanagedMachine]
    unavailable: list[ProviderNote] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        items = []
        for items_item_data in self.items:
            items_item = items_item_data.to_dict()
            items.append(items_item)

        unavailable: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.unavailable, Unset):
            unavailable = []
            for unavailable_item_data in self.unavailable:
                unavailable_item = unavailable_item_data.to_dict()
                unavailable.append(unavailable_item)

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "items": items,
            }
        )
        if unavailable is not UNSET:
            field_dict["unavailable"] = unavailable

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.provider_note import ProviderNote
        from ..models.unmanaged_machine import UnmanagedMachine

        d = dict(src_dict)
        items = []
        _items = d.pop("items")
        for items_item_data in _items:
            items_item = UnmanagedMachine.from_dict(items_item_data)

            items.append(items_item)

        _unavailable = d.pop("unavailable", UNSET)
        unavailable: list[ProviderNote] | Unset = UNSET
        if _unavailable is not UNSET:
            unavailable = []
            for unavailable_item_data in _unavailable:
                unavailable_item = ProviderNote.from_dict(unavailable_item_data)

                unavailable.append(unavailable_item)

        unmanaged_machine_list = cls(
            items=items,
            unavailable=unavailable,
        )

        unmanaged_machine_list.additional_properties = d
        return unmanaged_machine_list

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
