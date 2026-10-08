from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.event_actor import EventActor


T = TypeVar("T", bound="MachineEvent")


@_attrs_define
class MachineEvent:
    """
    Attributes:
        at (datetime.datetime):
        from_state (str):
        to_state (str):
        actor (EventActor):
        reason (str | Unset):  Default: ''.
    """

    at: datetime.datetime
    from_state: str
    to_state: str
    actor: EventActor
    reason: str | Unset = ""
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        at = self.at.isoformat()

        from_state = self.from_state

        to_state = self.to_state

        actor = self.actor.to_dict()

        reason = self.reason

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "at": at,
                "from_state": from_state,
                "to_state": to_state,
                "actor": actor,
            }
        )
        if reason is not UNSET:
            field_dict["reason"] = reason

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.event_actor import EventActor

        d = dict(src_dict)
        at = datetime.datetime.fromisoformat(d.pop("at"))

        from_state = d.pop("from_state")

        to_state = d.pop("to_state")

        actor = EventActor.from_dict(d.pop("actor"))

        reason = d.pop("reason", UNSET)

        machine_event = cls(
            at=at,
            from_state=from_state,
            to_state=to_state,
            actor=actor,
            reason=reason,
        )

        machine_event.additional_properties = d
        return machine_event

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
