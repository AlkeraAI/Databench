from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="MachineCost")


@_attrs_define
class MachineCost:
    """
    Attributes:
        price_per_minute_nanos (int | Unset):  Default: 0.
        minutes_billed (int | Unset):  Default: 0.
        true_cost_nanos (int | Unset):  Default: 0.
        currency (Literal['USD'] | Unset):  Default: 'USD'.
    """

    price_per_minute_nanos: int | Unset = 0
    minutes_billed: int | Unset = 0
    true_cost_nanos: int | Unset = 0
    currency: Literal["USD"] | Unset = "USD"
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        price_per_minute_nanos = self.price_per_minute_nanos

        minutes_billed = self.minutes_billed

        true_cost_nanos = self.true_cost_nanos

        currency = self.currency

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if price_per_minute_nanos is not UNSET:
            field_dict["price_per_minute_nanos"] = price_per_minute_nanos
        if minutes_billed is not UNSET:
            field_dict["minutes_billed"] = minutes_billed
        if true_cost_nanos is not UNSET:
            field_dict["true_cost_nanos"] = true_cost_nanos
        if currency is not UNSET:
            field_dict["currency"] = currency

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        price_per_minute_nanos = d.pop("price_per_minute_nanos", UNSET)

        minutes_billed = d.pop("minutes_billed", UNSET)

        true_cost_nanos = d.pop("true_cost_nanos", UNSET)

        currency = cast(Literal["USD"] | Unset, d.pop("currency", UNSET))
        if currency != "USD" and not isinstance(currency, Unset):
            raise ValueError(f"currency must match const 'USD', got '{currency}'")

        machine_cost = cls(
            price_per_minute_nanos=price_per_minute_nanos,
            minutes_billed=minutes_billed,
            true_cost_nanos=true_cost_nanos,
            currency=currency,
        )

        machine_cost.additional_properties = d
        return machine_cost

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
