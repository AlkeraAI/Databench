from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.offering_admin_read_audience import OfferingAdminReadAudience
from ..models.offering_admin_read_pricing_mode import OfferingAdminReadPricingMode
from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.disk_choices_read import DiskChoicesRead
    from ..models.gpu_spec import GpuSpec
    from ..models.stock_read import StockRead


T = TypeVar("T", bound="OfferingAdminRead")


@_attrs_define
class OfferingAdminRead:
    """An offering as a platform admin sees it. Platform only.

    Attributes:
        id (str):
        name (str):
        description (str):
        provider (str):
        region (str):
        compute_class (str):
        vcpu (int):
        memory_gb (int):
        rate_per_minute_nanos (int):
        storage_rate_per_gb_month_nanos (int):
        storage_gb_default (int):
        storage_gb_max (int):
        availability (str):
        purchasable (bool):
        stock (StockRead): Whether an offering can be bought now, as the server decided it
            (``alkera_core.compute.stock.offer_verdict``): the stock of the size it
            sells, and a sentence saying why not when it cannot. Clients render it.
        machine_type_id (str):
        pricing_mode (OfferingAdminReadPricingMode):
        markup_bps (int):
        provider_price_per_minute_nanos (int):
        audience (OfferingAdminReadAudience):
        org_ids (list[str]):
        sort_order (int):
        gpu (GpuSpec | None | Unset):
        idle_stop_minutes_default (int | None | Unset):
        start_runway_minutes (int | Unset):  Default: 60.
        disk (DiskChoicesRead | None | Unset):
        priced (bool | Unset):  Default: False.
        fixed_rate_per_minute_nanos (int | None | Unset):
        retired_at (datetime.datetime | None | Unset):
    """

    id: str
    name: str
    description: str
    provider: str
    region: str
    compute_class: str
    vcpu: int
    memory_gb: int
    rate_per_minute_nanos: int
    storage_rate_per_gb_month_nanos: int
    storage_gb_default: int
    storage_gb_max: int
    availability: str
    purchasable: bool
    stock: StockRead
    machine_type_id: str
    pricing_mode: OfferingAdminReadPricingMode
    markup_bps: int
    provider_price_per_minute_nanos: int
    audience: OfferingAdminReadAudience
    org_ids: list[str]
    sort_order: int
    gpu: GpuSpec | None | Unset = UNSET
    idle_stop_minutes_default: int | None | Unset = UNSET
    start_runway_minutes: int | Unset = 60
    disk: DiskChoicesRead | None | Unset = UNSET
    priced: bool | Unset = False
    fixed_rate_per_minute_nanos: int | None | Unset = UNSET
    retired_at: datetime.datetime | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.disk_choices_read import DiskChoicesRead
        from ..models.gpu_spec import GpuSpec

        id = self.id

        name = self.name

        description = self.description

        provider = self.provider

        region = self.region

        compute_class = self.compute_class

        vcpu = self.vcpu

        memory_gb = self.memory_gb

        rate_per_minute_nanos = self.rate_per_minute_nanos

        storage_rate_per_gb_month_nanos = self.storage_rate_per_gb_month_nanos

        storage_gb_default = self.storage_gb_default

        storage_gb_max = self.storage_gb_max

        availability = self.availability

        purchasable = self.purchasable

        stock = self.stock.to_dict()

        machine_type_id = self.machine_type_id

        pricing_mode = self.pricing_mode.value

        markup_bps = self.markup_bps

        provider_price_per_minute_nanos = self.provider_price_per_minute_nanos

        audience = self.audience.value

        org_ids = self.org_ids

        sort_order = self.sort_order

        gpu: dict[str, Any] | None | Unset
        if isinstance(self.gpu, Unset):
            gpu = UNSET
        elif isinstance(self.gpu, GpuSpec):
            gpu = self.gpu.to_dict()
        else:
            gpu = self.gpu

        idle_stop_minutes_default: int | None | Unset
        if isinstance(self.idle_stop_minutes_default, Unset):
            idle_stop_minutes_default = UNSET
        else:
            idle_stop_minutes_default = self.idle_stop_minutes_default

        start_runway_minutes = self.start_runway_minutes

        disk: dict[str, Any] | None | Unset
        if isinstance(self.disk, Unset):
            disk = UNSET
        elif isinstance(self.disk, DiskChoicesRead):
            disk = self.disk.to_dict()
        else:
            disk = self.disk

        priced = self.priced

        fixed_rate_per_minute_nanos: int | None | Unset
        if isinstance(self.fixed_rate_per_minute_nanos, Unset):
            fixed_rate_per_minute_nanos = UNSET
        else:
            fixed_rate_per_minute_nanos = self.fixed_rate_per_minute_nanos

        retired_at: None | str | Unset
        if isinstance(self.retired_at, Unset):
            retired_at = UNSET
        elif isinstance(self.retired_at, datetime.datetime):
            retired_at = self.retired_at.isoformat()
        else:
            retired_at = self.retired_at

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "name": name,
                "description": description,
                "provider": provider,
                "region": region,
                "compute_class": compute_class,
                "vcpu": vcpu,
                "memory_gb": memory_gb,
                "rate_per_minute_nanos": rate_per_minute_nanos,
                "storage_rate_per_gb_month_nanos": storage_rate_per_gb_month_nanos,
                "storage_gb_default": storage_gb_default,
                "storage_gb_max": storage_gb_max,
                "availability": availability,
                "purchasable": purchasable,
                "stock": stock,
                "machine_type_id": machine_type_id,
                "pricing_mode": pricing_mode,
                "markup_bps": markup_bps,
                "provider_price_per_minute_nanos": provider_price_per_minute_nanos,
                "audience": audience,
                "org_ids": org_ids,
                "sort_order": sort_order,
            }
        )
        if gpu is not UNSET:
            field_dict["gpu"] = gpu
        if idle_stop_minutes_default is not UNSET:
            field_dict["idle_stop_minutes_default"] = idle_stop_minutes_default
        if start_runway_minutes is not UNSET:
            field_dict["start_runway_minutes"] = start_runway_minutes
        if disk is not UNSET:
            field_dict["disk"] = disk
        if priced is not UNSET:
            field_dict["priced"] = priced
        if fixed_rate_per_minute_nanos is not UNSET:
            field_dict["fixed_rate_per_minute_nanos"] = fixed_rate_per_minute_nanos
        if retired_at is not UNSET:
            field_dict["retired_at"] = retired_at

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.disk_choices_read import DiskChoicesRead
        from ..models.gpu_spec import GpuSpec
        from ..models.stock_read import StockRead

        d = dict(src_dict)
        id = d.pop("id")

        name = d.pop("name")

        description = d.pop("description")

        provider = d.pop("provider")

        region = d.pop("region")

        compute_class = d.pop("compute_class")

        vcpu = d.pop("vcpu")

        memory_gb = d.pop("memory_gb")

        rate_per_minute_nanos = d.pop("rate_per_minute_nanos")

        storage_rate_per_gb_month_nanos = d.pop("storage_rate_per_gb_month_nanos")

        storage_gb_default = d.pop("storage_gb_default")

        storage_gb_max = d.pop("storage_gb_max")

        availability = d.pop("availability")

        purchasable = d.pop("purchasable")

        stock = StockRead.from_dict(d.pop("stock"))

        machine_type_id = d.pop("machine_type_id")

        pricing_mode = OfferingAdminReadPricingMode(d.pop("pricing_mode"))

        markup_bps = d.pop("markup_bps")

        provider_price_per_minute_nanos = d.pop("provider_price_per_minute_nanos")

        audience = OfferingAdminReadAudience(d.pop("audience"))

        org_ids = cast(list[str], d.pop("org_ids"))

        sort_order = d.pop("sort_order")

        def _parse_gpu(data: object) -> GpuSpec | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                gpu_type_0 = GpuSpec.from_dict(data)

                return gpu_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(GpuSpec | None | Unset, data)

        gpu = _parse_gpu(d.pop("gpu", UNSET))

        def _parse_idle_stop_minutes_default(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        idle_stop_minutes_default = _parse_idle_stop_minutes_default(
            d.pop("idle_stop_minutes_default", UNSET)
        )

        start_runway_minutes = d.pop("start_runway_minutes", UNSET)

        def _parse_disk(data: object) -> DiskChoicesRead | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                disk_type_0 = DiskChoicesRead.from_dict(data)

                return disk_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(DiskChoicesRead | None | Unset, data)

        disk = _parse_disk(d.pop("disk", UNSET))

        priced = d.pop("priced", UNSET)

        def _parse_fixed_rate_per_minute_nanos(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        fixed_rate_per_minute_nanos = _parse_fixed_rate_per_minute_nanos(
            d.pop("fixed_rate_per_minute_nanos", UNSET)
        )

        def _parse_retired_at(data: object) -> datetime.datetime | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                retired_at_type_0 = datetime.datetime.fromisoformat(data)

                return retired_at_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None | Unset, data)

        retired_at = _parse_retired_at(d.pop("retired_at", UNSET))

        offering_admin_read = cls(
            id=id,
            name=name,
            description=description,
            provider=provider,
            region=region,
            compute_class=compute_class,
            vcpu=vcpu,
            memory_gb=memory_gb,
            rate_per_minute_nanos=rate_per_minute_nanos,
            storage_rate_per_gb_month_nanos=storage_rate_per_gb_month_nanos,
            storage_gb_default=storage_gb_default,
            storage_gb_max=storage_gb_max,
            availability=availability,
            purchasable=purchasable,
            stock=stock,
            machine_type_id=machine_type_id,
            pricing_mode=pricing_mode,
            markup_bps=markup_bps,
            provider_price_per_minute_nanos=provider_price_per_minute_nanos,
            audience=audience,
            org_ids=org_ids,
            sort_order=sort_order,
            gpu=gpu,
            idle_stop_minutes_default=idle_stop_minutes_default,
            start_runway_minutes=start_runway_minutes,
            disk=disk,
            priced=priced,
            fixed_rate_per_minute_nanos=fixed_rate_per_minute_nanos,
            retired_at=retired_at,
        )

        offering_admin_read.additional_properties = d
        return offering_admin_read

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
