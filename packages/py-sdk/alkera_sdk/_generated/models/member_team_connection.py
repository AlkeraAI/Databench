from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast
from uuid import UUID

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.badge import Badge
from ..models.credential_state import CredentialState
from ..models.member_team_connection_auth_mode import MemberTeamConnectionAuthMode
from ..models.member_team_connection_shared_custody import MemberTeamConnectionSharedCustody
from ..models.outcome import Outcome
from ..models.reauth import Reauth
from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.member_shared_values import MemberSharedValues
    from ..models.member_team_connection_values_doc_item import MemberTeamConnectionValuesDocItem


T = TypeVar("T", bound="MemberTeamConnection")


@_attrs_define
class MemberTeamConnection:
    """The member sync-down shape — full definition, NO secrets. The member's
    daemon reconciles these into the workspace (suggested / auto-added).

        Attributes:
            id (UUID):
            team_id (UUID):
            team_name (str):
            plugin (str):
            handle (str):
            auth_mode (MemberTeamConnectionAuthMode):
            updated_at (datetime.datetime):
            owner_user_id (None | Unset | UUID):
            created_by_id (None | Unset | UUID):
            created_by_name (str | Unset):  Default: ''.
            can_manage (bool | Unset):  Default: False.
            shared_values (MemberSharedValues | Unset):
            auth_method (str | Unset):  Default: ''.
            member_fields (list[str] | Unset):
            values_doc (list[MemberTeamConnectionValuesDocItem] | Unset):
            ask_groups (list[list[str]] | Unset):
            auto_add (bool | Unset):  Default: False.
            enabled (bool | Unset):  Default: True.
            has_shared_secret (bool | Unset): Compatibility flag: true when any primary or named shared credential is
                stored. Default: False.
            has_primary_secret (bool | Unset): True when the shared credential bundle includes a primary value. Default:
                False.
            credential_version (int | Unset):  Default: 0.
            shared_custody (MemberTeamConnectionSharedCustody | Unset):  Default: MemberTeamConnectionSharedCustody.VALUE_0.
            oauth_client_id (None | str | Unset):
            badge (Badge | Unset): The one derived status a person sees.
            badge_reason (str | Unset):  Default: ''.
            outcome (None | Outcome | Unset):
            last_verified_at (datetime.datetime | None | Unset):
            credential_state (CredentialState | Unset): The state of one stored secret. The product writes ``present``,
                ``needs_reauth``, ``unreadable`` and ``revoked`` today; ``absent``,
                ``expiring``, ``expired`` and ``refreshing`` are reserved for expiry and
                refresh tracking and already derive correctly.
            reauth (None | Reauth | Unset):
            lease_refusal (str | Unset):  Default: ''.
    """

    id: UUID
    team_id: UUID
    team_name: str
    plugin: str
    handle: str
    auth_mode: MemberTeamConnectionAuthMode
    updated_at: datetime.datetime
    owner_user_id: None | Unset | UUID = UNSET
    created_by_id: None | Unset | UUID = UNSET
    created_by_name: str | Unset = ""
    can_manage: bool | Unset = False
    shared_values: MemberSharedValues | Unset = UNSET
    auth_method: str | Unset = ""
    member_fields: list[str] | Unset = UNSET
    values_doc: list[MemberTeamConnectionValuesDocItem] | Unset = UNSET
    ask_groups: list[list[str]] | Unset = UNSET
    auto_add: bool | Unset = False
    enabled: bool | Unset = True
    has_shared_secret: bool | Unset = False
    has_primary_secret: bool | Unset = False
    credential_version: int | Unset = 0
    shared_custody: MemberTeamConnectionSharedCustody | Unset = (
        MemberTeamConnectionSharedCustody.VALUE_0
    )
    oauth_client_id: None | str | Unset = UNSET
    badge: Badge | Unset = UNSET
    badge_reason: str | Unset = ""
    outcome: None | Outcome | Unset = UNSET
    last_verified_at: datetime.datetime | None | Unset = UNSET
    credential_state: CredentialState | Unset = UNSET
    reauth: None | Reauth | Unset = UNSET
    lease_refusal: str | Unset = ""
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        id = str(self.id)

        team_id = str(self.team_id)

        team_name = self.team_name

        plugin = self.plugin

        handle = self.handle

        auth_mode = self.auth_mode.value

        updated_at = self.updated_at.isoformat()

        owner_user_id: None | str | Unset
        if isinstance(self.owner_user_id, Unset):
            owner_user_id = UNSET
        elif isinstance(self.owner_user_id, UUID):
            owner_user_id = str(self.owner_user_id)
        else:
            owner_user_id = self.owner_user_id

        created_by_id: None | str | Unset
        if isinstance(self.created_by_id, Unset):
            created_by_id = UNSET
        elif isinstance(self.created_by_id, UUID):
            created_by_id = str(self.created_by_id)
        else:
            created_by_id = self.created_by_id

        created_by_name = self.created_by_name

        can_manage = self.can_manage

        shared_values: dict[str, Any] | Unset = UNSET
        if not isinstance(self.shared_values, Unset):
            shared_values = self.shared_values.to_dict()

        auth_method = self.auth_method

        member_fields: list[str] | Unset = UNSET
        if not isinstance(self.member_fields, Unset):
            member_fields = self.member_fields

        values_doc: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.values_doc, Unset):
            values_doc = []
            for values_doc_item_data in self.values_doc:
                values_doc_item = values_doc_item_data.to_dict()
                values_doc.append(values_doc_item)

        ask_groups: list[list[str]] | Unset = UNSET
        if not isinstance(self.ask_groups, Unset):
            ask_groups = []
            for ask_groups_item_data in self.ask_groups:
                ask_groups_item = ask_groups_item_data

                ask_groups.append(ask_groups_item)

        auto_add = self.auto_add

        enabled = self.enabled

        has_shared_secret = self.has_shared_secret

        has_primary_secret = self.has_primary_secret

        credential_version = self.credential_version

        shared_custody: str | Unset = UNSET
        if not isinstance(self.shared_custody, Unset):
            shared_custody = self.shared_custody.value

        oauth_client_id: None | str | Unset
        if isinstance(self.oauth_client_id, Unset):
            oauth_client_id = UNSET
        else:
            oauth_client_id = self.oauth_client_id

        badge: str | Unset = UNSET
        if not isinstance(self.badge, Unset):
            badge = self.badge.value

        badge_reason = self.badge_reason

        outcome: None | str | Unset
        if isinstance(self.outcome, Unset):
            outcome = UNSET
        elif isinstance(self.outcome, Outcome):
            outcome = self.outcome.value
        else:
            outcome = self.outcome

        last_verified_at: None | str | Unset
        if isinstance(self.last_verified_at, Unset):
            last_verified_at = UNSET
        elif isinstance(self.last_verified_at, datetime.datetime):
            last_verified_at = self.last_verified_at.isoformat()
        else:
            last_verified_at = self.last_verified_at

        credential_state: str | Unset = UNSET
        if not isinstance(self.credential_state, Unset):
            credential_state = self.credential_state.value

        reauth: None | str | Unset
        if isinstance(self.reauth, Unset):
            reauth = UNSET
        elif isinstance(self.reauth, Reauth):
            reauth = self.reauth.value
        else:
            reauth = self.reauth

        lease_refusal = self.lease_refusal

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "team_id": team_id,
                "team_name": team_name,
                "plugin": plugin,
                "handle": handle,
                "auth_mode": auth_mode,
                "updated_at": updated_at,
            }
        )
        if owner_user_id is not UNSET:
            field_dict["owner_user_id"] = owner_user_id
        if created_by_id is not UNSET:
            field_dict["created_by_id"] = created_by_id
        if created_by_name is not UNSET:
            field_dict["created_by_name"] = created_by_name
        if can_manage is not UNSET:
            field_dict["can_manage"] = can_manage
        if shared_values is not UNSET:
            field_dict["shared_values"] = shared_values
        if auth_method is not UNSET:
            field_dict["auth_method"] = auth_method
        if member_fields is not UNSET:
            field_dict["member_fields"] = member_fields
        if values_doc is not UNSET:
            field_dict["values_doc"] = values_doc
        if ask_groups is not UNSET:
            field_dict["ask_groups"] = ask_groups
        if auto_add is not UNSET:
            field_dict["auto_add"] = auto_add
        if enabled is not UNSET:
            field_dict["enabled"] = enabled
        if has_shared_secret is not UNSET:
            field_dict["has_shared_secret"] = has_shared_secret
        if has_primary_secret is not UNSET:
            field_dict["has_primary_secret"] = has_primary_secret
        if credential_version is not UNSET:
            field_dict["credential_version"] = credential_version
        if shared_custody is not UNSET:
            field_dict["shared_custody"] = shared_custody
        if oauth_client_id is not UNSET:
            field_dict["oauth_client_id"] = oauth_client_id
        if badge is not UNSET:
            field_dict["badge"] = badge
        if badge_reason is not UNSET:
            field_dict["badge_reason"] = badge_reason
        if outcome is not UNSET:
            field_dict["outcome"] = outcome
        if last_verified_at is not UNSET:
            field_dict["last_verified_at"] = last_verified_at
        if credential_state is not UNSET:
            field_dict["credential_state"] = credential_state
        if reauth is not UNSET:
            field_dict["reauth"] = reauth
        if lease_refusal is not UNSET:
            field_dict["lease_refusal"] = lease_refusal

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.member_shared_values import MemberSharedValues
        from ..models.member_team_connection_values_doc_item import (
            MemberTeamConnectionValuesDocItem,
        )

        d = dict(src_dict)
        id = UUID(d.pop("id"))

        team_id = UUID(d.pop("team_id"))

        team_name = d.pop("team_name")

        plugin = d.pop("plugin")

        handle = d.pop("handle")

        auth_mode = MemberTeamConnectionAuthMode(d.pop("auth_mode"))

        updated_at = datetime.datetime.fromisoformat(d.pop("updated_at"))

        def _parse_owner_user_id(data: object) -> None | Unset | UUID:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                owner_user_id_type_0 = UUID(data)

                return owner_user_id_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | Unset | UUID, data)

        owner_user_id = _parse_owner_user_id(d.pop("owner_user_id", UNSET))

        def _parse_created_by_id(data: object) -> None | Unset | UUID:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                created_by_id_type_0 = UUID(data)

                return created_by_id_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | Unset | UUID, data)

        created_by_id = _parse_created_by_id(d.pop("created_by_id", UNSET))

        created_by_name = d.pop("created_by_name", UNSET)

        can_manage = d.pop("can_manage", UNSET)

        _shared_values = d.pop("shared_values", UNSET)
        shared_values: MemberSharedValues | Unset
        if isinstance(_shared_values, Unset):
            shared_values = UNSET
        else:
            shared_values = MemberSharedValues.from_dict(_shared_values)

        auth_method = d.pop("auth_method", UNSET)

        member_fields = cast(list[str], d.pop("member_fields", UNSET))

        _values_doc = d.pop("values_doc", UNSET)
        values_doc: list[MemberTeamConnectionValuesDocItem] | Unset = UNSET
        if _values_doc is not UNSET:
            values_doc = []
            for values_doc_item_data in _values_doc:
                values_doc_item = MemberTeamConnectionValuesDocItem.from_dict(values_doc_item_data)

                values_doc.append(values_doc_item)

        _ask_groups = d.pop("ask_groups", UNSET)
        ask_groups: list[list[str]] | Unset = UNSET
        if _ask_groups is not UNSET:
            ask_groups = []
            for ask_groups_item_data in _ask_groups:
                ask_groups_item = cast(list[str], ask_groups_item_data)

                ask_groups.append(ask_groups_item)

        auto_add = d.pop("auto_add", UNSET)

        enabled = d.pop("enabled", UNSET)

        has_shared_secret = d.pop("has_shared_secret", UNSET)

        has_primary_secret = d.pop("has_primary_secret", UNSET)

        credential_version = d.pop("credential_version", UNSET)

        _shared_custody = d.pop("shared_custody", UNSET)
        shared_custody: MemberTeamConnectionSharedCustody | Unset
        if isinstance(_shared_custody, Unset):
            shared_custody = UNSET
        else:
            shared_custody = MemberTeamConnectionSharedCustody(_shared_custody)

        def _parse_oauth_client_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        oauth_client_id = _parse_oauth_client_id(d.pop("oauth_client_id", UNSET))

        _badge = d.pop("badge", UNSET)
        badge: Badge | Unset
        if isinstance(_badge, Unset):
            badge = UNSET
        else:
            badge = Badge(_badge)

        badge_reason = d.pop("badge_reason", UNSET)

        def _parse_outcome(data: object) -> None | Outcome | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                outcome_type_0 = Outcome(data)

                return outcome_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | Outcome | Unset, data)

        outcome = _parse_outcome(d.pop("outcome", UNSET))

        def _parse_last_verified_at(data: object) -> datetime.datetime | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                last_verified_at_type_0 = datetime.datetime.fromisoformat(data)

                return last_verified_at_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None | Unset, data)

        last_verified_at = _parse_last_verified_at(d.pop("last_verified_at", UNSET))

        _credential_state = d.pop("credential_state", UNSET)
        credential_state: CredentialState | Unset
        if isinstance(_credential_state, Unset):
            credential_state = UNSET
        else:
            credential_state = CredentialState(_credential_state)

        def _parse_reauth(data: object) -> None | Reauth | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                reauth_type_0 = Reauth(data)

                return reauth_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | Reauth | Unset, data)

        reauth = _parse_reauth(d.pop("reauth", UNSET))

        lease_refusal = d.pop("lease_refusal", UNSET)

        member_team_connection = cls(
            id=id,
            team_id=team_id,
            team_name=team_name,
            plugin=plugin,
            handle=handle,
            auth_mode=auth_mode,
            updated_at=updated_at,
            owner_user_id=owner_user_id,
            created_by_id=created_by_id,
            created_by_name=created_by_name,
            can_manage=can_manage,
            shared_values=shared_values,
            auth_method=auth_method,
            member_fields=member_fields,
            values_doc=values_doc,
            ask_groups=ask_groups,
            auto_add=auto_add,
            enabled=enabled,
            has_shared_secret=has_shared_secret,
            has_primary_secret=has_primary_secret,
            credential_version=credential_version,
            shared_custody=shared_custody,
            oauth_client_id=oauth_client_id,
            badge=badge,
            badge_reason=badge_reason,
            outcome=outcome,
            last_verified_at=last_verified_at,
            credential_state=credential_state,
            reauth=reauth,
            lease_refusal=lease_refusal,
        )

        member_team_connection.additional_properties = d
        return member_team_connection

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
