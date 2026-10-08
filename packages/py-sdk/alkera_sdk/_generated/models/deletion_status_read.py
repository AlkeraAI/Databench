from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import Any, TypeVar, cast
from uuid import UUID

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.deletion_status_read_source import DeletionStatusReadSource
from ..models.deletion_status_read_status import DeletionStatusReadStatus
from ..types import UNSET, Unset

T = TypeVar("T", bound="DeletionStatusRead")


@_attrs_define
class DeletionStatusRead:
    """
    Attributes:
        id (UUID):
        status (DeletionStatusReadStatus):
        source (DeletionStatusReadSource):
        requested_at (datetime.datetime):
        purge_after (datetime.datetime):
        cancelled_at (datetime.datetime | None | Unset):
        completed_at (datetime.datetime | None | Unset):
        blocked_reason (None | str | Unset):
    """

    id: UUID
    status: DeletionStatusReadStatus
    source: DeletionStatusReadSource
    requested_at: datetime.datetime
    purge_after: datetime.datetime
    cancelled_at: datetime.datetime | None | Unset = UNSET
    completed_at: datetime.datetime | None | Unset = UNSET
    blocked_reason: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        id = str(self.id)

        status = self.status.value

        source = self.source.value

        requested_at = self.requested_at.isoformat()

        purge_after = self.purge_after.isoformat()

        cancelled_at: None | str | Unset
        if isinstance(self.cancelled_at, Unset):
            cancelled_at = UNSET
        elif isinstance(self.cancelled_at, datetime.datetime):
            cancelled_at = self.cancelled_at.isoformat()
        else:
            cancelled_at = self.cancelled_at

        completed_at: None | str | Unset
        if isinstance(self.completed_at, Unset):
            completed_at = UNSET
        elif isinstance(self.completed_at, datetime.datetime):
            completed_at = self.completed_at.isoformat()
        else:
            completed_at = self.completed_at

        blocked_reason: None | str | Unset
        if isinstance(self.blocked_reason, Unset):
            blocked_reason = UNSET
        else:
            blocked_reason = self.blocked_reason

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "status": status,
                "source": source,
                "requested_at": requested_at,
                "purge_after": purge_after,
            }
        )
        if cancelled_at is not UNSET:
            field_dict["cancelled_at"] = cancelled_at
        if completed_at is not UNSET:
            field_dict["completed_at"] = completed_at
        if blocked_reason is not UNSET:
            field_dict["blocked_reason"] = blocked_reason

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        id = UUID(d.pop("id"))

        status = DeletionStatusReadStatus(d.pop("status"))

        source = DeletionStatusReadSource(d.pop("source"))

        requested_at = datetime.datetime.fromisoformat(d.pop("requested_at"))

        purge_after = datetime.datetime.fromisoformat(d.pop("purge_after"))

        def _parse_cancelled_at(data: object) -> datetime.datetime | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                cancelled_at_type_0 = datetime.datetime.fromisoformat(data)

                return cancelled_at_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None | Unset, data)

        cancelled_at = _parse_cancelled_at(d.pop("cancelled_at", UNSET))

        def _parse_completed_at(data: object) -> datetime.datetime | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                completed_at_type_0 = datetime.datetime.fromisoformat(data)

                return completed_at_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None | Unset, data)

        completed_at = _parse_completed_at(d.pop("completed_at", UNSET))

        def _parse_blocked_reason(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        blocked_reason = _parse_blocked_reason(d.pop("blocked_reason", UNSET))

        deletion_status_read = cls(
            id=id,
            status=status,
            source=source,
            requested_at=requested_at,
            purge_after=purge_after,
            cancelled_at=cancelled_at,
            completed_at=completed_at,
            blocked_reason=blocked_reason,
        )

        deletion_status_read.additional_properties = d
        return deletion_status_read

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
