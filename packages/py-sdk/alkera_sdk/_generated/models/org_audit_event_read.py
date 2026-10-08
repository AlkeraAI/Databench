from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast
from uuid import UUID

from attrs import define as _attrs_define
from attrs import field as _attrs_field

if TYPE_CHECKING:
    from ..models.org_audit_event_read_detail_type_0 import OrgAuditEventReadDetailType0


T = TypeVar("T", bound="OrgAuditEventRead")


@_attrs_define
class OrgAuditEventRead:
    """
    Attributes:
        id (UUID):
        actor_email (str):
        action (str):
        target (None | str):
        detail (None | OrgAuditEventReadDetailType0):
        created_at (datetime.datetime):
    """

    id: UUID
    actor_email: str
    action: str
    target: None | str
    detail: None | OrgAuditEventReadDetailType0
    created_at: datetime.datetime
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.org_audit_event_read_detail_type_0 import (
            OrgAuditEventReadDetailType0,
        )

        id = str(self.id)

        actor_email = self.actor_email

        action = self.action

        target: None | str
        target = self.target

        detail: dict[str, Any] | None
        if isinstance(self.detail, OrgAuditEventReadDetailType0):
            detail = self.detail.to_dict()
        else:
            detail = self.detail

        created_at = self.created_at.isoformat()

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "actor_email": actor_email,
                "action": action,
                "target": target,
                "detail": detail,
                "created_at": created_at,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.org_audit_event_read_detail_type_0 import (
            OrgAuditEventReadDetailType0,
        )

        d = dict(src_dict)
        id = UUID(d.pop("id"))

        actor_email = d.pop("actor_email")

        action = d.pop("action")

        def _parse_target(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        target = _parse_target(d.pop("target"))

        def _parse_detail(data: object) -> None | OrgAuditEventReadDetailType0:
            if data is None:
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                detail_type_0 = OrgAuditEventReadDetailType0.from_dict(data)

                return detail_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | OrgAuditEventReadDetailType0, data)

        detail = _parse_detail(d.pop("detail"))

        created_at = datetime.datetime.fromisoformat(d.pop("created_at"))

        org_audit_event_read = cls(
            id=id,
            actor_email=actor_email,
            action=action,
            target=target,
            detail=detail,
            created_at=created_at,
        )

        org_audit_event_read.additional_properties = d
        return org_audit_event_read

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
