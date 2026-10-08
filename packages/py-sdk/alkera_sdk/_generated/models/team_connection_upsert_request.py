from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast
from uuid import UUID

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.fields import Fields
    from ..models.team_connection_upsert_request_oauth_config_type_0 import (
        TeamConnectionUpsertRequestOauthConfigType0,
    )


T = TypeVar("T", bound="TeamConnectionUpsertRequest")


@_attrs_define
class TeamConnectionUpsertRequest:
    """Create/update a preconfigured connection (team admins).

    ``fields`` is the COMPLETE raw form input — every value, including the ones
    the admin keeps to themselves — because the server can only verify a whole
    connection, and this is the one moment somebody holds one. It builds and
    test-dials that, then stores none of it: what persists is the subset named
    by ``shared_fields``, raw, plus each independently encrypted credential role
    that was shared. A shared secret left blank on edit preserves that same
    primary or named role; a role is retired when the edited build no longer
    declares it, including when an optional feature is disabled.
    ``oauth_client_secret`` is likewise write-only, with None meaning keep.

        Attributes:
            plugin (str):
            handle (str):
            fields (Fields | Unset):
            shared_fields (list[str] | Unset):
            auth_method (str | Unset):  Default: ''.
            auto_add (bool | Unset):  Default: False.
            enabled (bool | Unset):  Default: True.
            oauth_client_id (None | str | Unset):
            oauth_client_secret (None | str | Unset):
            oauth_config (None | TeamConnectionUpsertRequestOauthConfigType0 | Unset):
            verification_id (None | Unset | UUID):
    """

    plugin: str
    handle: str
    fields: Fields | Unset = UNSET
    shared_fields: list[str] | Unset = UNSET
    auth_method: str | Unset = ""
    auto_add: bool | Unset = False
    enabled: bool | Unset = True
    oauth_client_id: None | str | Unset = UNSET
    oauth_client_secret: None | str | Unset = UNSET
    oauth_config: None | TeamConnectionUpsertRequestOauthConfigType0 | Unset = UNSET
    verification_id: None | Unset | UUID = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.team_connection_upsert_request_oauth_config_type_0 import (
            TeamConnectionUpsertRequestOauthConfigType0,
        )

        plugin = self.plugin

        handle = self.handle

        fields: dict[str, Any] | Unset = UNSET
        if not isinstance(self.fields, Unset):
            fields = self.fields.to_dict()

        shared_fields: list[str] | Unset = UNSET
        if not isinstance(self.shared_fields, Unset):
            shared_fields = self.shared_fields

        auth_method = self.auth_method

        auto_add = self.auto_add

        enabled = self.enabled

        oauth_client_id: None | str | Unset
        if isinstance(self.oauth_client_id, Unset):
            oauth_client_id = UNSET
        else:
            oauth_client_id = self.oauth_client_id

        oauth_client_secret: None | str | Unset
        if isinstance(self.oauth_client_secret, Unset):
            oauth_client_secret = UNSET
        else:
            oauth_client_secret = self.oauth_client_secret

        oauth_config: dict[str, Any] | None | Unset
        if isinstance(self.oauth_config, Unset):
            oauth_config = UNSET
        elif isinstance(self.oauth_config, TeamConnectionUpsertRequestOauthConfigType0):
            oauth_config = self.oauth_config.to_dict()
        else:
            oauth_config = self.oauth_config

        verification_id: None | str | Unset
        if isinstance(self.verification_id, Unset):
            verification_id = UNSET
        elif isinstance(self.verification_id, UUID):
            verification_id = str(self.verification_id)
        else:
            verification_id = self.verification_id

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "plugin": plugin,
                "handle": handle,
            }
        )
        if fields is not UNSET:
            field_dict["fields"] = fields
        if shared_fields is not UNSET:
            field_dict["shared_fields"] = shared_fields
        if auth_method is not UNSET:
            field_dict["auth_method"] = auth_method
        if auto_add is not UNSET:
            field_dict["auto_add"] = auto_add
        if enabled is not UNSET:
            field_dict["enabled"] = enabled
        if oauth_client_id is not UNSET:
            field_dict["oauth_client_id"] = oauth_client_id
        if oauth_client_secret is not UNSET:
            field_dict["oauth_client_secret"] = oauth_client_secret
        if oauth_config is not UNSET:
            field_dict["oauth_config"] = oauth_config
        if verification_id is not UNSET:
            field_dict["verification_id"] = verification_id

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.fields import Fields
        from ..models.team_connection_upsert_request_oauth_config_type_0 import (
            TeamConnectionUpsertRequestOauthConfigType0,
        )

        d = dict(src_dict)
        plugin = d.pop("plugin")

        handle = d.pop("handle")

        _fields = d.pop("fields", UNSET)
        fields: Fields | Unset
        if isinstance(_fields, Unset):
            fields = UNSET
        else:
            fields = Fields.from_dict(_fields)

        shared_fields = cast(list[str], d.pop("shared_fields", UNSET))

        auth_method = d.pop("auth_method", UNSET)

        auto_add = d.pop("auto_add", UNSET)

        enabled = d.pop("enabled", UNSET)

        def _parse_oauth_client_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        oauth_client_id = _parse_oauth_client_id(d.pop("oauth_client_id", UNSET))

        def _parse_oauth_client_secret(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        oauth_client_secret = _parse_oauth_client_secret(d.pop("oauth_client_secret", UNSET))

        def _parse_oauth_config(
            data: object,
        ) -> None | TeamConnectionUpsertRequestOauthConfigType0 | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                oauth_config_type_0 = TeamConnectionUpsertRequestOauthConfigType0.from_dict(data)

                return oauth_config_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | TeamConnectionUpsertRequestOauthConfigType0 | Unset, data)

        oauth_config = _parse_oauth_config(d.pop("oauth_config", UNSET))

        def _parse_verification_id(data: object) -> None | Unset | UUID:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                verification_id_type_0 = UUID(data)

                return verification_id_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | Unset | UUID, data)

        verification_id = _parse_verification_id(d.pop("verification_id", UNSET))

        team_connection_upsert_request = cls(
            plugin=plugin,
            handle=handle,
            fields=fields,
            shared_fields=shared_fields,
            auth_method=auth_method,
            auto_add=auto_add,
            enabled=enabled,
            oauth_client_id=oauth_client_id,
            oauth_client_secret=oauth_client_secret,
            oauth_config=oauth_config,
            verification_id=verification_id,
        )

        team_connection_upsert_request.additional_properties = d
        return team_connection_upsert_request

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
