from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.platform_machine_read_status import PlatformMachineReadStatus
from ..models.platform_machine_read_tenancy import PlatformMachineReadTenancy
from ..types import UNSET, Unset

T = TypeVar("T", bound="PlatformMachineRead")


@_attrs_define
class PlatformMachineRead:
    """One platform box: its credential, and its registration when it has one.

    Attributes:
        credential_id (str):
        label (str):
        provider (str):
        instance_type (str):
        tenancy (PlatformMachineReadTenancy):
        created_at (datetime.datetime):
        region (str | Unset):  Default: ''.
        revoked_at (datetime.datetime | None | Unset):
        last_used_at (datetime.datetime | None | Unset):
        machine_id (None | str | Unset):
        machine_name (str | Unset):  Default: ''.
        status (PlatformMachineReadStatus | Unset):  Default: PlatformMachineReadStatus.NONE.
        last_heartbeat_at (datetime.datetime | None | Unset):
        chats_served (int | Unset):  Default: 0.
        capacity (int | Unset):  Default: 0.
        daemon_version (str | Unset):  Default: ''.
        true_cost_per_minute_nanos (int | Unset):  Default: 0.
        assigned_org_id (None | str | Unset):
        assigned_org_name (str | Unset):  Default: ''.
    """

    credential_id: str
    label: str
    provider: str
    instance_type: str
    tenancy: PlatformMachineReadTenancy
    created_at: datetime.datetime
    region: str | Unset = ""
    revoked_at: datetime.datetime | None | Unset = UNSET
    last_used_at: datetime.datetime | None | Unset = UNSET
    machine_id: None | str | Unset = UNSET
    machine_name: str | Unset = ""
    status: PlatformMachineReadStatus | Unset = PlatformMachineReadStatus.NONE
    last_heartbeat_at: datetime.datetime | None | Unset = UNSET
    chats_served: int | Unset = 0
    capacity: int | Unset = 0
    daemon_version: str | Unset = ""
    true_cost_per_minute_nanos: int | Unset = 0
    assigned_org_id: None | str | Unset = UNSET
    assigned_org_name: str | Unset = ""
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        credential_id = self.credential_id

        label = self.label

        provider = self.provider

        instance_type = self.instance_type

        tenancy = self.tenancy.value

        created_at = self.created_at.isoformat()

        region = self.region

        revoked_at: None | str | Unset
        if isinstance(self.revoked_at, Unset):
            revoked_at = UNSET
        elif isinstance(self.revoked_at, datetime.datetime):
            revoked_at = self.revoked_at.isoformat()
        else:
            revoked_at = self.revoked_at

        last_used_at: None | str | Unset
        if isinstance(self.last_used_at, Unset):
            last_used_at = UNSET
        elif isinstance(self.last_used_at, datetime.datetime):
            last_used_at = self.last_used_at.isoformat()
        else:
            last_used_at = self.last_used_at

        machine_id: None | str | Unset
        if isinstance(self.machine_id, Unset):
            machine_id = UNSET
        else:
            machine_id = self.machine_id

        machine_name = self.machine_name

        status: str | Unset = UNSET
        if not isinstance(self.status, Unset):
            status = self.status.value

        last_heartbeat_at: None | str | Unset
        if isinstance(self.last_heartbeat_at, Unset):
            last_heartbeat_at = UNSET
        elif isinstance(self.last_heartbeat_at, datetime.datetime):
            last_heartbeat_at = self.last_heartbeat_at.isoformat()
        else:
            last_heartbeat_at = self.last_heartbeat_at

        chats_served = self.chats_served

        capacity = self.capacity

        daemon_version = self.daemon_version

        true_cost_per_minute_nanos = self.true_cost_per_minute_nanos

        assigned_org_id: None | str | Unset
        if isinstance(self.assigned_org_id, Unset):
            assigned_org_id = UNSET
        else:
            assigned_org_id = self.assigned_org_id

        assigned_org_name = self.assigned_org_name

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "credential_id": credential_id,
                "label": label,
                "provider": provider,
                "instance_type": instance_type,
                "tenancy": tenancy,
                "created_at": created_at,
            }
        )
        if region is not UNSET:
            field_dict["region"] = region
        if revoked_at is not UNSET:
            field_dict["revoked_at"] = revoked_at
        if last_used_at is not UNSET:
            field_dict["last_used_at"] = last_used_at
        if machine_id is not UNSET:
            field_dict["machine_id"] = machine_id
        if machine_name is not UNSET:
            field_dict["machine_name"] = machine_name
        if status is not UNSET:
            field_dict["status"] = status
        if last_heartbeat_at is not UNSET:
            field_dict["last_heartbeat_at"] = last_heartbeat_at
        if chats_served is not UNSET:
            field_dict["chats_served"] = chats_served
        if capacity is not UNSET:
            field_dict["capacity"] = capacity
        if daemon_version is not UNSET:
            field_dict["daemon_version"] = daemon_version
        if true_cost_per_minute_nanos is not UNSET:
            field_dict["true_cost_per_minute_nanos"] = true_cost_per_minute_nanos
        if assigned_org_id is not UNSET:
            field_dict["assigned_org_id"] = assigned_org_id
        if assigned_org_name is not UNSET:
            field_dict["assigned_org_name"] = assigned_org_name

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        credential_id = d.pop("credential_id")

        label = d.pop("label")

        provider = d.pop("provider")

        instance_type = d.pop("instance_type")

        tenancy = PlatformMachineReadTenancy(d.pop("tenancy"))

        created_at = datetime.datetime.fromisoformat(d.pop("created_at"))

        region = d.pop("region", UNSET)

        def _parse_revoked_at(data: object) -> datetime.datetime | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                revoked_at_type_0 = datetime.datetime.fromisoformat(data)

                return revoked_at_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None | Unset, data)

        revoked_at = _parse_revoked_at(d.pop("revoked_at", UNSET))

        def _parse_last_used_at(data: object) -> datetime.datetime | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                last_used_at_type_0 = datetime.datetime.fromisoformat(data)

                return last_used_at_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None | Unset, data)

        last_used_at = _parse_last_used_at(d.pop("last_used_at", UNSET))

        def _parse_machine_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        machine_id = _parse_machine_id(d.pop("machine_id", UNSET))

        machine_name = d.pop("machine_name", UNSET)

        _status = d.pop("status", UNSET)
        status: PlatformMachineReadStatus | Unset
        if isinstance(_status, Unset):
            status = UNSET
        else:
            status = PlatformMachineReadStatus(_status)

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

        chats_served = d.pop("chats_served", UNSET)

        capacity = d.pop("capacity", UNSET)

        daemon_version = d.pop("daemon_version", UNSET)

        true_cost_per_minute_nanos = d.pop("true_cost_per_minute_nanos", UNSET)

        def _parse_assigned_org_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        assigned_org_id = _parse_assigned_org_id(d.pop("assigned_org_id", UNSET))

        assigned_org_name = d.pop("assigned_org_name", UNSET)

        platform_machine_read = cls(
            credential_id=credential_id,
            label=label,
            provider=provider,
            instance_type=instance_type,
            tenancy=tenancy,
            created_at=created_at,
            region=region,
            revoked_at=revoked_at,
            last_used_at=last_used_at,
            machine_id=machine_id,
            machine_name=machine_name,
            status=status,
            last_heartbeat_at=last_heartbeat_at,
            chats_served=chats_served,
            capacity=capacity,
            daemon_version=daemon_version,
            true_cost_per_minute_nanos=true_cost_per_minute_nanos,
            assigned_org_id=assigned_org_id,
            assigned_org_name=assigned_org_name,
        )

        platform_machine_read.additional_properties = d
        return platform_machine_read

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
