from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import Any, TypeVar, cast
from uuid import UUID

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="AuditChainVerification")


@_attrs_define
class AuditChainVerification:
    """Result of verifying the org's tamper-evident audit hash chain.

    Attributes:
        ok (bool):
        checked (int):
        broken_event_id (None | Unset | UUID):
        broken_at (datetime.datetime | None | Unset):
        head_hash (None | str | Unset):
    """

    ok: bool
    checked: int
    broken_event_id: None | Unset | UUID = UNSET
    broken_at: datetime.datetime | None | Unset = UNSET
    head_hash: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        ok = self.ok

        checked = self.checked

        broken_event_id: None | str | Unset
        if isinstance(self.broken_event_id, Unset):
            broken_event_id = UNSET
        elif isinstance(self.broken_event_id, UUID):
            broken_event_id = str(self.broken_event_id)
        else:
            broken_event_id = self.broken_event_id

        broken_at: None | str | Unset
        if isinstance(self.broken_at, Unset):
            broken_at = UNSET
        elif isinstance(self.broken_at, datetime.datetime):
            broken_at = self.broken_at.isoformat()
        else:
            broken_at = self.broken_at

        head_hash: None | str | Unset
        if isinstance(self.head_hash, Unset):
            head_hash = UNSET
        else:
            head_hash = self.head_hash

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "ok": ok,
                "checked": checked,
            }
        )
        if broken_event_id is not UNSET:
            field_dict["broken_event_id"] = broken_event_id
        if broken_at is not UNSET:
            field_dict["broken_at"] = broken_at
        if head_hash is not UNSET:
            field_dict["head_hash"] = head_hash

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        ok = d.pop("ok")

        checked = d.pop("checked")

        def _parse_broken_event_id(data: object) -> None | Unset | UUID:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                broken_event_id_type_0 = UUID(data)

                return broken_event_id_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | Unset | UUID, data)

        broken_event_id = _parse_broken_event_id(d.pop("broken_event_id", UNSET))

        def _parse_broken_at(data: object) -> datetime.datetime | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                broken_at_type_0 = datetime.datetime.fromisoformat(data)

                return broken_at_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None | Unset, data)

        broken_at = _parse_broken_at(d.pop("broken_at", UNSET))

        def _parse_head_hash(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        head_hash = _parse_head_hash(d.pop("head_hash", UNSET))

        audit_chain_verification = cls(
            ok=ok,
            checked=checked,
            broken_event_id=broken_event_id,
            broken_at=broken_at,
            head_hash=head_hash,
        )

        audit_chain_verification.additional_properties = d
        return audit_chain_verification

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
