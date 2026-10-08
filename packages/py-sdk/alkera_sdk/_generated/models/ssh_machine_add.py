from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.ssh_machine_add_auth_kind import SshMachineAddAuthKind
from ..models.ssh_machine_add_use_mode import SshMachineAddUseMode
from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.audience_grant import AudienceGrant


T = TypeVar("T", bound="SshMachineAdd")


@_attrs_define
class SshMachineAdd:
    """
    Attributes:
        host (str):
        username (str):
        auth_kind (SshMachineAddAuthKind):
        name (str):
        host_key_fingerprint (str):
        port (int | Unset):  Default: 22.
        password (None | str | Unset):
        private_key (None | str | Unset):
        passphrase (None | str | Unset):
        use_mode (SshMachineAddUseMode | Unset):  Default: SshMachineAddUseMode.ASSIGNED.
        audience (list[AudienceGrant] | Unset):
        idle_stop_minutes (int | None | Unset):
    """

    host: str
    username: str
    auth_kind: SshMachineAddAuthKind
    name: str
    host_key_fingerprint: str
    port: int | Unset = 22
    password: None | str | Unset = UNSET
    private_key: None | str | Unset = UNSET
    passphrase: None | str | Unset = UNSET
    use_mode: SshMachineAddUseMode | Unset = SshMachineAddUseMode.ASSIGNED
    audience: list[AudienceGrant] | Unset = UNSET
    idle_stop_minutes: int | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        host = self.host

        username = self.username

        auth_kind = self.auth_kind.value

        name = self.name

        host_key_fingerprint = self.host_key_fingerprint

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

        use_mode: str | Unset = UNSET
        if not isinstance(self.use_mode, Unset):
            use_mode = self.use_mode.value

        audience: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.audience, Unset):
            audience = []
            for audience_item_data in self.audience:
                audience_item = audience_item_data.to_dict()
                audience.append(audience_item)

        idle_stop_minutes: int | None | Unset
        if isinstance(self.idle_stop_minutes, Unset):
            idle_stop_minutes = UNSET
        else:
            idle_stop_minutes = self.idle_stop_minutes

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "host": host,
                "username": username,
                "auth_kind": auth_kind,
                "name": name,
                "host_key_fingerprint": host_key_fingerprint,
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
        if use_mode is not UNSET:
            field_dict["use_mode"] = use_mode
        if audience is not UNSET:
            field_dict["audience"] = audience
        if idle_stop_minutes is not UNSET:
            field_dict["idle_stop_minutes"] = idle_stop_minutes

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.audience_grant import AudienceGrant

        d = dict(src_dict)
        host = d.pop("host")

        username = d.pop("username")

        auth_kind = SshMachineAddAuthKind(d.pop("auth_kind"))

        name = d.pop("name")

        host_key_fingerprint = d.pop("host_key_fingerprint")

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

        _use_mode = d.pop("use_mode", UNSET)
        use_mode: SshMachineAddUseMode | Unset
        if isinstance(_use_mode, Unset):
            use_mode = UNSET
        else:
            use_mode = SshMachineAddUseMode(_use_mode)

        _audience = d.pop("audience", UNSET)
        audience: list[AudienceGrant] | Unset = UNSET
        if _audience is not UNSET:
            audience = []
            for audience_item_data in _audience:
                audience_item = AudienceGrant.from_dict(audience_item_data)

                audience.append(audience_item)

        def _parse_idle_stop_minutes(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        idle_stop_minutes = _parse_idle_stop_minutes(d.pop("idle_stop_minutes", UNSET))

        ssh_machine_add = cls(
            host=host,
            username=username,
            auth_kind=auth_kind,
            name=name,
            host_key_fingerprint=host_key_fingerprint,
            port=port,
            password=password,
            private_key=private_key,
            passphrase=passphrase,
            use_mode=use_mode,
            audience=audience,
            idle_stop_minutes=idle_stop_minutes,
        )

        ssh_machine_add.additional_properties = d
        return ssh_machine_add

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
