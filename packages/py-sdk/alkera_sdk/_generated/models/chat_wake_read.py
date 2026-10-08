from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.chat_wake_read_outcome import ChatWakeReadOutcome
from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.machine_unavailable_read import MachineUnavailableRead


T = TypeVar("T", bound="ChatWakeRead")


@_attrs_define
class ChatWakeRead:
    """What opening a chat did to wake it. ``waking``: a wake was asked of the
    chat's box or its machine. ``awake``: nothing slept, so nothing was asked.
    ``throttled``: the chat was opened moments ago and that open's wake stands.
    ``machine_unavailable``: the workspace's own machine is gone, nothing was
    woken, and ``machine_unavailable`` says what was lost and where the opener
    may wake it instead. A refused start is not an outcome here; it is the
    compute refusal's own ``402`` / ``429``.

        Attributes:
            outcome (ChatWakeReadOutcome):
            machine_unavailable (MachineUnavailableRead | None | Unset):
    """

    outcome: ChatWakeReadOutcome
    machine_unavailable: MachineUnavailableRead | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.machine_unavailable_read import MachineUnavailableRead

        outcome = self.outcome.value

        machine_unavailable: dict[str, Any] | None | Unset
        if isinstance(self.machine_unavailable, Unset):
            machine_unavailable = UNSET
        elif isinstance(self.machine_unavailable, MachineUnavailableRead):
            machine_unavailable = self.machine_unavailable.to_dict()
        else:
            machine_unavailable = self.machine_unavailable

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "outcome": outcome,
            }
        )
        if machine_unavailable is not UNSET:
            field_dict["machine_unavailable"] = machine_unavailable

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.machine_unavailable_read import MachineUnavailableRead

        d = dict(src_dict)
        outcome = ChatWakeReadOutcome(d.pop("outcome"))

        def _parse_machine_unavailable(data: object) -> MachineUnavailableRead | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                machine_unavailable_type_0 = MachineUnavailableRead.from_dict(data)

                return machine_unavailable_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(MachineUnavailableRead | None | Unset, data)

        machine_unavailable = _parse_machine_unavailable(d.pop("machine_unavailable", UNSET))

        chat_wake_read = cls(
            outcome=outcome,
            machine_unavailable=machine_unavailable,
        )

        chat_wake_read.additional_properties = d
        return chat_wake_read

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
