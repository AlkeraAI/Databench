from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.fetched_named_secrets import FetchedNamedSecrets


T = TypeVar("T", bound="TeamConnectionCredentialResponse")


@_attrs_define
class TeamConnectionCredentialResponse:
    """The decrypted shared credential bundle for an entitled member.

    Each value is written to its own chmod-600 file locally. Every fetch is
    audited; ``credential_version`` keys the bundle's local copy.

        Attributes:
            secret (str): Primary shared credential; empty when the bundle contains only named values.
            credential_version (int):
            named_secrets (FetchedNamedSecrets | Unset):
    """

    secret: str
    credential_version: int
    named_secrets: FetchedNamedSecrets | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        secret = self.secret

        credential_version = self.credential_version

        named_secrets: dict[str, Any] | Unset = UNSET
        if not isinstance(self.named_secrets, Unset):
            named_secrets = self.named_secrets.to_dict()

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "secret": secret,
                "credential_version": credential_version,
            }
        )
        if named_secrets is not UNSET:
            field_dict["named_secrets"] = named_secrets

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.fetched_named_secrets import FetchedNamedSecrets

        d = dict(src_dict)
        secret = d.pop("secret")

        credential_version = d.pop("credential_version")

        _named_secrets = d.pop("named_secrets", UNSET)
        named_secrets: FetchedNamedSecrets | Unset
        if isinstance(_named_secrets, Unset):
            named_secrets = UNSET
        else:
            named_secrets = FetchedNamedSecrets.from_dict(_named_secrets)

        team_connection_credential_response = cls(
            secret=secret,
            credential_version=credential_version,
            named_secrets=named_secrets,
        )

        team_connection_credential_response.additional_properties = d
        return team_connection_credential_response

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
