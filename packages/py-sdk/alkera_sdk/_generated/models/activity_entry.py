from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="ActivityEntry")


@_attrs_define
class ActivityEntry:
    """One history row as a client sees it: ids and a kind, never a name.

    "Errors carry ids, never names" is a rule about every Files body, not only
    error bodies, so the before/after payloads the library records are
    not rendered here.

        Attributes:
            id (str):
            node_id (str):
            seq (int):
            kind (str):
            at (str):
            acting_principal (str):
            delegating_user (None | str | Unset):
            agent_session_id (None | str | Unset):
            op_id (None | str | Unset):
    """

    id: str
    node_id: str
    seq: int
    kind: str
    at: str
    acting_principal: str
    delegating_user: None | str | Unset = UNSET
    agent_session_id: None | str | Unset = UNSET
    op_id: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        id = self.id

        node_id = self.node_id

        seq = self.seq

        kind = self.kind

        at = self.at

        acting_principal = self.acting_principal

        delegating_user: None | str | Unset
        if isinstance(self.delegating_user, Unset):
            delegating_user = UNSET
        else:
            delegating_user = self.delegating_user

        agent_session_id: None | str | Unset
        if isinstance(self.agent_session_id, Unset):
            agent_session_id = UNSET
        else:
            agent_session_id = self.agent_session_id

        op_id: None | str | Unset
        if isinstance(self.op_id, Unset):
            op_id = UNSET
        else:
            op_id = self.op_id

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "nodeId": node_id,
                "seq": seq,
                "kind": kind,
                "at": at,
                "actingPrincipal": acting_principal,
            }
        )
        if delegating_user is not UNSET:
            field_dict["delegatingUser"] = delegating_user
        if agent_session_id is not UNSET:
            field_dict["agentSessionId"] = agent_session_id
        if op_id is not UNSET:
            field_dict["opId"] = op_id

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        id = d.pop("id")

        node_id = d.pop("nodeId")

        seq = d.pop("seq")

        kind = d.pop("kind")

        at = d.pop("at")

        acting_principal = d.pop("actingPrincipal")

        def _parse_delegating_user(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        delegating_user = _parse_delegating_user(d.pop("delegatingUser", UNSET))

        def _parse_agent_session_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        agent_session_id = _parse_agent_session_id(d.pop("agentSessionId", UNSET))

        def _parse_op_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        op_id = _parse_op_id(d.pop("opId", UNSET))

        activity_entry = cls(
            id=id,
            node_id=node_id,
            seq=seq,
            kind=kind,
            at=at,
            acting_principal=acting_principal,
            delegating_user=delegating_user,
            agent_session_id=agent_session_id,
            op_id=op_id,
        )

        activity_entry.additional_properties = d
        return activity_entry

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
