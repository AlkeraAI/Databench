from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast
from uuid import UUID

from attrs import define as _attrs_define
from attrs import field as _attrs_field

if TYPE_CHECKING:
    from ..models.security_event_read_detail_type_0 import SecurityEventReadDetailType0


T = TypeVar("T", bound="SecurityEventRead")


@_attrs_define
class SecurityEventRead:
    """One event in the caller's security log. ``org_team_id`` is the org the
    caller's session was in when it happened, when there was one.

        Attributes:
            id (UUID):
            event (str):
            org_team_id (None | UUID):
            ip_prefix (None | str):
            user_agent (None | str):
            detail (None | SecurityEventReadDetailType0):
            created_at (datetime.datetime):
    """

    id: UUID
    event: str
    org_team_id: None | UUID
    ip_prefix: None | str
    user_agent: None | str
    detail: None | SecurityEventReadDetailType0
    created_at: datetime.datetime
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.security_event_read_detail_type_0 import (
            SecurityEventReadDetailType0,
        )

        id = str(self.id)

        event = self.event

        org_team_id: None | str
        if isinstance(self.org_team_id, UUID):
            org_team_id = str(self.org_team_id)
        else:
            org_team_id = self.org_team_id

        ip_prefix: None | str
        ip_prefix = self.ip_prefix

        user_agent: None | str
        user_agent = self.user_agent

        detail: dict[str, Any] | None
        if isinstance(self.detail, SecurityEventReadDetailType0):
            detail = self.detail.to_dict()
        else:
            detail = self.detail

        created_at = self.created_at.isoformat()

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "event": event,
                "org_team_id": org_team_id,
                "ip_prefix": ip_prefix,
                "user_agent": user_agent,
                "detail": detail,
                "created_at": created_at,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.security_event_read_detail_type_0 import (
            SecurityEventReadDetailType0,
        )

        d = dict(src_dict)
        id = UUID(d.pop("id"))

        event = d.pop("event")

        def _parse_org_team_id(data: object) -> None | UUID:
            if data is None:
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                org_team_id_type_0 = UUID(data)

                return org_team_id_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | UUID, data)

        org_team_id = _parse_org_team_id(d.pop("org_team_id"))

        def _parse_ip_prefix(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        ip_prefix = _parse_ip_prefix(d.pop("ip_prefix"))

        def _parse_user_agent(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        user_agent = _parse_user_agent(d.pop("user_agent"))

        def _parse_detail(data: object) -> None | SecurityEventReadDetailType0:
            if data is None:
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                detail_type_0 = SecurityEventReadDetailType0.from_dict(data)

                return detail_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | SecurityEventReadDetailType0, data)

        detail = _parse_detail(d.pop("detail"))

        created_at = datetime.datetime.fromisoformat(d.pop("created_at"))

        security_event_read = cls(
            id=id,
            event=event,
            org_team_id=org_team_id,
            ip_prefix=ip_prefix,
            user_agent=user_agent,
            detail=detail,
            created_at=created_at,
        )

        security_event_read.additional_properties = d
        return security_event_read

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
