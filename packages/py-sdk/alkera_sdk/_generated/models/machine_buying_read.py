from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.machine_buying_read_reason_type_0 import MachineBuyingReadReasonType0
from ..types import UNSET, Unset

T = TypeVar("T", bound="MachineBuyingRead")


@_attrs_define
class MachineBuyingRead:
    """Whether the caller's org may buy another machine now, without saying
    which plan it is on: ``reason`` is ``plan`` when its plan buys none and
    ``quota`` when it holds as many as it may.

        Attributes:
            can_buy (bool):
            quota (int):
            used (int):
            reason (MachineBuyingReadReasonType0 | None | Unset):
            can_add (bool | Unset):  Default: False.
    """

    can_buy: bool
    quota: int
    used: int
    reason: MachineBuyingReadReasonType0 | None | Unset = UNSET
    can_add: bool | Unset = False
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        can_buy = self.can_buy

        quota = self.quota

        used = self.used

        reason: None | str | Unset
        if isinstance(self.reason, Unset):
            reason = UNSET
        elif isinstance(self.reason, MachineBuyingReadReasonType0):
            reason = self.reason.value
        else:
            reason = self.reason

        can_add = self.can_add

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "can_buy": can_buy,
                "quota": quota,
                "used": used,
            }
        )
        if reason is not UNSET:
            field_dict["reason"] = reason
        if can_add is not UNSET:
            field_dict["can_add"] = can_add

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        can_buy = d.pop("can_buy")

        quota = d.pop("quota")

        used = d.pop("used")

        def _parse_reason(data: object) -> MachineBuyingReadReasonType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                reason_type_0 = MachineBuyingReadReasonType0(data)

                return reason_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(MachineBuyingReadReasonType0 | None | Unset, data)

        reason = _parse_reason(d.pop("reason", UNSET))

        can_add = d.pop("can_add", UNSET)

        machine_buying_read = cls(
            can_buy=can_buy,
            quota=quota,
            used=used,
            reason=reason,
            can_add=can_add,
        )

        machine_buying_read.additional_properties = d
        return machine_buying_read

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
