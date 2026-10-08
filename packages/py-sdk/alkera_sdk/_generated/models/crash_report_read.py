from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast
from uuid import UUID

from attrs import define as _attrs_define
from attrs import field as _attrs_field

if TYPE_CHECKING:
    from ..models.crash_report_read_context_type_0 import CrashReportReadContextType0


T = TypeVar("T", bound="CrashReportRead")


@_attrs_define
class CrashReportRead:
    """Full stored crash report (returned to the submitter on create / detail).

    Attributes:
        id (UUID):
        user_id (UUID):
        org_team_id (UUID):
        component (str):
        error_type (None | str):
        message (str):
        stacktrace (None | str):
        context (CrashReportReadContextType0 | None):
        comment (None | str):
        app_version (None | str):
        platform (None | str):
        occurred_at (datetime.datetime | None):
        created_at (datetime.datetime):
    """

    id: UUID
    user_id: UUID
    org_team_id: UUID
    component: str
    error_type: None | str
    message: str
    stacktrace: None | str
    context: CrashReportReadContextType0 | None
    comment: None | str
    app_version: None | str
    platform: None | str
    occurred_at: datetime.datetime | None
    created_at: datetime.datetime
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.crash_report_read_context_type_0 import (
            CrashReportReadContextType0,
        )

        id = str(self.id)

        user_id = str(self.user_id)

        org_team_id = str(self.org_team_id)

        component = self.component

        error_type: None | str
        error_type = self.error_type

        message = self.message

        stacktrace: None | str
        stacktrace = self.stacktrace

        context: dict[str, Any] | None
        if isinstance(self.context, CrashReportReadContextType0):
            context = self.context.to_dict()
        else:
            context = self.context

        comment: None | str
        comment = self.comment

        app_version: None | str
        app_version = self.app_version

        platform: None | str
        platform = self.platform

        occurred_at: None | str
        if isinstance(self.occurred_at, datetime.datetime):
            occurred_at = self.occurred_at.isoformat()
        else:
            occurred_at = self.occurred_at

        created_at = self.created_at.isoformat()

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "user_id": user_id,
                "org_team_id": org_team_id,
                "component": component,
                "error_type": error_type,
                "message": message,
                "stacktrace": stacktrace,
                "context": context,
                "comment": comment,
                "app_version": app_version,
                "platform": platform,
                "occurred_at": occurred_at,
                "created_at": created_at,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.crash_report_read_context_type_0 import (
            CrashReportReadContextType0,
        )

        d = dict(src_dict)
        id = UUID(d.pop("id"))

        user_id = UUID(d.pop("user_id"))

        org_team_id = UUID(d.pop("org_team_id"))

        component = d.pop("component")

        def _parse_error_type(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        error_type = _parse_error_type(d.pop("error_type"))

        message = d.pop("message")

        def _parse_stacktrace(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        stacktrace = _parse_stacktrace(d.pop("stacktrace"))

        def _parse_context(data: object) -> CrashReportReadContextType0 | None:
            if data is None:
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                context_type_0 = CrashReportReadContextType0.from_dict(data)

                return context_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(CrashReportReadContextType0 | None, data)

        context = _parse_context(d.pop("context"))

        def _parse_comment(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        comment = _parse_comment(d.pop("comment"))

        def _parse_app_version(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        app_version = _parse_app_version(d.pop("app_version"))

        def _parse_platform(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        platform = _parse_platform(d.pop("platform"))

        def _parse_occurred_at(data: object) -> datetime.datetime | None:
            if data is None:
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                occurred_at_type_0 = datetime.datetime.fromisoformat(data)

                return occurred_at_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None, data)

        occurred_at = _parse_occurred_at(d.pop("occurred_at"))

        created_at = datetime.datetime.fromisoformat(d.pop("created_at"))

        crash_report_read = cls(
            id=id,
            user_id=user_id,
            org_team_id=org_team_id,
            component=component,
            error_type=error_type,
            message=message,
            stacktrace=stacktrace,
            context=context,
            comment=comment,
            app_version=app_version,
            platform=platform,
            occurred_at=occurred_at,
            created_at=created_at,
        )

        crash_report_read.additional_properties = d
        return crash_report_read

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
