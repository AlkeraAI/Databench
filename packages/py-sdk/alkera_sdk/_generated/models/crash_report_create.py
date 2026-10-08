from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.crash_report_create_component import CrashReportCreateComponent
from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.crash_report_create_context_type_0 import CrashReportCreateContextType0


T = TypeVar("T", bound="CrashReportCreate")


@_attrs_define
class CrashReportCreate:
    """An opt-in crash report submitted by a user (with their consent).

    Attributes:
        component (CrashReportCreateComponent):
        message (str):
        error_type (None | str | Unset):
        stacktrace (None | str | Unset):
        context (CrashReportCreateContextType0 | None | Unset):
        comment (None | str | Unset):
        app_version (None | str | Unset):
        platform (None | str | Unset):
        logs (None | str | Unset):
        occurred_at (datetime.datetime | None | Unset):
    """

    component: CrashReportCreateComponent
    message: str
    error_type: None | str | Unset = UNSET
    stacktrace: None | str | Unset = UNSET
    context: CrashReportCreateContextType0 | None | Unset = UNSET
    comment: None | str | Unset = UNSET
    app_version: None | str | Unset = UNSET
    platform: None | str | Unset = UNSET
    logs: None | str | Unset = UNSET
    occurred_at: datetime.datetime | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.crash_report_create_context_type_0 import (
            CrashReportCreateContextType0,
        )

        component = self.component.value

        message = self.message

        error_type: None | str | Unset
        if isinstance(self.error_type, Unset):
            error_type = UNSET
        else:
            error_type = self.error_type

        stacktrace: None | str | Unset
        if isinstance(self.stacktrace, Unset):
            stacktrace = UNSET
        else:
            stacktrace = self.stacktrace

        context: dict[str, Any] | None | Unset
        if isinstance(self.context, Unset):
            context = UNSET
        elif isinstance(self.context, CrashReportCreateContextType0):
            context = self.context.to_dict()
        else:
            context = self.context

        comment: None | str | Unset
        if isinstance(self.comment, Unset):
            comment = UNSET
        else:
            comment = self.comment

        app_version: None | str | Unset
        if isinstance(self.app_version, Unset):
            app_version = UNSET
        else:
            app_version = self.app_version

        platform: None | str | Unset
        if isinstance(self.platform, Unset):
            platform = UNSET
        else:
            platform = self.platform

        logs: None | str | Unset
        if isinstance(self.logs, Unset):
            logs = UNSET
        else:
            logs = self.logs

        occurred_at: None | str | Unset
        if isinstance(self.occurred_at, Unset):
            occurred_at = UNSET
        elif isinstance(self.occurred_at, datetime.datetime):
            occurred_at = self.occurred_at.isoformat()
        else:
            occurred_at = self.occurred_at

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "component": component,
                "message": message,
            }
        )
        if error_type is not UNSET:
            field_dict["error_type"] = error_type
        if stacktrace is not UNSET:
            field_dict["stacktrace"] = stacktrace
        if context is not UNSET:
            field_dict["context"] = context
        if comment is not UNSET:
            field_dict["comment"] = comment
        if app_version is not UNSET:
            field_dict["app_version"] = app_version
        if platform is not UNSET:
            field_dict["platform"] = platform
        if logs is not UNSET:
            field_dict["logs"] = logs
        if occurred_at is not UNSET:
            field_dict["occurred_at"] = occurred_at

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.crash_report_create_context_type_0 import (
            CrashReportCreateContextType0,
        )

        d = dict(src_dict)
        component = CrashReportCreateComponent(d.pop("component"))

        message = d.pop("message")

        def _parse_error_type(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        error_type = _parse_error_type(d.pop("error_type", UNSET))

        def _parse_stacktrace(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        stacktrace = _parse_stacktrace(d.pop("stacktrace", UNSET))

        def _parse_context(data: object) -> CrashReportCreateContextType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                context_type_0 = CrashReportCreateContextType0.from_dict(data)

                return context_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(CrashReportCreateContextType0 | None | Unset, data)

        context = _parse_context(d.pop("context", UNSET))

        def _parse_comment(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        comment = _parse_comment(d.pop("comment", UNSET))

        def _parse_app_version(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        app_version = _parse_app_version(d.pop("app_version", UNSET))

        def _parse_platform(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        platform = _parse_platform(d.pop("platform", UNSET))

        def _parse_logs(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        logs = _parse_logs(d.pop("logs", UNSET))

        def _parse_occurred_at(data: object) -> datetime.datetime | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                occurred_at_type_0 = datetime.datetime.fromisoformat(data)

                return occurred_at_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None | Unset, data)

        occurred_at = _parse_occurred_at(d.pop("occurred_at", UNSET))

        crash_report_create = cls(
            component=component,
            message=message,
            error_type=error_type,
            stacktrace=stacktrace,
            context=context,
            comment=comment,
            app_version=app_version,
            platform=platform,
            logs=logs,
            occurred_at=occurred_at,
        )

        crash_report_create.additional_properties = d
        return crash_report_create

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
