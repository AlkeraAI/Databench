from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.deployment_health_report_last_trigger_type_0 import (
    DeploymentHealthReportLastTriggerType0,
)
from ..models.deployment_health_report_mode import DeploymentHealthReportMode
from ..models.deployment_health_report_overall import DeploymentHealthReportOverall

if TYPE_CHECKING:
    from ..models.deployment_health_check_read import DeploymentHealthCheckRead


T = TypeVar("T", bound="DeploymentHealthReport")


@_attrs_define
class DeploymentHealthReport:
    """
    Attributes:
        overall (DeploymentHealthReportOverall):
        checks (list[DeploymentHealthCheckRead]):
        last_run_at (datetime.datetime | None):
        last_trigger (DeploymentHealthReportLastTriggerType0 | None):
        last_duration_ms (int | None):
        mode (DeploymentHealthReportMode):
        backend_version (str):
    """

    overall: DeploymentHealthReportOverall
    checks: list[DeploymentHealthCheckRead]
    last_run_at: datetime.datetime | None
    last_trigger: DeploymentHealthReportLastTriggerType0 | None
    last_duration_ms: int | None
    mode: DeploymentHealthReportMode
    backend_version: str
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        overall = self.overall.value

        checks = []
        for checks_item_data in self.checks:
            checks_item = checks_item_data.to_dict()
            checks.append(checks_item)

        last_run_at: None | str
        if isinstance(self.last_run_at, datetime.datetime):
            last_run_at = self.last_run_at.isoformat()
        else:
            last_run_at = self.last_run_at

        last_trigger: None | str
        if isinstance(self.last_trigger, DeploymentHealthReportLastTriggerType0):
            last_trigger = self.last_trigger.value
        else:
            last_trigger = self.last_trigger

        last_duration_ms: int | None
        last_duration_ms = self.last_duration_ms

        mode = self.mode.value

        backend_version = self.backend_version

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "overall": overall,
                "checks": checks,
                "last_run_at": last_run_at,
                "last_trigger": last_trigger,
                "last_duration_ms": last_duration_ms,
                "mode": mode,
                "backend_version": backend_version,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.deployment_health_check_read import DeploymentHealthCheckRead

        d = dict(src_dict)
        overall = DeploymentHealthReportOverall(d.pop("overall"))

        checks = []
        _checks = d.pop("checks")
        for checks_item_data in _checks:
            checks_item = DeploymentHealthCheckRead.from_dict(checks_item_data)

            checks.append(checks_item)

        def _parse_last_run_at(data: object) -> datetime.datetime | None:
            if data is None:
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                last_run_at_type_0 = datetime.datetime.fromisoformat(data)

                return last_run_at_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None, data)

        last_run_at = _parse_last_run_at(d.pop("last_run_at"))

        def _parse_last_trigger(data: object) -> DeploymentHealthReportLastTriggerType0 | None:
            if data is None:
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                last_trigger_type_0 = DeploymentHealthReportLastTriggerType0(data)

                return last_trigger_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(DeploymentHealthReportLastTriggerType0 | None, data)

        last_trigger = _parse_last_trigger(d.pop("last_trigger"))

        def _parse_last_duration_ms(data: object) -> int | None:
            if data is None:
                return data
            return cast(int | None, data)

        last_duration_ms = _parse_last_duration_ms(d.pop("last_duration_ms"))

        mode = DeploymentHealthReportMode(d.pop("mode"))

        backend_version = d.pop("backend_version")

        deployment_health_report = cls(
            overall=overall,
            checks=checks,
            last_run_at=last_run_at,
            last_trigger=last_trigger,
            last_duration_ms=last_duration_ms,
            mode=mode,
            backend_version=backend_version,
        )

        deployment_health_report.additional_properties = d
        return deployment_health_report

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
