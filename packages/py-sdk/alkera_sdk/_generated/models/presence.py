from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.presence_kind_type_0 import PresenceKindType0
from ..types import UNSET, Unset

T = TypeVar("T", bound="Presence")


@_attrs_define
class Presence:
    """Someone who is in a cell now (the rule is ``document/editing.py``).

    Attributes:
        who (str):
        cell_id (str):
        kind (None | PresenceKindType0 | Unset):
        at (datetime.datetime | None | Unset):
        actor_id (None | str | Unset):
        caret (bool | None | Unset):
        expires_in (float | None | Unset):
    """

    who: str
    cell_id: str
    kind: None | PresenceKindType0 | Unset = UNSET
    at: datetime.datetime | None | Unset = UNSET
    actor_id: None | str | Unset = UNSET
    caret: bool | None | Unset = UNSET
    expires_in: float | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        who = self.who

        cell_id = self.cell_id

        kind: None | str | Unset
        if isinstance(self.kind, Unset):
            kind = UNSET
        elif isinstance(self.kind, PresenceKindType0):
            kind = self.kind.value
        else:
            kind = self.kind

        at: None | str | Unset
        if isinstance(self.at, Unset):
            at = UNSET
        elif isinstance(self.at, datetime.datetime):
            at = self.at.isoformat()
        else:
            at = self.at

        actor_id: None | str | Unset
        if isinstance(self.actor_id, Unset):
            actor_id = UNSET
        else:
            actor_id = self.actor_id

        caret: bool | None | Unset
        if isinstance(self.caret, Unset):
            caret = UNSET
        else:
            caret = self.caret

        expires_in: float | None | Unset
        if isinstance(self.expires_in, Unset):
            expires_in = UNSET
        else:
            expires_in = self.expires_in

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "who": who,
                "cell_id": cell_id,
            }
        )
        if kind is not UNSET:
            field_dict["kind"] = kind
        if at is not UNSET:
            field_dict["at"] = at
        if actor_id is not UNSET:
            field_dict["actor_id"] = actor_id
        if caret is not UNSET:
            field_dict["caret"] = caret
        if expires_in is not UNSET:
            field_dict["expires_in"] = expires_in

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        who = d.pop("who")

        cell_id = d.pop("cell_id")

        def _parse_kind(data: object) -> None | PresenceKindType0 | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                kind_type_0 = PresenceKindType0(data)

                return kind_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | PresenceKindType0 | Unset, data)

        kind = _parse_kind(d.pop("kind", UNSET))

        def _parse_at(data: object) -> datetime.datetime | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                at_type_0 = datetime.datetime.fromisoformat(data)

                return at_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None | Unset, data)

        at = _parse_at(d.pop("at", UNSET))

        def _parse_actor_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        actor_id = _parse_actor_id(d.pop("actor_id", UNSET))

        def _parse_caret(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        caret = _parse_caret(d.pop("caret", UNSET))

        def _parse_expires_in(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        expires_in = _parse_expires_in(d.pop("expires_in", UNSET))

        presence = cls(
            who=who,
            cell_id=cell_id,
            kind=kind,
            at=at,
            actor_id=actor_id,
            caret=caret,
            expires_in=expires_in,
        )

        presence.additional_properties = d
        return presence

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
