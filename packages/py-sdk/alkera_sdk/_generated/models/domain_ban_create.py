from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="DomainBanCreate")


@_attrs_define
class DomainBanCreate:
    """``domain`` is normalized on the way in — lower-cased, surrounding
    whitespace and a leading ``@`` dropped — and refused unless what remains is
    a bare hostname, so a full address or a URL is a 422 rather than a ban that
    matches nobody.

        Attributes:
            domain (str):
            reason (str | Unset):  Default: ''.
    """

    domain: str
    reason: str | Unset = ""
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        domain = self.domain

        reason = self.reason

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "domain": domain,
            }
        )
        if reason is not UNSET:
            field_dict["reason"] = reason

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        domain = d.pop("domain")

        reason = d.pop("reason", UNSET)

        domain_ban_create = cls(
            domain=domain,
            reason=reason,
        )

        domain_ban_create.additional_properties = d
        return domain_ban_create

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
