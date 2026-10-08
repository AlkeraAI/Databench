from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.leased_named_secrets import LeasedNamedSecrets


T = TypeVar("T", bound="TeamConnectionCredentialLease")


@_attrs_define
class TeamConnectionCredentialLease:
    """A short-lived lease of a shared credential bundle.

    The daemon holds it in memory only and re-leases on expiry; the backend
    refuses the next lease when the connection is disabled, flipped off shared
    mode, or deleted. Every lease is audited.

        Attributes:
            secret (str): Primary shared credential; empty when the bundle contains only named values.
            credential_version (int):
            expires_at (datetime.datetime):
            named_secrets (LeasedNamedSecrets | Unset):
    """

    secret: str
    credential_version: int
    expires_at: datetime.datetime
    named_secrets: LeasedNamedSecrets | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        secret = self.secret

        credential_version = self.credential_version

        expires_at = self.expires_at.isoformat()

        named_secrets: dict[str, Any] | Unset = UNSET
        if not isinstance(self.named_secrets, Unset):
            named_secrets = self.named_secrets.to_dict()

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "secret": secret,
                "credential_version": credential_version,
                "expires_at": expires_at,
            }
        )
        if named_secrets is not UNSET:
            field_dict["named_secrets"] = named_secrets

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.leased_named_secrets import LeasedNamedSecrets

        d = dict(src_dict)
        secret = d.pop("secret")

        credential_version = d.pop("credential_version")

        expires_at = datetime.datetime.fromisoformat(d.pop("expires_at"))

        _named_secrets = d.pop("named_secrets", UNSET)
        named_secrets: LeasedNamedSecrets | Unset
        if isinstance(_named_secrets, Unset):
            named_secrets = UNSET
        else:
            named_secrets = LeasedNamedSecrets.from_dict(_named_secrets)

        team_connection_credential_lease = cls(
            secret=secret,
            credential_version=credential_version,
            expires_at=expires_at,
            named_secrets=named_secrets,
        )

        team_connection_credential_lease.additional_properties = d
        return team_connection_credential_lease

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
