from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.machine_claim_request_sandbox import MachineClaimRequestSandbox
from ..types import UNSET, Unset

T = TypeVar("T", bound="MachineClaimRequest")


@_attrs_define
class MachineClaimRequest:
    """A platform box claims the machine its credential was minted for. The
    kind, size and tenancy come from the credential, never from the box; the
    box says only which instance it is and what it can hold.

        Attributes:
            provider_pod_id (str):
            name (str):
            capacity (int | Unset):  Default: 6.
            daemon_version (str | Unset):  Default: ''.
            daemon_instance_id (None | str | Unset):
            sandbox (MachineClaimRequestSandbox | Unset):  Default: MachineClaimRequestSandbox.NONE.
    """

    provider_pod_id: str
    name: str
    capacity: int | Unset = 6
    daemon_version: str | Unset = ""
    daemon_instance_id: None | str | Unset = UNSET
    sandbox: MachineClaimRequestSandbox | Unset = MachineClaimRequestSandbox.NONE
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        provider_pod_id = self.provider_pod_id

        name = self.name

        capacity = self.capacity

        daemon_version = self.daemon_version

        daemon_instance_id: None | str | Unset
        if isinstance(self.daemon_instance_id, Unset):
            daemon_instance_id = UNSET
        else:
            daemon_instance_id = self.daemon_instance_id

        sandbox: str | Unset = UNSET
        if not isinstance(self.sandbox, Unset):
            sandbox = self.sandbox.value

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "provider_pod_id": provider_pod_id,
                "name": name,
            }
        )
        if capacity is not UNSET:
            field_dict["capacity"] = capacity
        if daemon_version is not UNSET:
            field_dict["daemon_version"] = daemon_version
        if daemon_instance_id is not UNSET:
            field_dict["daemon_instance_id"] = daemon_instance_id
        if sandbox is not UNSET:
            field_dict["sandbox"] = sandbox

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        provider_pod_id = d.pop("provider_pod_id")

        name = d.pop("name")

        capacity = d.pop("capacity", UNSET)

        daemon_version = d.pop("daemon_version", UNSET)

        def _parse_daemon_instance_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        daemon_instance_id = _parse_daemon_instance_id(d.pop("daemon_instance_id", UNSET))

        _sandbox = d.pop("sandbox", UNSET)
        sandbox: MachineClaimRequestSandbox | Unset
        if isinstance(_sandbox, Unset):
            sandbox = UNSET
        else:
            sandbox = MachineClaimRequestSandbox(_sandbox)

        machine_claim_request = cls(
            provider_pod_id=provider_pod_id,
            name=name,
            capacity=capacity,
            daemon_version=daemon_version,
            daemon_instance_id=daemon_instance_id,
            sandbox=sandbox,
        )

        machine_claim_request.additional_properties = d
        return machine_claim_request

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
