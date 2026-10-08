from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.kernel_events_batch_events_item import KernelEventsBatchEventsItem


T = TypeVar("T", bound="KernelEventsBatch")


@_attrs_define
class KernelEventsBatch:
    """Box -> backend: a batch of a kernel's events, in sequence order.

    Attributes:
        kernel_id (str):
        state (None | str | Unset):
        events (list[KernelEventsBatchEventsItem] | Unset):
    """

    kernel_id: str
    state: None | str | Unset = UNSET
    events: list[KernelEventsBatchEventsItem] | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        kernel_id = self.kernel_id

        state: None | str | Unset
        if isinstance(self.state, Unset):
            state = UNSET
        else:
            state = self.state

        events: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.events, Unset):
            events = []
            for events_item_data in self.events:
                events_item = events_item_data.to_dict()
                events.append(events_item)

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "kernel_id": kernel_id,
            }
        )
        if state is not UNSET:
            field_dict["state"] = state
        if events is not UNSET:
            field_dict["events"] = events

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.kernel_events_batch_events_item import (
            KernelEventsBatchEventsItem,
        )

        d = dict(src_dict)
        kernel_id = d.pop("kernel_id")

        def _parse_state(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        state = _parse_state(d.pop("state", UNSET))

        _events = d.pop("events", UNSET)
        events: list[KernelEventsBatchEventsItem] | Unset = UNSET
        if _events is not UNSET:
            events = []
            for events_item_data in _events:
                events_item = KernelEventsBatchEventsItem.from_dict(events_item_data)

                events.append(events_item)

        kernel_events_batch = cls(
            kernel_id=kernel_id,
            state=state,
            events=events,
        )

        return kernel_events_batch
