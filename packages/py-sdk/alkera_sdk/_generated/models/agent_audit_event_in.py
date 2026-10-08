from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.agent_audit_event_in_action import AgentAuditEventInAction
from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.detail import Detail


T = TypeVar("T", bound="AgentAuditEventIn")


@_attrs_define
class AgentAuditEventIn:
    """One agent-activity event as reported by the daemon.

    Carries who/what/when/outcome. Trace content never leaves the member's
    machine and is redacted server-side if sent anyway.

        Attributes:
            action (AgentAuditEventInAction):
            session_id (str):
            occurred_at (datetime.datetime):
            target (None | str | Unset):
            detail (Detail | Unset):
    """

    action: AgentAuditEventInAction
    session_id: str
    occurred_at: datetime.datetime
    target: None | str | Unset = UNSET
    detail: Detail | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        action = self.action.value

        session_id = self.session_id

        occurred_at = self.occurred_at.isoformat()

        target: None | str | Unset
        if isinstance(self.target, Unset):
            target = UNSET
        else:
            target = self.target

        detail: dict[str, Any] | Unset = UNSET
        if not isinstance(self.detail, Unset):
            detail = self.detail.to_dict()

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "action": action,
                "session_id": session_id,
                "occurred_at": occurred_at,
            }
        )
        if target is not UNSET:
            field_dict["target"] = target
        if detail is not UNSET:
            field_dict["detail"] = detail

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.detail import Detail

        d = dict(src_dict)
        action = AgentAuditEventInAction(d.pop("action"))

        session_id = d.pop("session_id")

        occurred_at = datetime.datetime.fromisoformat(d.pop("occurred_at"))

        def _parse_target(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        target = _parse_target(d.pop("target", UNSET))

        _detail = d.pop("detail", UNSET)
        detail: Detail | Unset
        if isinstance(_detail, Unset):
            detail = UNSET
        else:
            detail = Detail.from_dict(_detail)

        agent_audit_event_in = cls(
            action=action,
            session_id=session_id,
            occurred_at=occurred_at,
            target=target,
            detail=detail,
        )

        agent_audit_event_in.additional_properties = d
        return agent_audit_event_in

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
