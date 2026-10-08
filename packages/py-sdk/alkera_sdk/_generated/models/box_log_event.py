from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.box_log_event_fields import BoxLogEventFields


T = TypeVar("T", bound="BoxLogEvent")


@_attrs_define
class BoxLogEvent:
    """One event as the box's shipper formats it.

    Attributes:
        event (str):
        timestamp (None | str | Unset):
        level (str | Unset):  Default: 'info'.
        fields (BoxLogEventFields | Unset):
    """

    event: str
    timestamp: None | str | Unset = UNSET
    level: str | Unset = "info"
    fields: BoxLogEventFields | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        event = self.event

        timestamp: None | str | Unset
        if isinstance(self.timestamp, Unset):
            timestamp = UNSET
        else:
            timestamp = self.timestamp

        level = self.level

        fields: dict[str, Any] | Unset = UNSET
        if not isinstance(self.fields, Unset):
            fields = self.fields.to_dict()

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "event": event,
            }
        )
        if timestamp is not UNSET:
            field_dict["timestamp"] = timestamp
        if level is not UNSET:
            field_dict["level"] = level
        if fields is not UNSET:
            field_dict["fields"] = fields

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.box_log_event_fields import BoxLogEventFields

        d = dict(src_dict)
        event = d.pop("event")

        def _parse_timestamp(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        timestamp = _parse_timestamp(d.pop("timestamp", UNSET))

        level = d.pop("level", UNSET)

        _fields = d.pop("fields", UNSET)
        fields: BoxLogEventFields | Unset
        if isinstance(_fields, Unset):
            fields = UNSET
        else:
            fields = BoxLogEventFields.from_dict(_fields)

        box_log_event = cls(
            event=event,
            timestamp=timestamp,
            level=level,
            fields=fields,
        )

        box_log_event.additional_properties = d
        return box_log_event

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
