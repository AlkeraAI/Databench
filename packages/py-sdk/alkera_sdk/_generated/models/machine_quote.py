from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.machine_quote_verdict import MachineQuoteVerdict
from ..types import UNSET, Unset

T = TypeVar("T", bound="MachineQuote")


@_attrs_define
class MachineQuote:
    """What buying an offering at a size would cost the caller's org, and
    whether it would be admitted now: the purchase's own rules, decided by the
    server.

        Attributes:
            offering_id (str):
            storage_gb (int):
            rate_per_minute_nanos (int):
            storage_rate_per_minute_nanos (int):
            storage_per_month_nanos (int):
            volume_billed_while_stopped (bool):
            start_runway_nanos (int):
            verdict (MachineQuoteVerdict):
            code (None | str | Unset):
            message (str | Unset):  Default: ''.
            priced (bool | Unset):  Default: False.
    """

    offering_id: str
    storage_gb: int
    rate_per_minute_nanos: int
    storage_rate_per_minute_nanos: int
    storage_per_month_nanos: int
    volume_billed_while_stopped: bool
    start_runway_nanos: int
    verdict: MachineQuoteVerdict
    code: None | str | Unset = UNSET
    message: str | Unset = ""
    priced: bool | Unset = False
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        offering_id = self.offering_id

        storage_gb = self.storage_gb

        rate_per_minute_nanos = self.rate_per_minute_nanos

        storage_rate_per_minute_nanos = self.storage_rate_per_minute_nanos

        storage_per_month_nanos = self.storage_per_month_nanos

        volume_billed_while_stopped = self.volume_billed_while_stopped

        start_runway_nanos = self.start_runway_nanos

        verdict = self.verdict.value

        code: None | str | Unset
        if isinstance(self.code, Unset):
            code = UNSET
        else:
            code = self.code

        message = self.message

        priced = self.priced

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "offering_id": offering_id,
                "storage_gb": storage_gb,
                "rate_per_minute_nanos": rate_per_minute_nanos,
                "storage_rate_per_minute_nanos": storage_rate_per_minute_nanos,
                "storage_per_month_nanos": storage_per_month_nanos,
                "volume_billed_while_stopped": volume_billed_while_stopped,
                "start_runway_nanos": start_runway_nanos,
                "verdict": verdict,
            }
        )
        if code is not UNSET:
            field_dict["code"] = code
        if message is not UNSET:
            field_dict["message"] = message
        if priced is not UNSET:
            field_dict["priced"] = priced

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        offering_id = d.pop("offering_id")

        storage_gb = d.pop("storage_gb")

        rate_per_minute_nanos = d.pop("rate_per_minute_nanos")

        storage_rate_per_minute_nanos = d.pop("storage_rate_per_minute_nanos")

        storage_per_month_nanos = d.pop("storage_per_month_nanos")

        volume_billed_while_stopped = d.pop("volume_billed_while_stopped")

        start_runway_nanos = d.pop("start_runway_nanos")

        verdict = MachineQuoteVerdict(d.pop("verdict"))

        def _parse_code(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        code = _parse_code(d.pop("code", UNSET))

        message = d.pop("message", UNSET)

        priced = d.pop("priced", UNSET)

        machine_quote = cls(
            offering_id=offering_id,
            storage_gb=storage_gb,
            rate_per_minute_nanos=rate_per_minute_nanos,
            storage_rate_per_minute_nanos=storage_rate_per_minute_nanos,
            storage_per_month_nanos=storage_per_month_nanos,
            volume_billed_while_stopped=volume_billed_while_stopped,
            start_runway_nanos=start_runway_nanos,
            verdict=verdict,
            code=code,
            message=message,
            priced=priced,
        )

        machine_quote.additional_properties = d
        return machine_quote

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
