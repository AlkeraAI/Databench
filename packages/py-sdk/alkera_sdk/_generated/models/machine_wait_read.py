from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="MachineWaitRead")


@_attrs_define
class MachineWaitRead:
    """Why a machine waits for hardware and when the server asks again, as
    the reconcile will act on it.

        Attributes:
            reason (str):
            gives_up_at (datetime.datetime):
            next_try_at (datetime.datetime | None | Unset):
    """

    reason: str
    gives_up_at: datetime.datetime
    next_try_at: datetime.datetime | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        reason = self.reason

        gives_up_at = self.gives_up_at.isoformat()

        next_try_at: None | str | Unset
        if isinstance(self.next_try_at, Unset):
            next_try_at = UNSET
        elif isinstance(self.next_try_at, datetime.datetime):
            next_try_at = self.next_try_at.isoformat()
        else:
            next_try_at = self.next_try_at

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "reason": reason,
                "gives_up_at": gives_up_at,
            }
        )
        if next_try_at is not UNSET:
            field_dict["next_try_at"] = next_try_at

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        reason = d.pop("reason")

        gives_up_at = datetime.datetime.fromisoformat(d.pop("gives_up_at"))

        def _parse_next_try_at(data: object) -> datetime.datetime | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                next_try_at_type_0 = datetime.datetime.fromisoformat(data)

                return next_try_at_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None | Unset, data)

        next_try_at = _parse_next_try_at(d.pop("next_try_at", UNSET))

        machine_wait_read = cls(
            reason=reason,
            gives_up_at=gives_up_at,
            next_try_at=next_try_at,
        )

        machine_wait_read.additional_properties = d
        return machine_wait_read

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
