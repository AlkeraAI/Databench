from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast
from uuid import UUID

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.audit_log_read_detail_type_0 import AuditLogReadDetailType0


T = TypeVar("T", bound="AuditLogRead")


@_attrs_define
class AuditLogRead:
    """
    Attributes:
        id (UUID):
        actor_id (None | UUID):
        actor_email (str):
        actor_platform_role (None | str):
        action (str):
        method (str):
        path (str):
        status_code (int):
        detail (AuditLogReadDetailType0 | None):
        created_at (datetime.datetime):
        target (None | str | Unset): What the action was done to, named when it happened (an org, a user's email, a
            machine). None when the action names no single target.
    """

    id: UUID
    actor_id: None | UUID
    actor_email: str
    actor_platform_role: None | str
    action: str
    method: str
    path: str
    status_code: int
    detail: AuditLogReadDetailType0 | None
    created_at: datetime.datetime
    target: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.audit_log_read_detail_type_0 import AuditLogReadDetailType0

        id = str(self.id)

        actor_id: None | str
        if isinstance(self.actor_id, UUID):
            actor_id = str(self.actor_id)
        else:
            actor_id = self.actor_id

        actor_email = self.actor_email

        actor_platform_role: None | str
        actor_platform_role = self.actor_platform_role

        action = self.action

        method = self.method

        path = self.path

        status_code = self.status_code

        detail: dict[str, Any] | None
        if isinstance(self.detail, AuditLogReadDetailType0):
            detail = self.detail.to_dict()
        else:
            detail = self.detail

        created_at = self.created_at.isoformat()

        target: None | str | Unset
        if isinstance(self.target, Unset):
            target = UNSET
        else:
            target = self.target

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "actor_id": actor_id,
                "actor_email": actor_email,
                "actor_platform_role": actor_platform_role,
                "action": action,
                "method": method,
                "path": path,
                "status_code": status_code,
                "detail": detail,
                "created_at": created_at,
            }
        )
        if target is not UNSET:
            field_dict["target"] = target

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.audit_log_read_detail_type_0 import AuditLogReadDetailType0

        d = dict(src_dict)
        id = UUID(d.pop("id"))

        def _parse_actor_id(data: object) -> None | UUID:
            if data is None:
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                actor_id_type_0 = UUID(data)

                return actor_id_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | UUID, data)

        actor_id = _parse_actor_id(d.pop("actor_id"))

        actor_email = d.pop("actor_email")

        def _parse_actor_platform_role(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        actor_platform_role = _parse_actor_platform_role(d.pop("actor_platform_role"))

        action = d.pop("action")

        method = d.pop("method")

        path = d.pop("path")

        status_code = d.pop("status_code")

        def _parse_detail(data: object) -> AuditLogReadDetailType0 | None:
            if data is None:
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                detail_type_0 = AuditLogReadDetailType0.from_dict(data)

                return detail_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(AuditLogReadDetailType0 | None, data)

        detail = _parse_detail(d.pop("detail"))

        created_at = datetime.datetime.fromisoformat(d.pop("created_at"))

        def _parse_target(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        target = _parse_target(d.pop("target", UNSET))

        audit_log_read = cls(
            id=id,
            actor_id=actor_id,
            actor_email=actor_email,
            actor_platform_role=actor_platform_role,
            action=action,
            method=method,
            path=path,
            status_code=status_code,
            detail=detail,
            created_at=created_at,
            target=target,
        )

        audit_log_read.additional_properties = d
        return audit_log_read

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
