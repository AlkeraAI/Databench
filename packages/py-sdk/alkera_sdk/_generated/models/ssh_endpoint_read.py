from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.ssh_endpoint_read_auth_kind import SshEndpointReadAuthKind
from ..types import UNSET, Unset

T = TypeVar("T", bound="SshEndpointRead")


@_attrs_define
class SshEndpointRead:
    """Where an added machine is reached. Never the credential.

    Attributes:
        host (str):
        port (int):
        username (str):
        auth_kind (SshEndpointReadAuthKind):
        host_key_fingerprint (str):
        host_key_type (str | Unset):  Default: ''.
    """

    host: str
    port: int
    username: str
    auth_kind: SshEndpointReadAuthKind
    host_key_fingerprint: str
    host_key_type: str | Unset = ""
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        host = self.host

        port = self.port

        username = self.username

        auth_kind = self.auth_kind.value

        host_key_fingerprint = self.host_key_fingerprint

        host_key_type = self.host_key_type

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "host": host,
                "port": port,
                "username": username,
                "auth_kind": auth_kind,
                "host_key_fingerprint": host_key_fingerprint,
            }
        )
        if host_key_type is not UNSET:
            field_dict["host_key_type"] = host_key_type

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        host = d.pop("host")

        port = d.pop("port")

        username = d.pop("username")

        auth_kind = SshEndpointReadAuthKind(d.pop("auth_kind"))

        host_key_fingerprint = d.pop("host_key_fingerprint")

        host_key_type = d.pop("host_key_type", UNSET)

        ssh_endpoint_read = cls(
            host=host,
            port=port,
            username=username,
            auth_kind=auth_kind,
            host_key_fingerprint=host_key_fingerprint,
            host_key_type=host_key_type,
        )

        ssh_endpoint_read.additional_properties = d
        return ssh_endpoint_read

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
