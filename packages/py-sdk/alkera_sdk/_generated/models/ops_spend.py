from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

T = TypeVar("T", bound="OpsSpend")


@_attrs_define
class OpsSpend:
    """
    Attributes:
        today_nanos (int):
        month_to_date_nanos (int):
        monthly_cap_nanos (int | None):
        cap_headroom_nanos (int | None):
    """

    today_nanos: int
    month_to_date_nanos: int
    monthly_cap_nanos: int | None
    cap_headroom_nanos: int | None
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        today_nanos = self.today_nanos

        month_to_date_nanos = self.month_to_date_nanos

        monthly_cap_nanos: int | None
        monthly_cap_nanos = self.monthly_cap_nanos

        cap_headroom_nanos: int | None
        cap_headroom_nanos = self.cap_headroom_nanos

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "today_nanos": today_nanos,
                "month_to_date_nanos": month_to_date_nanos,
                "monthly_cap_nanos": monthly_cap_nanos,
                "cap_headroom_nanos": cap_headroom_nanos,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        today_nanos = d.pop("today_nanos")

        month_to_date_nanos = d.pop("month_to_date_nanos")

        def _parse_monthly_cap_nanos(data: object) -> int | None:
            if data is None:
                return data
            return cast(int | None, data)

        monthly_cap_nanos = _parse_monthly_cap_nanos(d.pop("monthly_cap_nanos"))

        def _parse_cap_headroom_nanos(data: object) -> int | None:
            if data is None:
                return data
            return cast(int | None, data)

        cap_headroom_nanos = _parse_cap_headroom_nanos(d.pop("cap_headroom_nanos"))

        ops_spend = cls(
            today_nanos=today_nanos,
            month_to_date_nanos=month_to_date_nanos,
            monthly_cap_nanos=monthly_cap_nanos,
            cap_headroom_nanos=cap_headroom_nanos,
        )

        ops_spend.additional_properties = d
        return ops_spend

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
