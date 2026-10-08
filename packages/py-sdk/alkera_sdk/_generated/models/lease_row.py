from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="LeaseRow")


@_attrs_define
class LeaseRow:
    """One of my leases, as ``alkera files mounts`` lists them.

    Attributes:
        node_id (str):
        epoch (int):
        machine (str):
        purpose (str):
        since (datetime.datetime):
        expires_at (datetime.datetime):
        last_sync_at (datetime.datetime | None | Unset):
    """

    node_id: str
    epoch: int
    machine: str
    purpose: str
    since: datetime.datetime
    expires_at: datetime.datetime
    last_sync_at: datetime.datetime | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        node_id = self.node_id

        epoch = self.epoch

        machine = self.machine

        purpose = self.purpose

        since = self.since.isoformat()

        expires_at = self.expires_at.isoformat()

        last_sync_at: None | str | Unset
        if isinstance(self.last_sync_at, Unset):
            last_sync_at = UNSET
        elif isinstance(self.last_sync_at, datetime.datetime):
            last_sync_at = self.last_sync_at.isoformat()
        else:
            last_sync_at = self.last_sync_at

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "nodeId": node_id,
                "epoch": epoch,
                "machine": machine,
                "purpose": purpose,
                "since": since,
                "expiresAt": expires_at,
            }
        )
        if last_sync_at is not UNSET:
            field_dict["lastSyncAt"] = last_sync_at

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        node_id = d.pop("nodeId")

        epoch = d.pop("epoch")

        machine = d.pop("machine")

        purpose = d.pop("purpose")

        since = datetime.datetime.fromisoformat(d.pop("since"))

        expires_at = datetime.datetime.fromisoformat(d.pop("expiresAt"))

        def _parse_last_sync_at(data: object) -> datetime.datetime | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                last_sync_at_type_0 = datetime.datetime.fromisoformat(data)

                return last_sync_at_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None | Unset, data)

        last_sync_at = _parse_last_sync_at(d.pop("lastSyncAt", UNSET))

        lease_row = cls(
            node_id=node_id,
            epoch=epoch,
            machine=machine,
            purpose=purpose,
            since=since,
            expires_at=expires_at,
            last_sync_at=last_sync_at,
        )

        lease_row.additional_properties = d
        return lease_row

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
