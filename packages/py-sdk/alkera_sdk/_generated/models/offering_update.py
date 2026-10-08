from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.offering_update_audience_type_0 import OfferingUpdateAudienceType0
from ..models.offering_update_pricing_mode_type_0 import OfferingUpdatePricingModeType0
from ..types import UNSET, Unset

T = TypeVar("T", bound="OfferingUpdate")


@_attrs_define
class OfferingUpdate:
    """A change to an offering; fields not sent are left as they are.
    ``retired`` true retires it, false brings it back. Platform only.

        Attributes:
            machine_type_id (None | str | Unset):
            name (None | str | Unset):
            description (None | str | Unset):
            pricing_mode (None | OfferingUpdatePricingModeType0 | Unset):
            markup_bps (int | None | Unset):
            fixed_rate_per_minute_nanos (int | None | Unset):
            storage_gb_default (int | None | Unset):
            storage_gb_max (int | None | Unset):
            storage_rate_per_gb_month_nanos (int | None | Unset):
            region (None | str | Unset):
            audience (None | OfferingUpdateAudienceType0 | Unset):
            org_ids (list[str] | None | Unset):
            purchasable (bool | None | Unset):
            idle_stop_minutes_default (int | None | Unset):
            sort_order (int | None | Unset):
            retired (bool | None | Unset):
    """

    machine_type_id: None | str | Unset = UNSET
    name: None | str | Unset = UNSET
    description: None | str | Unset = UNSET
    pricing_mode: None | OfferingUpdatePricingModeType0 | Unset = UNSET
    markup_bps: int | None | Unset = UNSET
    fixed_rate_per_minute_nanos: int | None | Unset = UNSET
    storage_gb_default: int | None | Unset = UNSET
    storage_gb_max: int | None | Unset = UNSET
    storage_rate_per_gb_month_nanos: int | None | Unset = UNSET
    region: None | str | Unset = UNSET
    audience: None | OfferingUpdateAudienceType0 | Unset = UNSET
    org_ids: list[str] | None | Unset = UNSET
    purchasable: bool | None | Unset = UNSET
    idle_stop_minutes_default: int | None | Unset = UNSET
    sort_order: int | None | Unset = UNSET
    retired: bool | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        machine_type_id: None | str | Unset
        if isinstance(self.machine_type_id, Unset):
            machine_type_id = UNSET
        else:
            machine_type_id = self.machine_type_id

        name: None | str | Unset
        if isinstance(self.name, Unset):
            name = UNSET
        else:
            name = self.name

        description: None | str | Unset
        if isinstance(self.description, Unset):
            description = UNSET
        else:
            description = self.description

        pricing_mode: None | str | Unset
        if isinstance(self.pricing_mode, Unset):
            pricing_mode = UNSET
        elif isinstance(self.pricing_mode, OfferingUpdatePricingModeType0):
            pricing_mode = self.pricing_mode.value
        else:
            pricing_mode = self.pricing_mode

        markup_bps: int | None | Unset
        if isinstance(self.markup_bps, Unset):
            markup_bps = UNSET
        else:
            markup_bps = self.markup_bps

        fixed_rate_per_minute_nanos: int | None | Unset
        if isinstance(self.fixed_rate_per_minute_nanos, Unset):
            fixed_rate_per_minute_nanos = UNSET
        else:
            fixed_rate_per_minute_nanos = self.fixed_rate_per_minute_nanos

        storage_gb_default: int | None | Unset
        if isinstance(self.storage_gb_default, Unset):
            storage_gb_default = UNSET
        else:
            storage_gb_default = self.storage_gb_default

        storage_gb_max: int | None | Unset
        if isinstance(self.storage_gb_max, Unset):
            storage_gb_max = UNSET
        else:
            storage_gb_max = self.storage_gb_max

        storage_rate_per_gb_month_nanos: int | None | Unset
        if isinstance(self.storage_rate_per_gb_month_nanos, Unset):
            storage_rate_per_gb_month_nanos = UNSET
        else:
            storage_rate_per_gb_month_nanos = self.storage_rate_per_gb_month_nanos

        region: None | str | Unset
        if isinstance(self.region, Unset):
            region = UNSET
        else:
            region = self.region

        audience: None | str | Unset
        if isinstance(self.audience, Unset):
            audience = UNSET
        elif isinstance(self.audience, OfferingUpdateAudienceType0):
            audience = self.audience.value
        else:
            audience = self.audience

        org_ids: list[str] | None | Unset
        if isinstance(self.org_ids, Unset):
            org_ids = UNSET
        elif isinstance(self.org_ids, list):
            org_ids = self.org_ids

        else:
            org_ids = self.org_ids

        purchasable: bool | None | Unset
        if isinstance(self.purchasable, Unset):
            purchasable = UNSET
        else:
            purchasable = self.purchasable

        idle_stop_minutes_default: int | None | Unset
        if isinstance(self.idle_stop_minutes_default, Unset):
            idle_stop_minutes_default = UNSET
        else:
            idle_stop_minutes_default = self.idle_stop_minutes_default

        sort_order: int | None | Unset
        if isinstance(self.sort_order, Unset):
            sort_order = UNSET
        else:
            sort_order = self.sort_order

        retired: bool | None | Unset
        if isinstance(self.retired, Unset):
            retired = UNSET
        else:
            retired = self.retired

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if machine_type_id is not UNSET:
            field_dict["machine_type_id"] = machine_type_id
        if name is not UNSET:
            field_dict["name"] = name
        if description is not UNSET:
            field_dict["description"] = description
        if pricing_mode is not UNSET:
            field_dict["pricing_mode"] = pricing_mode
        if markup_bps is not UNSET:
            field_dict["markup_bps"] = markup_bps
        if fixed_rate_per_minute_nanos is not UNSET:
            field_dict["fixed_rate_per_minute_nanos"] = fixed_rate_per_minute_nanos
        if storage_gb_default is not UNSET:
            field_dict["storage_gb_default"] = storage_gb_default
        if storage_gb_max is not UNSET:
            field_dict["storage_gb_max"] = storage_gb_max
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
        if retired is not UNSET:
            field_dict["retired"] = retired

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)

        def _parse_machine_type_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        machine_type_id = _parse_machine_type_id(d.pop("machine_type_id", UNSET))

        def _parse_name(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        name = _parse_name(d.pop("name", UNSET))

        def _parse_description(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        description = _parse_description(d.pop("description", UNSET))

        def _parse_pricing_mode(data: object) -> None | OfferingUpdatePricingModeType0 | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                pricing_mode_type_0 = OfferingUpdatePricingModeType0(data)

                return pricing_mode_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | OfferingUpdatePricingModeType0 | Unset, data)

        pricing_mode = _parse_pricing_mode(d.pop("pricing_mode", UNSET))

        def _parse_markup_bps(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        markup_bps = _parse_markup_bps(d.pop("markup_bps", UNSET))

        def _parse_fixed_rate_per_minute_nanos(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        fixed_rate_per_minute_nanos = _parse_fixed_rate_per_minute_nanos(
            d.pop("fixed_rate_per_minute_nanos", UNSET)
        )

        def _parse_storage_gb_default(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        storage_gb_default = _parse_storage_gb_default(d.pop("storage_gb_default", UNSET))

        def _parse_storage_gb_max(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        storage_gb_max = _parse_storage_gb_max(d.pop("storage_gb_max", UNSET))

        def _parse_storage_rate_per_gb_month_nanos(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        storage_rate_per_gb_month_nanos = _parse_storage_rate_per_gb_month_nanos(
            d.pop("storage_rate_per_gb_month_nanos", UNSET)
        )

        def _parse_region(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        region = _parse_region(d.pop("region", UNSET))

        def _parse_audience(data: object) -> None | OfferingUpdateAudienceType0 | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                audience_type_0 = OfferingUpdateAudienceType0(data)

                return audience_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | OfferingUpdateAudienceType0 | Unset, data)

        audience = _parse_audience(d.pop("audience", UNSET))

        def _parse_org_ids(data: object) -> list[str] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                org_ids_type_0 = cast(list[str], data)

                return org_ids_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[str] | None | Unset, data)

        org_ids = _parse_org_ids(d.pop("org_ids", UNSET))

        def _parse_purchasable(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        purchasable = _parse_purchasable(d.pop("purchasable", UNSET))

        def _parse_idle_stop_minutes_default(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        idle_stop_minutes_default = _parse_idle_stop_minutes_default(
            d.pop("idle_stop_minutes_default", UNSET)
        )

        def _parse_sort_order(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        sort_order = _parse_sort_order(d.pop("sort_order", UNSET))

        def _parse_retired(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        retired = _parse_retired(d.pop("retired", UNSET))

        offering_update = cls(
            machine_type_id=machine_type_id,
            name=name,
            description=description,
            pricing_mode=pricing_mode,
            markup_bps=markup_bps,
            fixed_rate_per_minute_nanos=fixed_rate_per_minute_nanos,
            storage_gb_default=storage_gb_default,
            storage_gb_max=storage_gb_max,
            storage_rate_per_gb_month_nanos=storage_rate_per_gb_month_nanos,
            region=region,
            audience=audience,
            org_ids=org_ids,
            purchasable=purchasable,
            idle_stop_minutes_default=idle_stop_minutes_default,
            sort_order=sort_order,
            retired=retired,
        )

        offering_update.additional_properties = d
        return offering_update

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
