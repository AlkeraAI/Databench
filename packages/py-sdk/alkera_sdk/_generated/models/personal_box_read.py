from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="PersonalBoxRead")


@_attrs_define
class PersonalBoxRead:
    """One box on the caller's own hardware, as its person sees it: what it is
    called, the org it serves them in, when it was set up and last spoke, and
    whether its standing was taken away. ``id`` is the credential's id, the
    one the revoke route names. Never the secret.

        Attributes:
            id (str):
            label (str):
            org_id (str):
            created_at (datetime.datetime):
            machine_id (None | str | Unset):
            last_used_at (datetime.datetime | None | Unset):
            revoked_at (datetime.datetime | None | Unset):
    """

    id: str
    label: str
    org_id: str
    created_at: datetime.datetime
    machine_id: None | str | Unset = UNSET
    last_used_at: datetime.datetime | None | Unset = UNSET
    revoked_at: datetime.datetime | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        id = self.id

        label = self.label

        org_id = self.org_id

        created_at = self.created_at.isoformat()

        machine_id: None | str | Unset
        if isinstance(self.machine_id, Unset):
            machine_id = UNSET
        else:
            machine_id = self.machine_id

        last_used_at: None | str | Unset
        if isinstance(self.last_used_at, Unset):
            last_used_at = UNSET
        elif isinstance(self.last_used_at, datetime.datetime):
            last_used_at = self.last_used_at.isoformat()
        else:
            last_used_at = self.last_used_at

        revoked_at: None | str | Unset
        if isinstance(self.revoked_at, Unset):
            revoked_at = UNSET
        elif isinstance(self.revoked_at, datetime.datetime):
            revoked_at = self.revoked_at.isoformat()
        else:
            revoked_at = self.revoked_at

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "label": label,
                "org_id": org_id,
                "created_at": created_at,
            }
        )
        if machine_id is not UNSET:
            field_dict["machine_id"] = machine_id
        if last_used_at is not UNSET:
            field_dict["last_used_at"] = last_used_at
        if revoked_at is not UNSET:
            field_dict["revoked_at"] = revoked_at

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        id = d.pop("id")

        label = d.pop("label")

        org_id = d.pop("org_id")

        created_at = datetime.datetime.fromisoformat(d.pop("created_at"))

        def _parse_machine_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        machine_id = _parse_machine_id(d.pop("machine_id", UNSET))

        def _parse_last_used_at(data: object) -> datetime.datetime | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                last_used_at_type_0 = datetime.datetime.fromisoformat(data)

                return last_used_at_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None | Unset, data)

        last_used_at = _parse_last_used_at(d.pop("last_used_at", UNSET))

        def _parse_revoked_at(data: object) -> datetime.datetime | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                revoked_at_type_0 = datetime.datetime.fromisoformat(data)

                return revoked_at_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None | Unset, data)

        revoked_at = _parse_revoked_at(d.pop("revoked_at", UNSET))

        personal_box_read = cls(
            id=id,
            label=label,
            org_id=org_id,
            created_at=created_at,
            machine_id=machine_id,
            last_used_at=last_used_at,
            revoked_at=revoked_at,
        )

        personal_box_read.additional_properties = d
        return personal_box_read

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
