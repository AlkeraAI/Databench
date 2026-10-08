from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast
from uuid import UUID

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.planned_org_read_fate import PlannedOrgReadFate
from ..types import UNSET, Unset

T = TypeVar("T", bound="PlannedOrgRead")


@_attrs_define
class PlannedOrgRead:
    """
    Attributes:
        org_id (UUID):
        org_name (str):
        fate (PlannedOrgReadFate):
        shared_items (int):
        private_items (int):
        transfer_to_name (None | str | Unset):
    """

    org_id: UUID
    org_name: str
    fate: PlannedOrgReadFate
    shared_items: int
    private_items: int
    transfer_to_name: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        org_id = str(self.org_id)

        org_name = self.org_name

        fate = self.fate.value

        shared_items = self.shared_items

        private_items = self.private_items

        transfer_to_name: None | str | Unset
        if isinstance(self.transfer_to_name, Unset):
            transfer_to_name = UNSET
        else:
            transfer_to_name = self.transfer_to_name

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "org_id": org_id,
                "org_name": org_name,
                "fate": fate,
                "shared_items": shared_items,
                "private_items": private_items,
            }
        )
        if transfer_to_name is not UNSET:
            field_dict["transfer_to_name"] = transfer_to_name

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        org_id = UUID(d.pop("org_id"))

        org_name = d.pop("org_name")

        fate = PlannedOrgReadFate(d.pop("fate"))

        shared_items = d.pop("shared_items")

        private_items = d.pop("private_items")

        def _parse_transfer_to_name(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        transfer_to_name = _parse_transfer_to_name(d.pop("transfer_to_name", UNSET))

        planned_org_read = cls(
            org_id=org_id,
            org_name=org_name,
            fate=fate,
            shared_items=shared_items,
            private_items=private_items,
            transfer_to_name=transfer_to_name,
        )

        planned_org_read.additional_properties = d
        return planned_org_read

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
