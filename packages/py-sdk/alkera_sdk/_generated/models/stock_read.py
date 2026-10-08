from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.stock_read_state import StockReadState
from ..types import UNSET, Unset

T = TypeVar("T", bound="StockRead")


@_attrs_define
class StockRead:
    """Whether an offering can be bought now, as the server decided it
    (``alkera_core.compute.stock.offer_verdict``): the stock of the size it
    sells, and a sentence saying why not when it cannot. Clients render it.

        Attributes:
            state (StockReadState):
            can_buy (bool):
            reason (str | Unset):  Default: ''.
    """

    state: StockReadState
    can_buy: bool
    reason: str | Unset = ""
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        state = self.state.value

        can_buy = self.can_buy

        reason = self.reason

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "state": state,
                "can_buy": can_buy,
            }
        )
        if reason is not UNSET:
            field_dict["reason"] = reason

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        state = StockReadState(d.pop("state"))

        can_buy = d.pop("can_buy")

        reason = d.pop("reason", UNSET)

        stock_read = cls(
            state=state,
            can_buy=can_buy,
            reason=reason,
        )

        stock_read.additional_properties = d
        return stock_read

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
