from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

T = TypeVar("T", bound="OpsMachine")


@_attrs_define
class OpsMachine:
    """
    Attributes:
        id (str):
        name (str):
        org_id (str):
        provider (str):
        tenancy (str):
        state (str):
        chats_served (int):
    """

    id: str
    name: str
    org_id: str
    provider: str
    tenancy: str
    state: str
    chats_served: int
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        id = self.id

        name = self.name

        org_id = self.org_id

        provider = self.provider

        tenancy = self.tenancy

        state = self.state

        chats_served = self.chats_served

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "name": name,
                "org_id": org_id,
                "provider": provider,
                "tenancy": tenancy,
                "state": state,
                "chats_served": chats_served,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        id = d.pop("id")

        name = d.pop("name")

        org_id = d.pop("org_id")

        provider = d.pop("provider")

        tenancy = d.pop("tenancy")

        state = d.pop("state")

        chats_served = d.pop("chats_served")

        ops_machine = cls(
            id=id,
            name=name,
            org_id=org_id,
            provider=provider,
            tenancy=tenancy,
            state=state,
            chats_served=chats_served,
        )

        ops_machine.additional_properties = d
        return ops_machine

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
