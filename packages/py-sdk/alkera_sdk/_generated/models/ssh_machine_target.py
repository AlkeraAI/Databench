from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.ssh_machine_target_auth_kind import SshMachineTargetAuthKind
from ..types import UNSET, Unset

T = TypeVar("T", bound="SshMachineTarget")


@_attrs_define
class SshMachineTarget:
    """A host and how to sign in to it.

    Attributes:
        host (str):
        username (str):
        auth_kind (SshMachineTargetAuthKind):
        port (int | Unset):  Default: 22.
        password (None | str | Unset):
        private_key (None | str | Unset):
        passphrase (None | str | Unset):
    """

    host: str
    username: str
    auth_kind: SshMachineTargetAuthKind
    port: int | Unset = 22
    password: None | str | Unset = UNSET
    private_key: None | str | Unset = UNSET
    passphrase: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        host = self.host

        username = self.username

        auth_kind = self.auth_kind.value

        port = self.port

        password: None | str | Unset
        if isinstance(self.password, Unset):
            password = UNSET
        else:
            password = self.password

        private_key: None | str | Unset
        if isinstance(self.private_key, Unset):
            private_key = UNSET
        else:
            private_key = self.private_key

        passphrase: None | str | Unset
        if isinstance(self.passphrase, Unset):
            passphrase = UNSET
        else:
            passphrase = self.passphrase

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "host": host,
                "username": username,
                "auth_kind": auth_kind,
            }
        )
        if port is not UNSET:
            field_dict["port"] = port
        if password is not UNSET:
            field_dict["password"] = password
        if private_key is not UNSET:
            field_dict["private_key"] = private_key
        if passphrase is not UNSET:
            field_dict["passphrase"] = passphrase

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        host = d.pop("host")

        username = d.pop("username")

        auth_kind = SshMachineTargetAuthKind(d.pop("auth_kind"))

        port = d.pop("port", UNSET)

        def _parse_password(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        password = _parse_password(d.pop("password", UNSET))

        def _parse_private_key(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        private_key = _parse_private_key(d.pop("private_key", UNSET))

        def _parse_passphrase(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        passphrase = _parse_passphrase(d.pop("passphrase", UNSET))

        ssh_machine_target = cls(
            host=host,
            username=username,
            auth_kind=auth_kind,
            port=port,
            password=password,
            private_key=private_key,
            passphrase=passphrase,
        )

        ssh_machine_target.additional_properties = d
        return ssh_machine_target

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
