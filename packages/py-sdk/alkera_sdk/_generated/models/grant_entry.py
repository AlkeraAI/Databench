from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast
from uuid import UUID

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.principal_ref import PrincipalRef


T = TypeVar("T", bound="GrantEntry")


@_attrs_define
class GrantEntry:
    """One line of the permissions answer.

    ``principal_name`` is the one field here that is not an id or an enum, and
    it is filled only on the listing: a share dialog that showed uuids would be
    unusable, and the listing is the one place the name discloses nothing —
    every row on it is a grant that already exists on a node the caller may
    read. It is ``None`` whenever the principal cannot be resolved inside the
    caller's org (a deleted account, a disbanded team, a share link), so the
    absence of a name never becomes an answer about who exists.

        Attributes:
            principal (PrincipalRef): Who a grant is for: a ``user``, a ``team`` or the ``org`` itself.

                The kind is an open registry, so a kind the server has not registered is a
                422 rather than a row nothing can resolve. An ``org`` grant names the
                caller's own org; any other id is a 422 too. A ``user`` or ``team`` id that
                is not a principal of the caller's org (another org's, a stranger, an id
                never issued) is a 404, the same answer for every one of them, so the route
                never confirms who exists outside the org.
            role (str):
            origin (str):
            id (None | Unset | UUID):
            principal_name (None | str | Unset):
            granting_node_id (None | Unset | UUID):
            expires_at (datetime.datetime | None | Unset):
            shown_role (None | str | Unset):
            role_label (None | str | Unset):
            is_owner (bool | Unset):  Default: False.
            can_change (bool | Unset):  Default: False.
            can_remove (bool | Unset):  Default: False.
    """

    principal: PrincipalRef
    role: str
    origin: str
    id: None | Unset | UUID = UNSET
    principal_name: None | str | Unset = UNSET
    granting_node_id: None | Unset | UUID = UNSET
    expires_at: datetime.datetime | None | Unset = UNSET
    shown_role: None | str | Unset = UNSET
    role_label: None | str | Unset = UNSET
    is_owner: bool | Unset = False
    can_change: bool | Unset = False
    can_remove: bool | Unset = False
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        principal = self.principal.to_dict()

        role = self.role

        origin = self.origin

        id: None | str | Unset
        if isinstance(self.id, Unset):
            id = UNSET
        elif isinstance(self.id, UUID):
            id = str(self.id)
        else:
            id = self.id

        principal_name: None | str | Unset
        if isinstance(self.principal_name, Unset):
            principal_name = UNSET
        else:
            principal_name = self.principal_name

        granting_node_id: None | str | Unset
        if isinstance(self.granting_node_id, Unset):
            granting_node_id = UNSET
        elif isinstance(self.granting_node_id, UUID):
            granting_node_id = str(self.granting_node_id)
        else:
            granting_node_id = self.granting_node_id

        expires_at: None | str | Unset
        if isinstance(self.expires_at, Unset):
            expires_at = UNSET
        elif isinstance(self.expires_at, datetime.datetime):
            expires_at = self.expires_at.isoformat()
        else:
            expires_at = self.expires_at

        shown_role: None | str | Unset
        if isinstance(self.shown_role, Unset):
            shown_role = UNSET
        else:
            shown_role = self.shown_role

        role_label: None | str | Unset
        if isinstance(self.role_label, Unset):
            role_label = UNSET
        else:
            role_label = self.role_label

        is_owner = self.is_owner

        can_change = self.can_change

        can_remove = self.can_remove

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "principal": principal,
                "role": role,
                "origin": origin,
            }
        )
        if id is not UNSET:
            field_dict["id"] = id
        if principal_name is not UNSET:
            field_dict["principalName"] = principal_name
        if granting_node_id is not UNSET:
            field_dict["grantingNodeId"] = granting_node_id
        if expires_at is not UNSET:
            field_dict["expiresAt"] = expires_at
        if shown_role is not UNSET:
            field_dict["shownRole"] = shown_role
        if role_label is not UNSET:
            field_dict["roleLabel"] = role_label
        if is_owner is not UNSET:
            field_dict["isOwner"] = is_owner
        if can_change is not UNSET:
            field_dict["canChange"] = can_change
        if can_remove is not UNSET:
            field_dict["canRemove"] = can_remove

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.principal_ref import PrincipalRef

        d = dict(src_dict)
        principal = PrincipalRef.from_dict(d.pop("principal"))

        role = d.pop("role")

        origin = d.pop("origin")

        def _parse_id(data: object) -> None | Unset | UUID:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                id_type_0 = UUID(data)

                return id_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | Unset | UUID, data)

        id = _parse_id(d.pop("id", UNSET))

        def _parse_principal_name(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        principal_name = _parse_principal_name(d.pop("principalName", UNSET))

        def _parse_granting_node_id(data: object) -> None | Unset | UUID:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                granting_node_id_type_0 = UUID(data)

                return granting_node_id_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | Unset | UUID, data)

        granting_node_id = _parse_granting_node_id(d.pop("grantingNodeId", UNSET))

        def _parse_expires_at(data: object) -> datetime.datetime | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                expires_at_type_0 = datetime.datetime.fromisoformat(data)

                return expires_at_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None | Unset, data)

        expires_at = _parse_expires_at(d.pop("expiresAt", UNSET))

        def _parse_shown_role(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        shown_role = _parse_shown_role(d.pop("shownRole", UNSET))

        def _parse_role_label(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        role_label = _parse_role_label(d.pop("roleLabel", UNSET))

        is_owner = d.pop("isOwner", UNSET)

        can_change = d.pop("canChange", UNSET)

        can_remove = d.pop("canRemove", UNSET)

        grant_entry = cls(
            principal=principal,
            role=role,
            origin=origin,
            id=id,
            principal_name=principal_name,
            granting_node_id=granting_node_id,
            expires_at=expires_at,
            shown_role=shown_role,
            role_label=role_label,
            is_owner=is_owner,
            can_change=can_change,
            can_remove=can_remove,
        )

        grant_entry.additional_properties = d
        return grant_entry

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
