from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.notebook_connection_kind import NotebookConnectionKind
from ..types import UNSET, Unset

T = TypeVar("T", bound="NotebookConnection")


@_attrs_define
class NotebookConnection:
    """One connection a SQL cell of the notebook may name.

    ``name`` is the connection's name, what a cell stores
    (``alkera.sql(..., connection="<name>")``) and what the kernel resolves
    on the box; it is also how the connection is shown. ``can_use`` is the
    server's answer for this reader and this connection, with ``reason`` one
    sentence when it is no.

        Attributes:
            id (str):
            name (str):
            engine (str):
            engine_title (str):
            kind (NotebookConnectionKind):
            can_use (bool):
            team_name (str | Unset):  Default: ''.
            credential_owner (str | Unset):  Default: ''.
            reason (str | Unset):  Default: ''.
    """

    id: str
    name: str
    engine: str
    engine_title: str
    kind: NotebookConnectionKind
    can_use: bool
    team_name: str | Unset = ""
    credential_owner: str | Unset = ""
    reason: str | Unset = ""
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        id = self.id

        name = self.name

        engine = self.engine

        engine_title = self.engine_title

        kind = self.kind.value

        can_use = self.can_use

        team_name = self.team_name

        credential_owner = self.credential_owner

        reason = self.reason

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "name": name,
                "engine": engine,
                "engine_title": engine_title,
                "kind": kind,
                "can_use": can_use,
            }
        )
        if team_name is not UNSET:
            field_dict["team_name"] = team_name
        if credential_owner is not UNSET:
            field_dict["credential_owner"] = credential_owner
        if reason is not UNSET:
            field_dict["reason"] = reason

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        id = d.pop("id")

        name = d.pop("name")

        engine = d.pop("engine")

        engine_title = d.pop("engine_title")

        kind = NotebookConnectionKind(d.pop("kind"))

        can_use = d.pop("can_use")

        team_name = d.pop("team_name", UNSET)

        credential_owner = d.pop("credential_owner", UNSET)

        reason = d.pop("reason", UNSET)

        notebook_connection = cls(
            id=id,
            name=name,
            engine=engine,
            engine_title=engine_title,
            kind=kind,
            can_use=can_use,
            team_name=team_name,
            credential_owner=credential_owner,
            reason=reason,
        )

        notebook_connection.additional_properties = d
        return notebook_connection

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
