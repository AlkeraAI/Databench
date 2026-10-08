from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.offering_create_audience import OfferingCreateAudience
from ..models.offering_create_pricing_mode import OfferingCreatePricingMode
from ..types import UNSET, Unset

T = TypeVar("T", bound="OfferingCreate")


@_attrs_define
class OfferingCreate:
    """A new offering. Platform only.

    Attributes:
        machine_type_id (str):
        name (str):
        pricing_mode (OfferingCreatePricingMode):
        storage_gb_default (int):
        storage_gb_max (int):
        description (str | Unset):  Default: ''.
        markup_bps (int | Unset):  Default: 0.
        fixed_rate_per_minute_nanos (int | None | Unset):
        storage_rate_per_gb_month_nanos (int | Unset):  Default: 0.
        region (str | Unset):  Default: ''.
        audience (OfferingCreateAudience | Unset):  Default: OfferingCreateAudience.ALL.
        org_ids (list[str] | Unset):
        purchasable (bool | Unset):  Default: True.
        idle_stop_minutes_default (int | None | Unset):
        sort_order (int | Unset):  Default: 0.
    """

    machine_type_id: str
    name: str
    pricing_mode: OfferingCreatePricingMode
    storage_gb_default: int
    storage_gb_max: int
    description: str | Unset = ""
    markup_bps: int | Unset = 0
    fixed_rate_per_minute_nanos: int | None | Unset = UNSET
    storage_rate_per_gb_month_nanos: int | Unset = 0
    region: str | Unset = ""
    audience: OfferingCreateAudience | Unset = OfferingCreateAudience.ALL
    org_ids: list[str] | Unset = UNSET
    purchasable: bool | Unset = True
    idle_stop_minutes_default: int | None | Unset = UNSET
    sort_order: int | Unset = 0
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        machine_type_id = self.machine_type_id

        name = self.name

        pricing_mode = self.pricing_mode.value

        storage_gb_default = self.storage_gb_default

        storage_gb_max = self.storage_gb_max

        description = self.description

        markup_bps = self.markup_bps

        fixed_rate_per_minute_nanos: int | None | Unset
        if isinstance(self.fixed_rate_per_minute_nanos, Unset):
            fixed_rate_per_minute_nanos = UNSET
        else:
            fixed_rate_per_minute_nanos = self.fixed_rate_per_minute_nanos

        storage_rate_per_gb_month_nanos = self.storage_rate_per_gb_month_nanos

        region = self.region

        audience: str | Unset = UNSET
        if not isinstance(self.audience, Unset):
            audience = self.audience.value

        org_ids: list[str] | Unset = UNSET
        if not isinstance(self.org_ids, Unset):
            org_ids = self.org_ids

        purchasable = self.purchasable

        idle_stop_minutes_default: int | None | Unset
        if isinstance(self.idle_stop_minutes_default, Unset):
            idle_stop_minutes_default = UNSET
        else:
            idle_stop_minutes_default = self.idle_stop_minutes_default

        sort_order = self.sort_order

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "machine_type_id": machine_type_id,
                "name": name,
                "pricing_mode": pricing_mode,
                "storage_gb_default": storage_gb_default,
                "storage_gb_max": storage_gb_max,
            }
        )
        if description is not UNSET:
            field_dict["description"] = description
        if markup_bps is not UNSET:
            field_dict["markup_bps"] = markup_bps
        if fixed_rate_per_minute_nanos is not UNSET:
            field_dict["fixed_rate_per_minute_nanos"] = fixed_rate_per_minute_nanos
        if storage_rate_per_gb_month_nanos is not UNSET:
            field_dict["storage_rate_per_gb_month_nanos"] = storage_rate_per_gb_month_nanos
        if region is not UNSET:
            field_dict["region"] = region
        if audience is not UNSET:
            field_dict["audience"] = audience
        if org_ids is not UNSET:
            field_dict["org_ids"] = org_ids
        if purchasable is not UNSET:
            field_dict["purchasable"] = purchasable
        if idle_stop_minutes_default is not UNSET:
            field_dict["idle_stop_minutes_default"] = idle_stop_minutes_default
        if sort_order is not UNSET:
            field_dict["sort_order"] = sort_order

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        machine_type_id = d.pop("machine_type_id")

        name = d.pop("name")

        pricing_mode = OfferingCreatePricingMode(d.pop("pricing_mode"))

        storage_gb_default = d.pop("storage_gb_default")

        storage_gb_max = d.pop("storage_gb_max")

        description = d.pop("description", UNSET)

        markup_bps = d.pop("markup_bps", UNSET)

        def _parse_fixed_rate_per_minute_nanos(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        fixed_rate_per_minute_nanos = _parse_fixed_rate_per_minute_nanos(
            d.pop("fixed_rate_per_minute_nanos", UNSET)
        )

        storage_rate_per_gb_month_nanos = d.pop("storage_rate_per_gb_month_nanos", UNSET)

        region = d.pop("region", UNSET)

        _audience = d.pop("audience", UNSET)
        audience: OfferingCreateAudience | Unset
        if isinstance(_audience, Unset):
            audience = UNSET
        else:
            audience = OfferingCreateAudience(_audience)

        org_ids = cast(list[str], d.pop("org_ids", UNSET))

        purchasable = d.pop("purchasable", UNSET)

        def _parse_idle_stop_minutes_default(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        idle_stop_minutes_default = _parse_idle_stop_minutes_default(
            d.pop("idle_stop_minutes_default", UNSET)
        )

        sort_order = d.pop("sort_order", UNSET)

        offering_create = cls(
            machine_type_id=machine_type_id,
            name=name,
            pricing_mode=pricing_mode,
            storage_gb_default=storage_gb_default,
            storage_gb_max=storage_gb_max,
            description=description,
            markup_bps=markup_bps,
            fixed_rate_per_minute_nanos=fixed_rate_per_minute_nanos,
            storage_rate_per_gb_month_nanos=storage_rate_per_gb_month_nanos,
            region=region,
            audience=audience,
            org_ids=org_ids,
            purchasable=purchasable,
            idle_stop_minutes_default=idle_stop_minutes_default,
            sort_order=sort_order,
        )

        offering_create.additional_properties = d
        return offering_create

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
