from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast
from uuid import UUID

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="WorkspaceCreate")


@_attrs_define
class WorkspaceCreate:
    """A new project workspace, with a folder of its own.

    Attributes:
        title (str):
        client_id (None | str | Unset):
        machine_pin (None | Unset | UUID):
    """

    title: str
    client_id: None | str | Unset = UNSET
    machine_pin: None | Unset | UUID = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        title = self.title

        client_id: None | str | Unset
        if isinstance(self.client_id, Unset):
            client_id = UNSET
        else:
            client_id = self.client_id

        machine_pin: None | str | Unset
        if isinstance(self.machine_pin, Unset):
            machine_pin = UNSET
        elif isinstance(self.machine_pin, UUID):
            machine_pin = str(self.machine_pin)
        else:
            machine_pin = self.machine_pin

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "title": title,
            }
        )
        if client_id is not UNSET:
            field_dict["client_id"] = client_id
        if machine_pin is not UNSET:
            field_dict["machine_pin"] = machine_pin

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        title = d.pop("title")

        def _parse_client_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        client_id = _parse_client_id(d.pop("client_id", UNSET))

        def _parse_machine_pin(data: object) -> None | Unset | UUID:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                machine_pin_type_0 = UUID(data)

                return machine_pin_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | Unset | UUID, data)

        machine_pin = _parse_machine_pin(d.pop("machine_pin", UNSET))

        workspace_create = cls(
            title=title,
            client_id=client_id,
            machine_pin=machine_pin,
        )

        workspace_create.additional_properties = d
        return workspace_create

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
