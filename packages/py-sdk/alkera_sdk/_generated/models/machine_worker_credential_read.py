from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import Any, TypeVar
from uuid import UUID

from attrs import define as _attrs_define
from attrs import field as _attrs_field

T = TypeVar("T", bound="MachineWorkerCredentialRead")


@_attrs_define
class MachineWorkerCredentialRead:
    """A freshly minted org-bound worker credential. ``token`` is the bearer
    the org's process presents; it reaches ``org_id`` and nothing else, and
    stops working at ``expires_at`` or the moment the machine credential that
    minted it is revoked, whichever is first.

        Attributes:
            token (str):
            org_id (UUID):
            expires_at (datetime.datetime):
            expires_in (int):
    """

    token: str
    org_id: UUID
    expires_at: datetime.datetime
    expires_in: int
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        token = self.token

        org_id = str(self.org_id)

        expires_at = self.expires_at.isoformat()

        expires_in = self.expires_in

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "token": token,
                "org_id": org_id,
                "expires_at": expires_at,
                "expires_in": expires_in,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        token = d.pop("token")

        org_id = UUID(d.pop("org_id"))

        expires_at = datetime.datetime.fromisoformat(d.pop("expires_at"))

        expires_in = d.pop("expires_in")

        machine_worker_credential_read = cls(
            token=token,
            org_id=org_id,
            expires_at=expires_at,
            expires_in=expires_in,
        )

        machine_worker_credential_read.additional_properties = d
        return machine_worker_credential_read

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
