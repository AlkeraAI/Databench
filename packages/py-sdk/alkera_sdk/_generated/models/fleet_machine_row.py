from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.fleet_machine_row_acquisition_type_0 import FleetMachineRowAcquisitionType0
from ..models.fleet_machine_row_liveness import FleetMachineRowLiveness
from ..models.fleet_machine_row_origin import FleetMachineRowOrigin
from ..models.fleet_machine_row_sandbox import FleetMachineRowSandbox
from ..models.fleet_machine_row_state import FleetMachineRowState
from ..models.fleet_machine_row_status import FleetMachineRowStatus
from ..models.fleet_machine_row_tenancy import FleetMachineRowTenancy
from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.fleet_org_machine import FleetOrgMachine
    from ..models.machine_cost import MachineCost
    from ..models.machine_drain import MachineDrain
    from ..models.machine_fault_read import MachineFaultRead
    from ..models.machine_isolation_read import MachineIsolationRead
    from ..models.machine_resources import MachineResources
    from ..models.org_ref import OrgRef
    from ..models.status_fact import StatusFact


T = TypeVar("T", bound="FleetMachineRow")


@_attrs_define
class FleetMachineRow:
    """
    Attributes:
        id (str):
        created_at (datetime.datetime):
        name (str | Unset):  Default: ''.
        provider (str | Unset):  Default: ''.
        provider_machine_id (str | Unset):  Default: ''.
        machine_type_code (str | Unset):  Default: ''.
        region (str | Unset):  Default: ''.
        state (FleetMachineRowState | Unset):  Default: FleetMachineRowState.PENDING.
        liveness (FleetMachineRowLiveness | Unset):  Default: FleetMachineRowLiveness.NONE.
        tenancy (FleetMachineRowTenancy | Unset):  Default: FleetMachineRowTenancy.POOL.
        sandbox (FleetMachineRowSandbox | Unset):  Default: FleetMachineRowSandbox.NONE.
        dedicated_org (None | OrgRef | Unset):
        capacity (int | Unset):  Default: 0.
        chats_served (int | Unset):  Default: 0.
        chats_stranded (int | Unset):  Default: 0.
        chats_asleep (int | Unset):  Default: 0.
        storage_gb (int | Unset):  Default: 0.
        heartbeat_at (datetime.datetime | None | Unset):
        daemon_version (str | Unset):  Default: ''.
        state_changed_at (datetime.datetime | None | Unset):
        drain (MachineDrain | None | Unset):
        wake_requested_at (datetime.datetime | None | Unset):
        cost (MachineCost | Unset):
        resources (MachineResources | None | Unset):
        isolation (MachineIsolationRead | None | Unset):
        fault (MachineFaultRead | None | Unset):
        origin (FleetMachineRowOrigin | Unset):  Default: FleetMachineRowOrigin.REGISTERED.
        credential_id (None | str | Unset):
        label (str | Unset):  Default: ''.
        instance_type (str | Unset):  Default: ''.
        revoked_at (datetime.datetime | None | Unset):
        last_used_at (datetime.datetime | None | Unset):
        machine_id (None | str | Unset):
        machine_name (str | Unset):  Default: ''.
        status (FleetMachineRowStatus | Unset):  Default: FleetMachineRowStatus.NONE.
        last_heartbeat_at (datetime.datetime | None | Unset):
        true_cost_per_minute_nanos (int | Unset):  Default: 0.
        assigned_org_id (None | str | Unset):
        assigned_org_name (str | Unset):  Default: ''.
        status_fact (None | StatusFact | Unset):
        org_machine (FleetOrgMachine | None | Unset):
        acquisition (FleetMachineRowAcquisitionType0 | None | Unset):
    """

    id: str
    created_at: datetime.datetime
    name: str | Unset = ""
    provider: str | Unset = ""
    provider_machine_id: str | Unset = ""
    machine_type_code: str | Unset = ""
    region: str | Unset = ""
    state: FleetMachineRowState | Unset = FleetMachineRowState.PENDING
    liveness: FleetMachineRowLiveness | Unset = FleetMachineRowLiveness.NONE
    tenancy: FleetMachineRowTenancy | Unset = FleetMachineRowTenancy.POOL
    sandbox: FleetMachineRowSandbox | Unset = FleetMachineRowSandbox.NONE
    dedicated_org: None | OrgRef | Unset = UNSET
    capacity: int | Unset = 0
    chats_served: int | Unset = 0
    chats_stranded: int | Unset = 0
    chats_asleep: int | Unset = 0
    storage_gb: int | Unset = 0
    heartbeat_at: datetime.datetime | None | Unset = UNSET
    daemon_version: str | Unset = ""
    state_changed_at: datetime.datetime | None | Unset = UNSET
    drain: MachineDrain | None | Unset = UNSET
    wake_requested_at: datetime.datetime | None | Unset = UNSET
    cost: MachineCost | Unset = UNSET
    resources: MachineResources | None | Unset = UNSET
    isolation: MachineIsolationRead | None | Unset = UNSET
    fault: MachineFaultRead | None | Unset = UNSET
    origin: FleetMachineRowOrigin | Unset = FleetMachineRowOrigin.REGISTERED
    credential_id: None | str | Unset = UNSET
    label: str | Unset = ""
    instance_type: str | Unset = ""
    revoked_at: datetime.datetime | None | Unset = UNSET
    last_used_at: datetime.datetime | None | Unset = UNSET
    machine_id: None | str | Unset = UNSET
    machine_name: str | Unset = ""
    status: FleetMachineRowStatus | Unset = FleetMachineRowStatus.NONE
    last_heartbeat_at: datetime.datetime | None | Unset = UNSET
    true_cost_per_minute_nanos: int | Unset = 0
    assigned_org_id: None | str | Unset = UNSET
    assigned_org_name: str | Unset = ""
    status_fact: None | StatusFact | Unset = UNSET
    org_machine: FleetOrgMachine | None | Unset = UNSET
    acquisition: FleetMachineRowAcquisitionType0 | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.fleet_org_machine import FleetOrgMachine
        from ..models.machine_drain import MachineDrain
        from ..models.machine_fault_read import MachineFaultRead
        from ..models.machine_isolation_read import MachineIsolationRead
        from ..models.machine_resources import MachineResources
        from ..models.org_ref import OrgRef
        from ..models.status_fact import StatusFact

        id = self.id

        created_at = self.created_at.isoformat()

        name = self.name

        provider = self.provider

        provider_machine_id = self.provider_machine_id

        machine_type_code = self.machine_type_code

        region = self.region

        state: str | Unset = UNSET
        if not isinstance(self.state, Unset):
            state = self.state.value

        liveness: str | Unset = UNSET
        if not isinstance(self.liveness, Unset):
            liveness = self.liveness.value

        tenancy: str | Unset = UNSET
        if not isinstance(self.tenancy, Unset):
            tenancy = self.tenancy.value

        sandbox: str | Unset = UNSET
        if not isinstance(self.sandbox, Unset):
            sandbox = self.sandbox.value

        dedicated_org: dict[str, Any] | None | Unset
        if isinstance(self.dedicated_org, Unset):
            dedicated_org = UNSET
        elif isinstance(self.dedicated_org, OrgRef):
            dedicated_org = self.dedicated_org.to_dict()
        else:
            dedicated_org = self.dedicated_org

        capacity = self.capacity

        chats_served = self.chats_served

        chats_stranded = self.chats_stranded

        chats_asleep = self.chats_asleep

        storage_gb = self.storage_gb

        heartbeat_at: None | str | Unset
        if isinstance(self.heartbeat_at, Unset):
            heartbeat_at = UNSET
        elif isinstance(self.heartbeat_at, datetime.datetime):
            heartbeat_at = self.heartbeat_at.isoformat()
        else:
            heartbeat_at = self.heartbeat_at

        daemon_version = self.daemon_version

        state_changed_at: None | str | Unset
        if isinstance(self.state_changed_at, Unset):
            state_changed_at = UNSET
        elif isinstance(self.state_changed_at, datetime.datetime):
            state_changed_at = self.state_changed_at.isoformat()
        else:
            state_changed_at = self.state_changed_at

        drain: dict[str, Any] | None | Unset
        if isinstance(self.drain, Unset):
            drain = UNSET
        elif isinstance(self.drain, MachineDrain):
            drain = self.drain.to_dict()
        else:
            drain = self.drain

        wake_requested_at: None | str | Unset
        if isinstance(self.wake_requested_at, Unset):
            wake_requested_at = UNSET
        elif isinstance(self.wake_requested_at, datetime.datetime):
            wake_requested_at = self.wake_requested_at.isoformat()
        else:
            wake_requested_at = self.wake_requested_at

        cost: dict[str, Any] | Unset = UNSET
        if not isinstance(self.cost, Unset):
            cost = self.cost.to_dict()

        resources: dict[str, Any] | None | Unset
        if isinstance(self.resources, Unset):
            resources = UNSET
        elif isinstance(self.resources, MachineResources):
            resources = self.resources.to_dict()
        else:
            resources = self.resources

        isolation: dict[str, Any] | None | Unset
        if isinstance(self.isolation, Unset):
            isolation = UNSET
        elif isinstance(self.isolation, MachineIsolationRead):
            isolation = self.isolation.to_dict()
        else:
            isolation = self.isolation

        fault: dict[str, Any] | None | Unset
        if isinstance(self.fault, Unset):
            fault = UNSET
        elif isinstance(self.fault, MachineFaultRead):
            fault = self.fault.to_dict()
        else:
            fault = self.fault

        origin: str | Unset = UNSET
        if not isinstance(self.origin, Unset):
            origin = self.origin.value

        credential_id: None | str | Unset
        if isinstance(self.credential_id, Unset):
            credential_id = UNSET
        else:
            credential_id = self.credential_id

        label = self.label

        instance_type = self.instance_type

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

        true_cost_per_minute_nanos = self.true_cost_per_minute_nanos

        assigned_org_id: None | str | Unset
        if isinstance(self.assigned_org_id, Unset):
            assigned_org_id = UNSET
        else:
            assigned_org_id = self.assigned_org_id

        assigned_org_name = self.assigned_org_name

        status_fact: dict[str, Any] | None | Unset
        if isinstance(self.status_fact, Unset):
            status_fact = UNSET
        elif isinstance(self.status_fact, StatusFact):
            status_fact = self.status_fact.to_dict()
        else:
            status_fact = self.status_fact

        org_machine: dict[str, Any] | None | Unset
        if isinstance(self.org_machine, Unset):
            org_machine = UNSET
        elif isinstance(self.org_machine, FleetOrgMachine):
            org_machine = self.org_machine.to_dict()
        else:
            org_machine = self.org_machine

        acquisition: None | str | Unset
        if isinstance(self.acquisition, Unset):
            acquisition = UNSET
        elif isinstance(self.acquisition, FleetMachineRowAcquisitionType0):
            acquisition = self.acquisition.value
        else:
            acquisition = self.acquisition

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "created_at": created_at,
            }
        )
        if name is not UNSET:
            field_dict["name"] = name
        if provider is not UNSET:
            field_dict["provider"] = provider
        if provider_machine_id is not UNSET:
            field_dict["provider_machine_id"] = provider_machine_id
        if machine_type_code is not UNSET:
            field_dict["machine_type_code"] = machine_type_code
        if region is not UNSET:
            field_dict["region"] = region
        if state is not UNSET:
            field_dict["state"] = state
        if liveness is not UNSET:
            field_dict["liveness"] = liveness
        if tenancy is not UNSET:
            field_dict["tenancy"] = tenancy
        if sandbox is not UNSET:
            field_dict["sandbox"] = sandbox
        if dedicated_org is not UNSET:
            field_dict["dedicated_org"] = dedicated_org
        if capacity is not UNSET:
            field_dict["capacity"] = capacity
        if chats_served is not UNSET:
            field_dict["chats_served"] = chats_served
        if chats_stranded is not UNSET:
            field_dict["chats_stranded"] = chats_stranded
        if chats_asleep is not UNSET:
            field_dict["chats_asleep"] = chats_asleep
        if storage_gb is not UNSET:
            field_dict["storage_gb"] = storage_gb
        if heartbeat_at is not UNSET:
            field_dict["heartbeat_at"] = heartbeat_at
        if daemon_version is not UNSET:
            field_dict["daemon_version"] = daemon_version
        if state_changed_at is not UNSET:
            field_dict["state_changed_at"] = state_changed_at
        if drain is not UNSET:
            field_dict["drain"] = drain
        if wake_requested_at is not UNSET:
            field_dict["wake_requested_at"] = wake_requested_at
        if cost is not UNSET:
            field_dict["cost"] = cost
        if resources is not UNSET:
            field_dict["resources"] = resources
        if isolation is not UNSET:
            field_dict["isolation"] = isolation
        if fault is not UNSET:
            field_dict["fault"] = fault
        if origin is not UNSET:
            field_dict["origin"] = origin
        if credential_id is not UNSET:
            field_dict["credential_id"] = credential_id
        if label is not UNSET:
            field_dict["label"] = label
        if instance_type is not UNSET:
            field_dict["instance_type"] = instance_type
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
        if true_cost_per_minute_nanos is not UNSET:
            field_dict["true_cost_per_minute_nanos"] = true_cost_per_minute_nanos
        if assigned_org_id is not UNSET:
            field_dict["assigned_org_id"] = assigned_org_id
        if assigned_org_name is not UNSET:
            field_dict["assigned_org_name"] = assigned_org_name
        if status_fact is not UNSET:
            field_dict["status_fact"] = status_fact
        if org_machine is not UNSET:
            field_dict["org_machine"] = org_machine
        if acquisition is not UNSET:
            field_dict["acquisition"] = acquisition

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.fleet_org_machine import FleetOrgMachine
        from ..models.machine_cost import MachineCost
        from ..models.machine_drain import MachineDrain
        from ..models.machine_fault_read import MachineFaultRead
        from ..models.machine_isolation_read import MachineIsolationRead
        from ..models.machine_resources import MachineResources
        from ..models.org_ref import OrgRef
        from ..models.status_fact import StatusFact

        d = dict(src_dict)
        id = d.pop("id")

        created_at = datetime.datetime.fromisoformat(d.pop("created_at"))

        name = d.pop("name", UNSET)

        provider = d.pop("provider", UNSET)

        provider_machine_id = d.pop("provider_machine_id", UNSET)

        machine_type_code = d.pop("machine_type_code", UNSET)

        region = d.pop("region", UNSET)

        _state = d.pop("state", UNSET)
        state: FleetMachineRowState | Unset
        if isinstance(_state, Unset):
            state = UNSET
        else:
            state = FleetMachineRowState(_state)

        _liveness = d.pop("liveness", UNSET)
        liveness: FleetMachineRowLiveness | Unset
        if isinstance(_liveness, Unset):
            liveness = UNSET
        else:
            liveness = FleetMachineRowLiveness(_liveness)

        _tenancy = d.pop("tenancy", UNSET)
        tenancy: FleetMachineRowTenancy | Unset
        if isinstance(_tenancy, Unset):
            tenancy = UNSET
        else:
            tenancy = FleetMachineRowTenancy(_tenancy)

        _sandbox = d.pop("sandbox", UNSET)
        sandbox: FleetMachineRowSandbox | Unset
        if isinstance(_sandbox, Unset):
            sandbox = UNSET
        else:
            sandbox = FleetMachineRowSandbox(_sandbox)

        def _parse_dedicated_org(data: object) -> None | OrgRef | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                dedicated_org_type_0 = OrgRef.from_dict(data)

                return dedicated_org_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | OrgRef | Unset, data)

        dedicated_org = _parse_dedicated_org(d.pop("dedicated_org", UNSET))

        capacity = d.pop("capacity", UNSET)

        chats_served = d.pop("chats_served", UNSET)

        chats_stranded = d.pop("chats_stranded", UNSET)

        chats_asleep = d.pop("chats_asleep", UNSET)

        storage_gb = d.pop("storage_gb", UNSET)

        def _parse_heartbeat_at(data: object) -> datetime.datetime | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                heartbeat_at_type_0 = datetime.datetime.fromisoformat(data)

                return heartbeat_at_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None | Unset, data)

        heartbeat_at = _parse_heartbeat_at(d.pop("heartbeat_at", UNSET))

        daemon_version = d.pop("daemon_version", UNSET)

        def _parse_state_changed_at(data: object) -> datetime.datetime | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                state_changed_at_type_0 = datetime.datetime.fromisoformat(data)

                return state_changed_at_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None | Unset, data)

        state_changed_at = _parse_state_changed_at(d.pop("state_changed_at", UNSET))

        def _parse_drain(data: object) -> MachineDrain | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                drain_type_0 = MachineDrain.from_dict(data)

                return drain_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(MachineDrain | None | Unset, data)

        drain = _parse_drain(d.pop("drain", UNSET))

        def _parse_wake_requested_at(data: object) -> datetime.datetime | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                wake_requested_at_type_0 = datetime.datetime.fromisoformat(data)

                return wake_requested_at_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None | Unset, data)

        wake_requested_at = _parse_wake_requested_at(d.pop("wake_requested_at", UNSET))

        _cost = d.pop("cost", UNSET)
        cost: MachineCost | Unset
        if isinstance(_cost, Unset):
            cost = UNSET
        else:
            cost = MachineCost.from_dict(_cost)

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

        def _parse_isolation(data: object) -> MachineIsolationRead | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                isolation_type_0 = MachineIsolationRead.from_dict(data)

                return isolation_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(MachineIsolationRead | None | Unset, data)

        isolation = _parse_isolation(d.pop("isolation", UNSET))

        def _parse_fault(data: object) -> MachineFaultRead | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                fault_type_0 = MachineFaultRead.from_dict(data)

                return fault_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(MachineFaultRead | None | Unset, data)

        fault = _parse_fault(d.pop("fault", UNSET))

        _origin = d.pop("origin", UNSET)
        origin: FleetMachineRowOrigin | Unset
        if isinstance(_origin, Unset):
            origin = UNSET
        else:
            origin = FleetMachineRowOrigin(_origin)

        def _parse_credential_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        credential_id = _parse_credential_id(d.pop("credential_id", UNSET))

        label = d.pop("label", UNSET)

        instance_type = d.pop("instance_type", UNSET)

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
        status: FleetMachineRowStatus | Unset
        if isinstance(_status, Unset):
            status = UNSET
        else:
            status = FleetMachineRowStatus(_status)

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

        true_cost_per_minute_nanos = d.pop("true_cost_per_minute_nanos", UNSET)

        def _parse_assigned_org_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        assigned_org_id = _parse_assigned_org_id(d.pop("assigned_org_id", UNSET))

        assigned_org_name = d.pop("assigned_org_name", UNSET)

        def _parse_status_fact(data: object) -> None | StatusFact | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                status_fact_type_0 = StatusFact.from_dict(data)

                return status_fact_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | StatusFact | Unset, data)

        status_fact = _parse_status_fact(d.pop("status_fact", UNSET))

        def _parse_org_machine(data: object) -> FleetOrgMachine | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                org_machine_type_0 = FleetOrgMachine.from_dict(data)

                return org_machine_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(FleetOrgMachine | None | Unset, data)

        org_machine = _parse_org_machine(d.pop("org_machine", UNSET))

        def _parse_acquisition(data: object) -> FleetMachineRowAcquisitionType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                acquisition_type_0 = FleetMachineRowAcquisitionType0(data)

                return acquisition_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(FleetMachineRowAcquisitionType0 | None | Unset, data)

        acquisition = _parse_acquisition(d.pop("acquisition", UNSET))

        fleet_machine_row = cls(
            id=id,
            created_at=created_at,
            name=name,
            provider=provider,
            provider_machine_id=provider_machine_id,
            machine_type_code=machine_type_code,
            region=region,
            state=state,
            liveness=liveness,
            tenancy=tenancy,
            sandbox=sandbox,
            dedicated_org=dedicated_org,
            capacity=capacity,
            chats_served=chats_served,
            chats_stranded=chats_stranded,
            chats_asleep=chats_asleep,
            storage_gb=storage_gb,
            heartbeat_at=heartbeat_at,
            daemon_version=daemon_version,
            state_changed_at=state_changed_at,
            drain=drain,
            wake_requested_at=wake_requested_at,
            cost=cost,
            resources=resources,
            isolation=isolation,
            fault=fault,
            origin=origin,
            credential_id=credential_id,
            label=label,
            instance_type=instance_type,
            revoked_at=revoked_at,
            last_used_at=last_used_at,
            machine_id=machine_id,
            machine_name=machine_name,
            status=status,
            last_heartbeat_at=last_heartbeat_at,
            true_cost_per_minute_nanos=true_cost_per_minute_nanos,
            assigned_org_id=assigned_org_id,
            assigned_org_name=assigned_org_name,
            status_fact=status_fact,
            org_machine=org_machine,
            acquisition=acquisition,
        )

        fleet_machine_row.additional_properties = d
        return fleet_machine_row

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
