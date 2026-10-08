from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="MachineRegisterRequest")


@_attrs_define
class MachineRegisterRequest:
    """Register a running pod as the org's workspace machine (idempotent by
    ``provider_pod_id``: registering the same pod twice returns the same row).

        Attributes:
            provider_pod_id (str):
            name (str):
            machine_type_code (str):
            provider (str | Unset):  Default: 'runpod'.
            daemon_instance_id (None | str | Unset):
    """

    provider_pod_id: str
    name: str
    machine_type_code: str
    provider: str | Unset = "runpod"
    daemon_instance_id: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        provider_pod_id = self.provider_pod_id

        name = self.name

        machine_type_code = self.machine_type_code

        provider = self.provider

        daemon_instance_id: None | str | Unset
        if isinstance(self.daemon_instance_id, Unset):
            daemon_instance_id = UNSET
        else:
            daemon_instance_id = self.daemon_instance_id

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "provider_pod_id": provider_pod_id,
                "name": name,
                "machine_type_code": machine_type_code,
            }
        )
        if provider is not UNSET:
            field_dict["provider"] = provider
        if daemon_instance_id is not UNSET:
            field_dict["daemon_instance_id"] = daemon_instance_id

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        provider_pod_id = d.pop("provider_pod_id")

        name = d.pop("name")

        machine_type_code = d.pop("machine_type_code")

        provider = d.pop("provider", UNSET)

        def _parse_daemon_instance_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        daemon_instance_id = _parse_daemon_instance_id(d.pop("daemon_instance_id", UNSET))

        machine_register_request = cls(
            provider_pod_id=provider_pod_id,
            name=name,
            machine_type_code=machine_type_code,
            provider=provider,
            daemon_instance_id=daemon_instance_id,
        )

        machine_register_request.additional_properties = d
        return machine_register_request

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
