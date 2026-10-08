from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Literal, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.machine_card_kind import MachineCardKind
from ..models.machine_card_state_type_0 import MachineCardStateType0
from ..models.machine_card_step_type_0 import MachineCardStepType0
from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.machine_fault_read import MachineFaultRead
    from ..models.machine_spec import MachineSpec
    from ..models.machine_wait_read import MachineWaitRead
    from ..models.status_fact import StatusFact


T = TypeVar("T", bound="MachineCard")


@_attrs_define
class MachineCard:
    """One machine as every surface draws it: an org machine, the shared
    machines, a person's own box, or a box a member registered for the org.

        Attributes:
            kind (MachineCardKind):
            name (str):
            state (Literal['shared'] | MachineCardStateType0):
            org_machine_id (None | str | Unset):
            spec (MachineSpec | None | Unset):
            step (MachineCardStepType0 | None | Unset):
            step_started_at (datetime.datetime | None | Unset):
            step_expected_seconds (int | None | Unset):
            stop_reason (str | Unset):  Default: ''.
            drain_stops_at (datetime.datetime | None | Unset):
            fault (MachineFaultRead | None | Unset):
            disk_full (bool | Unset):  Default: False.
            status (None | StatusFact | Unset):
            wait (MachineWaitRead | None | Unset):
    """

    kind: MachineCardKind
    name: str
    state: Literal["shared"] | MachineCardStateType0
    org_machine_id: None | str | Unset = UNSET
    spec: MachineSpec | None | Unset = UNSET
    step: MachineCardStepType0 | None | Unset = UNSET
    step_started_at: datetime.datetime | None | Unset = UNSET
    step_expected_seconds: int | None | Unset = UNSET
    stop_reason: str | Unset = ""
    drain_stops_at: datetime.datetime | None | Unset = UNSET
    fault: MachineFaultRead | None | Unset = UNSET
    disk_full: bool | Unset = False
    status: None | StatusFact | Unset = UNSET
    wait: MachineWaitRead | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.machine_fault_read import MachineFaultRead
        from ..models.machine_spec import MachineSpec
        from ..models.machine_wait_read import MachineWaitRead
        from ..models.status_fact import StatusFact

        kind = self.kind.value

        name = self.name

        state: Literal["shared"] | str
        if isinstance(self.state, MachineCardStateType0):
            state = self.state.value
        else:
            state = self.state

        org_machine_id: None | str | Unset
        if isinstance(self.org_machine_id, Unset):
            org_machine_id = UNSET
        else:
            org_machine_id = self.org_machine_id

        spec: dict[str, Any] | None | Unset
        if isinstance(self.spec, Unset):
            spec = UNSET
        elif isinstance(self.spec, MachineSpec):
            spec = self.spec.to_dict()
        else:
            spec = self.spec

        step: None | str | Unset
        if isinstance(self.step, Unset):
            step = UNSET
        elif isinstance(self.step, MachineCardStepType0):
            step = self.step.value
        else:
            step = self.step

        step_started_at: None | str | Unset
        if isinstance(self.step_started_at, Unset):
            step_started_at = UNSET
        elif isinstance(self.step_started_at, datetime.datetime):
            step_started_at = self.step_started_at.isoformat()
        else:
            step_started_at = self.step_started_at

        step_expected_seconds: int | None | Unset
        if isinstance(self.step_expected_seconds, Unset):
            step_expected_seconds = UNSET
        else:
            step_expected_seconds = self.step_expected_seconds

        stop_reason = self.stop_reason

        drain_stops_at: None | str | Unset
        if isinstance(self.drain_stops_at, Unset):
            drain_stops_at = UNSET
        elif isinstance(self.drain_stops_at, datetime.datetime):
            drain_stops_at = self.drain_stops_at.isoformat()
        else:
            drain_stops_at = self.drain_stops_at

        fault: dict[str, Any] | None | Unset
        if isinstance(self.fault, Unset):
            fault = UNSET
        elif isinstance(self.fault, MachineFaultRead):
            fault = self.fault.to_dict()
        else:
            fault = self.fault

        disk_full = self.disk_full

        status: dict[str, Any] | None | Unset
        if isinstance(self.status, Unset):
            status = UNSET
        elif isinstance(self.status, StatusFact):
            status = self.status.to_dict()
        else:
            status = self.status

        wait: dict[str, Any] | None | Unset
        if isinstance(self.wait, Unset):
            wait = UNSET
        elif isinstance(self.wait, MachineWaitRead):
            wait = self.wait.to_dict()
        else:
            wait = self.wait

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "kind": kind,
                "name": name,
                "state": state,
            }
        )
        if org_machine_id is not UNSET:
            field_dict["org_machine_id"] = org_machine_id
        if spec is not UNSET:
            field_dict["spec"] = spec
        if step is not UNSET:
            field_dict["step"] = step
        if step_started_at is not UNSET:
            field_dict["step_started_at"] = step_started_at
        if step_expected_seconds is not UNSET:
            field_dict["step_expected_seconds"] = step_expected_seconds
        if stop_reason is not UNSET:
            field_dict["stop_reason"] = stop_reason
        if drain_stops_at is not UNSET:
            field_dict["drain_stops_at"] = drain_stops_at
        if fault is not UNSET:
            field_dict["fault"] = fault
        if disk_full is not UNSET:
            field_dict["disk_full"] = disk_full
        if status is not UNSET:
            field_dict["status"] = status
        if wait is not UNSET:
            field_dict["wait"] = wait

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.machine_fault_read import MachineFaultRead
        from ..models.machine_spec import MachineSpec
        from ..models.machine_wait_read import MachineWaitRead
        from ..models.status_fact import StatusFact

        d = dict(src_dict)
        kind = MachineCardKind(d.pop("kind"))

        name = d.pop("name")

        def _parse_state(data: object) -> Literal["shared"] | MachineCardStateType0:
            try:
                if not isinstance(data, str):
                    raise TypeError()
                state_type_0 = MachineCardStateType0(data)

                return state_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            state_type_1 = cast(Literal["shared"], data)
            if state_type_1 != "shared":
                raise ValueError(f"state_type_1 must match const 'shared', got '{state_type_1}'")
            return state_type_1

        state = _parse_state(d.pop("state"))

        def _parse_org_machine_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        org_machine_id = _parse_org_machine_id(d.pop("org_machine_id", UNSET))

        def _parse_spec(data: object) -> MachineSpec | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                spec_type_0 = MachineSpec.from_dict(data)

                return spec_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(MachineSpec | None | Unset, data)

        spec = _parse_spec(d.pop("spec", UNSET))

        def _parse_step(data: object) -> MachineCardStepType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                step_type_0 = MachineCardStepType0(data)

                return step_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(MachineCardStepType0 | None | Unset, data)

        step = _parse_step(d.pop("step", UNSET))

        def _parse_step_started_at(data: object) -> datetime.datetime | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                step_started_at_type_0 = datetime.datetime.fromisoformat(data)

                return step_started_at_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None | Unset, data)

        step_started_at = _parse_step_started_at(d.pop("step_started_at", UNSET))

        def _parse_step_expected_seconds(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        step_expected_seconds = _parse_step_expected_seconds(d.pop("step_expected_seconds", UNSET))

        stop_reason = d.pop("stop_reason", UNSET)

        def _parse_drain_stops_at(data: object) -> datetime.datetime | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                drain_stops_at_type_0 = datetime.datetime.fromisoformat(data)

                return drain_stops_at_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None | Unset, data)

        drain_stops_at = _parse_drain_stops_at(d.pop("drain_stops_at", UNSET))

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

        disk_full = d.pop("disk_full", UNSET)

        def _parse_status(data: object) -> None | StatusFact | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                status_type_0 = StatusFact.from_dict(data)

                return status_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | StatusFact | Unset, data)

        status = _parse_status(d.pop("status", UNSET))

        def _parse_wait(data: object) -> MachineWaitRead | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                wait_type_0 = MachineWaitRead.from_dict(data)

                return wait_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(MachineWaitRead | None | Unset, data)

        wait = _parse_wait(d.pop("wait", UNSET))

        machine_card = cls(
            kind=kind,
            name=name,
            state=state,
            org_machine_id=org_machine_id,
            spec=spec,
            step=step,
            step_started_at=step_started_at,
            step_expected_seconds=step_expected_seconds,
            stop_reason=stop_reason,
            drain_stops_at=drain_stops_at,
            fault=fault,
            disk_full=disk_full,
            status=status,
            wait=wait,
        )

        machine_card.additional_properties = d
        return machine_card

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
