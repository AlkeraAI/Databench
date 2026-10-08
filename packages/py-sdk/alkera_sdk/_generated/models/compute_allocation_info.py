from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.compute_allocation_info_machine_status_type_0 import (
    ComputeAllocationInfoMachineStatusType0,
)
from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.machine_type_info import MachineTypeInfo


T = TypeVar("T", bound="ComputeAllocationInfo")


@_attrs_define
class ComputeAllocationInfo:
    """A user's provisioned (or terminated) compute allocation.

    Attributes:
        id (str):
        machine_type (MachineTypeInfo): One provisionable machine type from the catalog, priced for the caller.
        state (str):
        created_at (datetime.datetime):
        lifecycle (str | Unset):  Default: 'session'.
        name (str | Unset):  Default: ''.
        provider_machine_id (str | Unset):  Default: ''.
        public_ip (str | Unset):  Default: ''.
        ssh_port (int | Unset):  Default: 0.
        ssh_user (str | Unset):  Default: 'root'.
        ready_at (datetime.datetime | None | Unset):
        released_at (datetime.datetime | None | Unset):
        minutes_elapsed (int | Unset):  Default: 0.
        spend_nanos (int | Unset):  Default: 0.
        minutes_billed (int | Unset):  Default: 0.
        billed_nanos (int | Unset):  Default: 0.
        terminated_reason (str | Unset):  Default: ''.
        low_credit (bool | Unset):  Default: False.
        max_lease_minutes (int | None | Unset):
        status_message (str | Unset):  Default: ''.
        error (str | Unset):  Default: ''.
        project_path (str | Unset):  Default: ''.
        session_id (str | Unset):  Default: ''.
        machine_status (ComputeAllocationInfoMachineStatusType0 | None | Unset):
        last_heartbeat_at (datetime.datetime | None | Unset):
    """

    id: str
    machine_type: MachineTypeInfo
    state: str
    created_at: datetime.datetime
    lifecycle: str | Unset = "session"
    name: str | Unset = ""
    provider_machine_id: str | Unset = ""
    public_ip: str | Unset = ""
    ssh_port: int | Unset = 0
    ssh_user: str | Unset = "root"
    ready_at: datetime.datetime | None | Unset = UNSET
    released_at: datetime.datetime | None | Unset = UNSET
    minutes_elapsed: int | Unset = 0
    spend_nanos: int | Unset = 0
    minutes_billed: int | Unset = 0
    billed_nanos: int | Unset = 0
    terminated_reason: str | Unset = ""
    low_credit: bool | Unset = False
    max_lease_minutes: int | None | Unset = UNSET
    status_message: str | Unset = ""
    error: str | Unset = ""
    project_path: str | Unset = ""
    session_id: str | Unset = ""
    machine_status: ComputeAllocationInfoMachineStatusType0 | None | Unset = UNSET
    last_heartbeat_at: datetime.datetime | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        id = self.id

        machine_type = self.machine_type.to_dict()

        state = self.state

        created_at = self.created_at.isoformat()

        lifecycle = self.lifecycle

        name = self.name

        provider_machine_id = self.provider_machine_id

        public_ip = self.public_ip

        ssh_port = self.ssh_port

        ssh_user = self.ssh_user

        ready_at: None | str | Unset
        if isinstance(self.ready_at, Unset):
            ready_at = UNSET
        elif isinstance(self.ready_at, datetime.datetime):
            ready_at = self.ready_at.isoformat()
        else:
            ready_at = self.ready_at

        released_at: None | str | Unset
        if isinstance(self.released_at, Unset):
            released_at = UNSET
        elif isinstance(self.released_at, datetime.datetime):
            released_at = self.released_at.isoformat()
        else:
            released_at = self.released_at

        minutes_elapsed = self.minutes_elapsed

        spend_nanos = self.spend_nanos

        minutes_billed = self.minutes_billed

        billed_nanos = self.billed_nanos

        terminated_reason = self.terminated_reason

        low_credit = self.low_credit

        max_lease_minutes: int | None | Unset
        if isinstance(self.max_lease_minutes, Unset):
            max_lease_minutes = UNSET
        else:
            max_lease_minutes = self.max_lease_minutes

        status_message = self.status_message

        error = self.error

        project_path = self.project_path

        session_id = self.session_id

        machine_status: None | str | Unset
        if isinstance(self.machine_status, Unset):
            machine_status = UNSET
        elif isinstance(self.machine_status, ComputeAllocationInfoMachineStatusType0):
            machine_status = self.machine_status.value
        else:
            machine_status = self.machine_status

        last_heartbeat_at: None | str | Unset
        if isinstance(self.last_heartbeat_at, Unset):
            last_heartbeat_at = UNSET
        elif isinstance(self.last_heartbeat_at, datetime.datetime):
            last_heartbeat_at = self.last_heartbeat_at.isoformat()
        else:
            last_heartbeat_at = self.last_heartbeat_at

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "machine_type": machine_type,
                "state": state,
                "created_at": created_at,
            }
        )
        if lifecycle is not UNSET:
            field_dict["lifecycle"] = lifecycle
        if name is not UNSET:
            field_dict["name"] = name
        if provider_machine_id is not UNSET:
            field_dict["provider_machine_id"] = provider_machine_id
        if public_ip is not UNSET:
            field_dict["public_ip"] = public_ip
        if ssh_port is not UNSET:
            field_dict["ssh_port"] = ssh_port
        if ssh_user is not UNSET:
            field_dict["ssh_user"] = ssh_user
        if ready_at is not UNSET:
            field_dict["ready_at"] = ready_at
        if released_at is not UNSET:
            field_dict["released_at"] = released_at
        if minutes_elapsed is not UNSET:
            field_dict["minutes_elapsed"] = minutes_elapsed
        if spend_nanos is not UNSET:
            field_dict["spend_nanos"] = spend_nanos
        if minutes_billed is not UNSET:
            field_dict["minutes_billed"] = minutes_billed
        if billed_nanos is not UNSET:
            field_dict["billed_nanos"] = billed_nanos
        if terminated_reason is not UNSET:
            field_dict["terminated_reason"] = terminated_reason
        if low_credit is not UNSET:
            field_dict["low_credit"] = low_credit
        if max_lease_minutes is not UNSET:
            field_dict["max_lease_minutes"] = max_lease_minutes
        if status_message is not UNSET:
            field_dict["status_message"] = status_message
        if error is not UNSET:
            field_dict["error"] = error
        if project_path is not UNSET:
            field_dict["project_path"] = project_path
        if session_id is not UNSET:
            field_dict["session_id"] = session_id
        if machine_status is not UNSET:
            field_dict["machine_status"] = machine_status
        if last_heartbeat_at is not UNSET:
            field_dict["last_heartbeat_at"] = last_heartbeat_at

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.machine_type_info import MachineTypeInfo

        d = dict(src_dict)
        id = d.pop("id")

        machine_type = MachineTypeInfo.from_dict(d.pop("machine_type"))

        state = d.pop("state")

        created_at = datetime.datetime.fromisoformat(d.pop("created_at"))

        lifecycle = d.pop("lifecycle", UNSET)

        name = d.pop("name", UNSET)

        provider_machine_id = d.pop("provider_machine_id", UNSET)

        public_ip = d.pop("public_ip", UNSET)

        ssh_port = d.pop("ssh_port", UNSET)

        ssh_user = d.pop("ssh_user", UNSET)

        def _parse_ready_at(data: object) -> datetime.datetime | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                ready_at_type_0 = datetime.datetime.fromisoformat(data)

                return ready_at_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None | Unset, data)

        ready_at = _parse_ready_at(d.pop("ready_at", UNSET))

        def _parse_released_at(data: object) -> datetime.datetime | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                released_at_type_0 = datetime.datetime.fromisoformat(data)

                return released_at_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None | Unset, data)

        released_at = _parse_released_at(d.pop("released_at", UNSET))

        minutes_elapsed = d.pop("minutes_elapsed", UNSET)

        spend_nanos = d.pop("spend_nanos", UNSET)

        minutes_billed = d.pop("minutes_billed", UNSET)

        billed_nanos = d.pop("billed_nanos", UNSET)

        terminated_reason = d.pop("terminated_reason", UNSET)

        low_credit = d.pop("low_credit", UNSET)

        def _parse_max_lease_minutes(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        max_lease_minutes = _parse_max_lease_minutes(d.pop("max_lease_minutes", UNSET))

        status_message = d.pop("status_message", UNSET)

        error = d.pop("error", UNSET)

        project_path = d.pop("project_path", UNSET)

        session_id = d.pop("session_id", UNSET)

        def _parse_machine_status(
            data: object,
        ) -> ComputeAllocationInfoMachineStatusType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                machine_status_type_0 = ComputeAllocationInfoMachineStatusType0(data)

                return machine_status_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(ComputeAllocationInfoMachineStatusType0 | None | Unset, data)

        machine_status = _parse_machine_status(d.pop("machine_status", UNSET))

        def _parse_last_heartbeat_at(data: object) -> datetime.datetime | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                last_heartbeat_at_type_0 = datetime.datetime.fromisoformat(data)

                return last_heartbeat_at_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None | Unset, data)

        last_heartbeat_at = _parse_last_heartbeat_at(d.pop("last_heartbeat_at", UNSET))

        compute_allocation_info = cls(
            id=id,
            machine_type=machine_type,
            state=state,
            created_at=created_at,
            lifecycle=lifecycle,
            name=name,
            provider_machine_id=provider_machine_id,
            public_ip=public_ip,
            ssh_port=ssh_port,
            ssh_user=ssh_user,
            ready_at=ready_at,
            released_at=released_at,
            minutes_elapsed=minutes_elapsed,
            spend_nanos=spend_nanos,
            minutes_billed=minutes_billed,
            billed_nanos=billed_nanos,
            terminated_reason=terminated_reason,
            low_credit=low_credit,
            max_lease_minutes=max_lease_minutes,
            status_message=status_message,
            error=error,
            project_path=project_path,
            session_id=session_id,
            machine_status=machine_status,
            last_heartbeat_at=last_heartbeat_at,
        )

        compute_allocation_info.additional_properties = d
        return compute_allocation_info

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
