from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.ops_health_check_status import OpsHealthCheckStatus

T = TypeVar("T", bound="OpsHealthCheck")


@_attrs_define
class OpsHealthCheck:
    """
    Attributes:
        key (str):
        label (str):
        status (OpsHealthCheckStatus):
        detail (str):
    """

    key: str
    label: str
    status: OpsHealthCheckStatus
    detail: str
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        key = self.key

        label = self.label

        status = self.status.value

        detail = self.detail

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "key": key,
                "label": label,
                "status": status,
                "detail": detail,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        key = d.pop("key")

        label = d.pop("label")

        status = OpsHealthCheckStatus(d.pop("status"))

        detail = d.pop("detail")

        ops_health_check = cls(
            key=key,
            label=label,
            status=status,
            detail=detail,
        )

        ops_health_check.additional_properties = d
        return ops_health_check

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
