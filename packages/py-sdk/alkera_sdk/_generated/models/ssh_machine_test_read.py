from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="SshMachineTestRead")


@_attrs_define
class SshMachineTestRead:
    """What a connection test found. ``reachable`` false carries the reason in
    ``error_code`` and ``message``; the facts are then empty.

        Attributes:
            reachable (bool):
            host_key_fingerprint (None | str | Unset):
            host_key_type (None | str | Unset):
            os (str | Unset):  Default: ''.
            arch (str | Unset):  Default: ''.
            vcpu (int | Unset):  Default: 0.
            memory_gb (int | Unset):  Default: 0.
            disk_gb (int | Unset):  Default: 0.
            gpu_count (int | Unset):  Default: 0.
            prerequisites_met (bool | Unset):  Default: False.
            missing (list[str] | Unset):
            error_code (None | str | Unset):
            message (None | str | Unset):
    """

    reachable: bool
    host_key_fingerprint: None | str | Unset = UNSET
    host_key_type: None | str | Unset = UNSET
    os: str | Unset = ""
    arch: str | Unset = ""
    vcpu: int | Unset = 0
    memory_gb: int | Unset = 0
    disk_gb: int | Unset = 0
    gpu_count: int | Unset = 0
    prerequisites_met: bool | Unset = False
    missing: list[str] | Unset = UNSET
    error_code: None | str | Unset = UNSET
    message: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        reachable = self.reachable

        host_key_fingerprint: None | str | Unset
        if isinstance(self.host_key_fingerprint, Unset):
            host_key_fingerprint = UNSET
        else:
            host_key_fingerprint = self.host_key_fingerprint

        host_key_type: None | str | Unset
        if isinstance(self.host_key_type, Unset):
            host_key_type = UNSET
        else:
            host_key_type = self.host_key_type

        os = self.os

        arch = self.arch

        vcpu = self.vcpu

        memory_gb = self.memory_gb

        disk_gb = self.disk_gb

        gpu_count = self.gpu_count

        prerequisites_met = self.prerequisites_met

        missing: list[str] | Unset = UNSET
        if not isinstance(self.missing, Unset):
            missing = self.missing

        error_code: None | str | Unset
        if isinstance(self.error_code, Unset):
            error_code = UNSET
        else:
            error_code = self.error_code

        message: None | str | Unset
        if isinstance(self.message, Unset):
            message = UNSET
        else:
            message = self.message

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "reachable": reachable,
            }
        )
        if host_key_fingerprint is not UNSET:
            field_dict["host_key_fingerprint"] = host_key_fingerprint
        if host_key_type is not UNSET:
            field_dict["host_key_type"] = host_key_type
        if os is not UNSET:
            field_dict["os"] = os
        if arch is not UNSET:
            field_dict["arch"] = arch
        if vcpu is not UNSET:
            field_dict["vcpu"] = vcpu
        if memory_gb is not UNSET:
            field_dict["memory_gb"] = memory_gb
        if disk_gb is not UNSET:
            field_dict["disk_gb"] = disk_gb
        if gpu_count is not UNSET:
            field_dict["gpu_count"] = gpu_count
        if prerequisites_met is not UNSET:
            field_dict["prerequisites_met"] = prerequisites_met
        if missing is not UNSET:
            field_dict["missing"] = missing
        if error_code is not UNSET:
            field_dict["error_code"] = error_code
        if message is not UNSET:
            field_dict["message"] = message

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        reachable = d.pop("reachable")

        def _parse_host_key_fingerprint(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        host_key_fingerprint = _parse_host_key_fingerprint(d.pop("host_key_fingerprint", UNSET))

        def _parse_host_key_type(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        host_key_type = _parse_host_key_type(d.pop("host_key_type", UNSET))

        os = d.pop("os", UNSET)

        arch = d.pop("arch", UNSET)

        vcpu = d.pop("vcpu", UNSET)

        memory_gb = d.pop("memory_gb", UNSET)

        disk_gb = d.pop("disk_gb", UNSET)

        gpu_count = d.pop("gpu_count", UNSET)

        prerequisites_met = d.pop("prerequisites_met", UNSET)

        missing = cast(list[str], d.pop("missing", UNSET))

        def _parse_error_code(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        error_code = _parse_error_code(d.pop("error_code", UNSET))

        def _parse_message(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        message = _parse_message(d.pop("message", UNSET))

        ssh_machine_test_read = cls(
            reachable=reachable,
            host_key_fingerprint=host_key_fingerprint,
            host_key_type=host_key_type,
            os=os,
            arch=arch,
            vcpu=vcpu,
            memory_gb=memory_gb,
            disk_gb=disk_gb,
            gpu_count=gpu_count,
            prerequisites_met=prerequisites_met,
            missing=missing,
            error_code=error_code,
            message=message,
        )

        ssh_machine_test_read.additional_properties = d
        return ssh_machine_test_read

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
