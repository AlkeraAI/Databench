from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="LiveReportBody")


@_attrs_define
class LiveReportBody:
    """One node as the holder reports it: a stored state, or ``applied`` /
    ``superseded`` to say the difference the drive recorded is gone.

        Attributes:
            node_id (str):
            state (str):
            box_size (int | None | Unset):
            box_mtime (datetime.datetime | None | Unset):
            displaced (None | str | Unset):
    """

    node_id: str
    state: str
    box_size: int | None | Unset = UNSET
    box_mtime: datetime.datetime | None | Unset = UNSET
    displaced: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        node_id = self.node_id

        state = self.state

        box_size: int | None | Unset
        if isinstance(self.box_size, Unset):
            box_size = UNSET
        else:
            box_size = self.box_size

        box_mtime: None | str | Unset
        if isinstance(self.box_mtime, Unset):
            box_mtime = UNSET
        elif isinstance(self.box_mtime, datetime.datetime):
            box_mtime = self.box_mtime.isoformat()
        else:
            box_mtime = self.box_mtime

        displaced: None | str | Unset
        if isinstance(self.displaced, Unset):
            displaced = UNSET
        else:
            displaced = self.displaced

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "nodeId": node_id,
                "state": state,
            }
        )
        if box_size is not UNSET:
            field_dict["boxSize"] = box_size
        if box_mtime is not UNSET:
            field_dict["boxMtime"] = box_mtime
        if displaced is not UNSET:
            field_dict["displaced"] = displaced

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        node_id = d.pop("nodeId")

        state = d.pop("state")

        def _parse_box_size(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        box_size = _parse_box_size(d.pop("boxSize", UNSET))

        def _parse_box_mtime(data: object) -> datetime.datetime | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                box_mtime_type_0 = datetime.datetime.fromisoformat(data)

                return box_mtime_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None | Unset, data)

        box_mtime = _parse_box_mtime(d.pop("boxMtime", UNSET))

        def _parse_displaced(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        displaced = _parse_displaced(d.pop("displaced", UNSET))

        live_report_body = cls(
            node_id=node_id,
            state=state,
            box_size=box_size,
            box_mtime=box_mtime,
            displaced=displaced,
        )

        live_report_body.additional_properties = d
        return live_report_body

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
