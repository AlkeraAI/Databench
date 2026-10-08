from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.machine_state_read_status import MachineStateReadStatus
from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.status_fact import StatusFact


T = TypeVar("T", bound="MachineStateRead")


@_attrs_define
class MachineStateRead:
    """What the machine banner shows for the caller's org: the machine its next
    chat would be placed on and that machine's reachability, ``pool`` when a
    shared pool box would take it (no id or name: which box is placement's
    choice at create time), or ``none`` when nothing would.

    ``reason`` is the machine's own account of a state that is not ready — the
    text a failed provision or a refused start recorded. Without it the banner
    can say only that something is wrong, which leaves the reader with nothing
    to act on and an admin with nothing to look for. It is never a credential
    and never a price: it is the same words the ``compute_machine.changed``
    frame carries.

        Attributes:
            machine_id (None | str | Unset):
            status (MachineStateReadStatus | Unset):  Default: MachineStateReadStatus.NONE.
            name (str | Unset):  Default: ''.
            reason (str | Unset):  Default: ''.
            last_heartbeat_at (datetime.datetime | None | Unset):
            status_fact (None | StatusFact | Unset):
    """

    machine_id: None | str | Unset = UNSET
    status: MachineStateReadStatus | Unset = MachineStateReadStatus.NONE
    name: str | Unset = ""
    reason: str | Unset = ""
    last_heartbeat_at: datetime.datetime | None | Unset = UNSET
    status_fact: None | StatusFact | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.status_fact import StatusFact

        machine_id: None | str | Unset
        if isinstance(self.machine_id, Unset):
            machine_id = UNSET
        else:
            machine_id = self.machine_id

        status: str | Unset = UNSET
        if not isinstance(self.status, Unset):
            status = self.status.value

        name = self.name

        reason = self.reason

        last_heartbeat_at: None | str | Unset
        if isinstance(self.last_heartbeat_at, Unset):
            last_heartbeat_at = UNSET
        elif isinstance(self.last_heartbeat_at, datetime.datetime):
            last_heartbeat_at = self.last_heartbeat_at.isoformat()
        else:
            last_heartbeat_at = self.last_heartbeat_at

        status_fact: dict[str, Any] | None | Unset
        if isinstance(self.status_fact, Unset):
            status_fact = UNSET
        elif isinstance(self.status_fact, StatusFact):
            status_fact = self.status_fact.to_dict()
        else:
            status_fact = self.status_fact

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if machine_id is not UNSET:
            field_dict["machine_id"] = machine_id
        if status is not UNSET:
            field_dict["status"] = status
        if name is not UNSET:
            field_dict["name"] = name
        if reason is not UNSET:
            field_dict["reason"] = reason
        if last_heartbeat_at is not UNSET:
            field_dict["last_heartbeat_at"] = last_heartbeat_at
        if status_fact is not UNSET:
            field_dict["status_fact"] = status_fact

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.status_fact import StatusFact

        d = dict(src_dict)

        def _parse_machine_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        machine_id = _parse_machine_id(d.pop("machine_id", UNSET))

        _status = d.pop("status", UNSET)
        status: MachineStateReadStatus | Unset
        if isinstance(_status, Unset):
            status = UNSET
        else:
            status = MachineStateReadStatus(_status)

        name = d.pop("name", UNSET)

        reason = d.pop("reason", UNSET)

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

        machine_state_read = cls(
            machine_id=machine_id,
            status=status,
            name=name,
            reason=reason,
            last_heartbeat_at=last_heartbeat_at,
            status_fact=status_fact,
        )

        machine_state_read.additional_properties = d
        return machine_state_read

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
