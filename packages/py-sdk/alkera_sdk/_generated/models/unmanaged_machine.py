from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="UnmanagedMachine")


@_attrs_define
class UnmanagedMachine:
    """A machine a provider bills this account for that no allocation row owns:
    started by hand, or left behind. Read-only; the console never acts on it.

        Attributes:
            provider (str):
            provider_machine_id (str):
            name (str | Unset):  Default: ''.
            phase (str | Unset):  Default: ''.
            raw_status (str | Unset):  Default: ''.
            created_at (datetime.datetime | None | Unset):
    """

    provider: str
    provider_machine_id: str
    name: str | Unset = ""
    phase: str | Unset = ""
    raw_status: str | Unset = ""
    created_at: datetime.datetime | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        provider = self.provider

        provider_machine_id = self.provider_machine_id

        name = self.name

        phase = self.phase

        raw_status = self.raw_status

        created_at: None | str | Unset
        if isinstance(self.created_at, Unset):
            created_at = UNSET
        elif isinstance(self.created_at, datetime.datetime):
            created_at = self.created_at.isoformat()
        else:
            created_at = self.created_at

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "provider": provider,
                "provider_machine_id": provider_machine_id,
            }
        )
        if name is not UNSET:
            field_dict["name"] = name
        if phase is not UNSET:
            field_dict["phase"] = phase
        if raw_status is not UNSET:
            field_dict["raw_status"] = raw_status
        if created_at is not UNSET:
            field_dict["created_at"] = created_at

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        provider = d.pop("provider")

        provider_machine_id = d.pop("provider_machine_id")

        name = d.pop("name", UNSET)

        phase = d.pop("phase", UNSET)

        raw_status = d.pop("raw_status", UNSET)

        def _parse_created_at(data: object) -> datetime.datetime | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                created_at_type_0 = datetime.datetime.fromisoformat(data)

                return created_at_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None | Unset, data)

        created_at = _parse_created_at(d.pop("created_at", UNSET))

        unmanaged_machine = cls(
            provider=provider,
            provider_machine_id=provider_machine_id,
            name=name,
            phase=phase,
            raw_status=raw_status,
            created_at=created_at,
        )

        unmanaged_machine.additional_properties = d
        return unmanaged_machine

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
