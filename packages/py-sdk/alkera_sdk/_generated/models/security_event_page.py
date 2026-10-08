from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast
from uuid import UUID

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.security_event_read import SecurityEventRead


T = TypeVar("T", bound="SecurityEventPage")


@_attrs_define
class SecurityEventPage:
    """
    Attributes:
        events (list[SecurityEventRead]):
        next_before (datetime.datetime | None):
        next_before_id (None | Unset | UUID):
    """

    events: list[SecurityEventRead]
    next_before: datetime.datetime | None
    next_before_id: None | Unset | UUID = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        events = []
        for events_item_data in self.events:
            events_item = events_item_data.to_dict()
            events.append(events_item)

        next_before: None | str
        if isinstance(self.next_before, datetime.datetime):
            next_before = self.next_before.isoformat()
        else:
            next_before = self.next_before

        next_before_id: None | str | Unset
        if isinstance(self.next_before_id, Unset):
            next_before_id = UNSET
        elif isinstance(self.next_before_id, UUID):
            next_before_id = str(self.next_before_id)
        else:
            next_before_id = self.next_before_id

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "events": events,
                "next_before": next_before,
            }
        )
        if next_before_id is not UNSET:
            field_dict["next_before_id"] = next_before_id

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.security_event_read import SecurityEventRead

        d = dict(src_dict)
        events = []
        _events = d.pop("events")
        for events_item_data in _events:
            events_item = SecurityEventRead.from_dict(events_item_data)

            events.append(events_item)

        def _parse_next_before(data: object) -> datetime.datetime | None:
            if data is None:
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                next_before_type_0 = datetime.datetime.fromisoformat(data)

                return next_before_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None, data)

        next_before = _parse_next_before(d.pop("next_before"))

        def _parse_next_before_id(data: object) -> None | Unset | UUID:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                next_before_id_type_0 = UUID(data)

                return next_before_id_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | Unset | UUID, data)

        next_before_id = _parse_next_before_id(d.pop("next_before_id", UNSET))

        security_event_page = cls(
            events=events,
            next_before=next_before,
            next_before_id=next_before_id,
        )

        security_event_page.additional_properties = d
        return security_event_page

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
