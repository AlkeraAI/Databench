from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.machine_heartbeat_request_sandbox_type_0 import MachineHeartbeatRequestSandboxType0
from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.machine_fault_report import MachineFaultReport
    from ..models.machine_isolation_report import MachineIsolationReport
    from ..models.machine_resources import MachineResources


T = TypeVar("T", bound="MachineHeartbeatRequest")


@_attrs_define
class MachineHeartbeatRequest:
    """What a box says with each heartbeat, all optional: how many chats it
    can hold and holds now (how a pool is spread), the daemon it runs, and
    whether it has been told to stop and is finishing what it holds.

        Attributes:
            capacity (int | None | Unset):
            chats_served (int | None | Unset):
            daemon_version (None | str | Unset):
            draining (bool | None | Unset):
            restarting (bool | None | Unset):
            daemon_instance_id (None | str | Unset):
            resources (MachineResources | None | Unset):
            sandbox (MachineHeartbeatRequestSandboxType0 | None | Unset):
            capabilities (list[str] | None | Unset):
            isolation (MachineIsolationReport | None | Unset):
            fault (MachineFaultReport | None | Unset):
            last_activity_at (datetime.datetime | None | Unset):
    """

    capacity: int | None | Unset = UNSET
    chats_served: int | None | Unset = UNSET
    daemon_version: None | str | Unset = UNSET
    draining: bool | None | Unset = UNSET
    restarting: bool | None | Unset = UNSET
    daemon_instance_id: None | str | Unset = UNSET
    resources: MachineResources | None | Unset = UNSET
    sandbox: MachineHeartbeatRequestSandboxType0 | None | Unset = UNSET
    capabilities: list[str] | None | Unset = UNSET
    isolation: MachineIsolationReport | None | Unset = UNSET
    fault: MachineFaultReport | None | Unset = UNSET
    last_activity_at: datetime.datetime | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.machine_fault_report import MachineFaultReport
        from ..models.machine_isolation_report import MachineIsolationReport
        from ..models.machine_resources import MachineResources

        capacity: int | None | Unset
        if isinstance(self.capacity, Unset):
            capacity = UNSET
        else:
            capacity = self.capacity

        chats_served: int | None | Unset
        if isinstance(self.chats_served, Unset):
            chats_served = UNSET
        else:
            chats_served = self.chats_served

        daemon_version: None | str | Unset
        if isinstance(self.daemon_version, Unset):
            daemon_version = UNSET
        else:
            daemon_version = self.daemon_version

        draining: bool | None | Unset
        if isinstance(self.draining, Unset):
            draining = UNSET
        else:
            draining = self.draining

        restarting: bool | None | Unset
        if isinstance(self.restarting, Unset):
            restarting = UNSET
        else:
            restarting = self.restarting

        daemon_instance_id: None | str | Unset
        if isinstance(self.daemon_instance_id, Unset):
            daemon_instance_id = UNSET
        else:
            daemon_instance_id = self.daemon_instance_id

        resources: dict[str, Any] | None | Unset
        if isinstance(self.resources, Unset):
            resources = UNSET
        elif isinstance(self.resources, MachineResources):
            resources = self.resources.to_dict()
        else:
            resources = self.resources

        sandbox: None | str | Unset
        if isinstance(self.sandbox, Unset):
            sandbox = UNSET
        elif isinstance(self.sandbox, MachineHeartbeatRequestSandboxType0):
            sandbox = self.sandbox.value
        else:
            sandbox = self.sandbox

        capabilities: list[str] | None | Unset
        if isinstance(self.capabilities, Unset):
            capabilities = UNSET
        elif isinstance(self.capabilities, list):
            capabilities = self.capabilities

        else:
            capabilities = self.capabilities

        isolation: dict[str, Any] | None | Unset
        if isinstance(self.isolation, Unset):
            isolation = UNSET
        elif isinstance(self.isolation, MachineIsolationReport):
            isolation = self.isolation.to_dict()
        else:
            isolation = self.isolation

        fault: dict[str, Any] | None | Unset
        if isinstance(self.fault, Unset):
            fault = UNSET
        elif isinstance(self.fault, MachineFaultReport):
            fault = self.fault.to_dict()
        else:
            fault = self.fault

        last_activity_at: None | str | Unset
        if isinstance(self.last_activity_at, Unset):
            last_activity_at = UNSET
        elif isinstance(self.last_activity_at, datetime.datetime):
            last_activity_at = self.last_activity_at.isoformat()
        else:
            last_activity_at = self.last_activity_at

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if capacity is not UNSET:
            field_dict["capacity"] = capacity
        if chats_served is not UNSET:
            field_dict["chats_served"] = chats_served
        if daemon_version is not UNSET:
            field_dict["daemon_version"] = daemon_version
        if draining is not UNSET:
            field_dict["draining"] = draining
        if restarting is not UNSET:
            field_dict["restarting"] = restarting
        if daemon_instance_id is not UNSET:
            field_dict["daemon_instance_id"] = daemon_instance_id
        if resources is not UNSET:
            field_dict["resources"] = resources
        if sandbox is not UNSET:
            field_dict["sandbox"] = sandbox
        if capabilities is not UNSET:
            field_dict["capabilities"] = capabilities
        if isolation is not UNSET:
            field_dict["isolation"] = isolation
        if fault is not UNSET:
            field_dict["fault"] = fault
        if last_activity_at is not UNSET:
            field_dict["last_activity_at"] = last_activity_at

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.machine_fault_report import MachineFaultReport
        from ..models.machine_isolation_report import MachineIsolationReport
        from ..models.machine_resources import MachineResources

        d = dict(src_dict)

        def _parse_capacity(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        capacity = _parse_capacity(d.pop("capacity", UNSET))

        def _parse_chats_served(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        chats_served = _parse_chats_served(d.pop("chats_served", UNSET))

        def _parse_daemon_version(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        daemon_version = _parse_daemon_version(d.pop("daemon_version", UNSET))

        def _parse_draining(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        draining = _parse_draining(d.pop("draining", UNSET))

        def _parse_restarting(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        restarting = _parse_restarting(d.pop("restarting", UNSET))

        def _parse_daemon_instance_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        daemon_instance_id = _parse_daemon_instance_id(d.pop("daemon_instance_id", UNSET))

        def _parse_resources(data: object) -> MachineResources | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                resources_type_0 = MachineResources.from_dict(data)

                return resources_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(MachineResources | None | Unset, data)

        resources = _parse_resources(d.pop("resources", UNSET))

        def _parse_sandbox(data: object) -> MachineHeartbeatRequestSandboxType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                sandbox_type_0 = MachineHeartbeatRequestSandboxType0(data)

                return sandbox_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(MachineHeartbeatRequestSandboxType0 | None | Unset, data)

        sandbox = _parse_sandbox(d.pop("sandbox", UNSET))

        def _parse_capabilities(data: object) -> list[str] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                capabilities_type_0 = cast(list[str], data)

                return capabilities_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[str] | None | Unset, data)

        capabilities = _parse_capabilities(d.pop("capabilities", UNSET))

        def _parse_isolation(data: object) -> MachineIsolationReport | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                isolation_type_0 = MachineIsolationReport.from_dict(data)

                return isolation_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(MachineIsolationReport | None | Unset, data)

        isolation = _parse_isolation(d.pop("isolation", UNSET))

        def _parse_fault(data: object) -> MachineFaultReport | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                fault_type_0 = MachineFaultReport.from_dict(data)

                return fault_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(MachineFaultReport | None | Unset, data)

        fault = _parse_fault(d.pop("fault", UNSET))

        def _parse_last_activity_at(data: object) -> datetime.datetime | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                last_activity_at_type_0 = datetime.datetime.fromisoformat(data)

                return last_activity_at_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None | Unset, data)

        last_activity_at = _parse_last_activity_at(d.pop("last_activity_at", UNSET))

        machine_heartbeat_request = cls(
            capacity=capacity,
            chats_served=chats_served,
            daemon_version=daemon_version,
            draining=draining,
            restarting=restarting,
            daemon_instance_id=daemon_instance_id,
            resources=resources,
            sandbox=sandbox,
            capabilities=capabilities,
            isolation=isolation,
            fault=fault,
            last_activity_at=last_activity_at,
        )

        machine_heartbeat_request.additional_properties = d
        return machine_heartbeat_request

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
