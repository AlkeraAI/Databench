from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

if TYPE_CHECKING:
    from ..models.ops_count import OpsCount
    from ..models.ops_health import OpsHealth
    from ..models.ops_machine import OpsMachine
    from ..models.ops_spend import OpsSpend


T = TypeVar("T", bound="OpsSummary")


@_attrs_define
class OpsSummary:
    """
    Attributes:
        machines (list[OpsMachine]):
        machines_by_state (list[OpsCount]):
        machines_by_provider (list[OpsCount]):
        chats_served_total (int):
        spend (OpsSpend):
        crash_reports_24h (int):
        crash_reports_unread (int):
        health (OpsHealth):
        build_id (None | str):
        app_version (str):
    """

    machines: list[OpsMachine]
    machines_by_state: list[OpsCount]
    machines_by_provider: list[OpsCount]
    chats_served_total: int
    spend: OpsSpend
    crash_reports_24h: int
    crash_reports_unread: int
    health: OpsHealth
    build_id: None | str
    app_version: str
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        machines = []
        for machines_item_data in self.machines:
            machines_item = machines_item_data.to_dict()
            machines.append(machines_item)

        machines_by_state = []
        for machines_by_state_item_data in self.machines_by_state:
            machines_by_state_item = machines_by_state_item_data.to_dict()
            machines_by_state.append(machines_by_state_item)

        machines_by_provider = []
        for machines_by_provider_item_data in self.machines_by_provider:
            machines_by_provider_item = machines_by_provider_item_data.to_dict()
            machines_by_provider.append(machines_by_provider_item)

        chats_served_total = self.chats_served_total

        spend = self.spend.to_dict()

        crash_reports_24h = self.crash_reports_24h

        crash_reports_unread = self.crash_reports_unread

        health = self.health.to_dict()

        build_id: None | str
        build_id = self.build_id

        app_version = self.app_version

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "machines": machines,
                "machines_by_state": machines_by_state,
                "machines_by_provider": machines_by_provider,
                "chats_served_total": chats_served_total,
                "spend": spend,
                "crash_reports_24h": crash_reports_24h,
                "crash_reports_unread": crash_reports_unread,
                "health": health,
                "build_id": build_id,
                "app_version": app_version,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.ops_count import OpsCount
        from ..models.ops_health import OpsHealth
        from ..models.ops_machine import OpsMachine
        from ..models.ops_spend import OpsSpend

        d = dict(src_dict)
        machines = []
        _machines = d.pop("machines")
        for machines_item_data in _machines:
            machines_item = OpsMachine.from_dict(machines_item_data)

            machines.append(machines_item)

        machines_by_state = []
        _machines_by_state = d.pop("machines_by_state")
        for machines_by_state_item_data in _machines_by_state:
            machines_by_state_item = OpsCount.from_dict(machines_by_state_item_data)

            machines_by_state.append(machines_by_state_item)

        machines_by_provider = []
        _machines_by_provider = d.pop("machines_by_provider")
        for machines_by_provider_item_data in _machines_by_provider:
            machines_by_provider_item = OpsCount.from_dict(machines_by_provider_item_data)

            machines_by_provider.append(machines_by_provider_item)

        chats_served_total = d.pop("chats_served_total")

        spend = OpsSpend.from_dict(d.pop("spend"))

        crash_reports_24h = d.pop("crash_reports_24h")

        crash_reports_unread = d.pop("crash_reports_unread")

        health = OpsHealth.from_dict(d.pop("health"))

        def _parse_build_id(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        build_id = _parse_build_id(d.pop("build_id"))

        app_version = d.pop("app_version")

        ops_summary = cls(
            machines=machines,
            machines_by_state=machines_by_state,
            machines_by_provider=machines_by_provider,
            chats_served_total=chats_served_total,
            spend=spend,
            crash_reports_24h=crash_reports_24h,
            crash_reports_unread=crash_reports_unread,
            health=health,
            build_id=build_id,
            app_version=app_version,
        )

        ops_summary.additional_properties = d
        return ops_summary

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
