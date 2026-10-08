from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast
from uuid import UUID

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.machine_routing_entry_state import MachineRoutingEntryState
from ..types import UNSET, Unset

T = TypeVar("T", bound="MachineRoutingEntry")


@_attrs_define
class MachineRoutingEntry:
    """One chat bound to the machine, as the root process routes it: which
    org's process serves it and whether that process should be holding it.
    Ids and states only: what the chat says is read by that org's process,
    on its own credential.

        Attributes:
            chat_id (UUID):
            org_id (UUID):
            workspace_id (None | Unset | UUID):
            state (MachineRoutingEntryState | Unset):  Default: MachineRoutingEntryState.ASLEEP.
            pending_turn (bool | Unset):  Default: False.
            wake_requested (bool | Unset):  Default: False.
            end_seq (int | Unset):  Default: 0.
    """

    chat_id: UUID
    org_id: UUID
    workspace_id: None | Unset | UUID = UNSET
    state: MachineRoutingEntryState | Unset = MachineRoutingEntryState.ASLEEP
    pending_turn: bool | Unset = False
    wake_requested: bool | Unset = False
    end_seq: int | Unset = 0
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        chat_id = str(self.chat_id)

        org_id = str(self.org_id)

        workspace_id: None | str | Unset
        if isinstance(self.workspace_id, Unset):
            workspace_id = UNSET
        elif isinstance(self.workspace_id, UUID):
            workspace_id = str(self.workspace_id)
        else:
            workspace_id = self.workspace_id

        state: str | Unset = UNSET
        if not isinstance(self.state, Unset):
            state = self.state.value

        pending_turn = self.pending_turn

        wake_requested = self.wake_requested

        end_seq = self.end_seq

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "chat_id": chat_id,
                "org_id": org_id,
            }
        )
        if workspace_id is not UNSET:
            field_dict["workspace_id"] = workspace_id
        if state is not UNSET:
            field_dict["state"] = state
        if pending_turn is not UNSET:
            field_dict["pending_turn"] = pending_turn
        if wake_requested is not UNSET:
            field_dict["wake_requested"] = wake_requested
        if end_seq is not UNSET:
            field_dict["end_seq"] = end_seq

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        chat_id = UUID(d.pop("chat_id"))

        org_id = UUID(d.pop("org_id"))

        def _parse_workspace_id(data: object) -> None | Unset | UUID:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                workspace_id_type_0 = UUID(data)

                return workspace_id_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | Unset | UUID, data)

        workspace_id = _parse_workspace_id(d.pop("workspace_id", UNSET))

        _state = d.pop("state", UNSET)
        state: MachineRoutingEntryState | Unset
        if isinstance(_state, Unset):
            state = UNSET
        else:
            state = MachineRoutingEntryState(_state)

        pending_turn = d.pop("pending_turn", UNSET)

        wake_requested = d.pop("wake_requested", UNSET)

        end_seq = d.pop("end_seq", UNSET)

        machine_routing_entry = cls(
            chat_id=chat_id,
            org_id=org_id,
            workspace_id=workspace_id,
            state=state,
            pending_turn=pending_turn,
            wake_requested=wake_requested,
            end_seq=end_seq,
        )

        machine_routing_entry.additional_properties = d
        return machine_routing_entry

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
