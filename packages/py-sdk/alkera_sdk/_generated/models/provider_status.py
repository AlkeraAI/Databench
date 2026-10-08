from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.provider_status_kind import ProviderStatusKind
from ..types import UNSET, Unset

T = TypeVar("T", bound="ProviderStatus")


@_attrs_define
class ProviderStatus:
    """Whether this deployment can start machines at one provider, and, when it
    cannot, the settings it is missing.

        Attributes:
            kind (ProviderStatusKind):
            configured (bool):
            reason (str | Unset):  Default: ''.
    """

    kind: ProviderStatusKind
    configured: bool
    reason: str | Unset = ""
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        kind = self.kind.value

        configured = self.configured

        reason = self.reason

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "kind": kind,
                "configured": configured,
            }
        )
        if reason is not UNSET:
            field_dict["reason"] = reason

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        kind = ProviderStatusKind(d.pop("kind"))

        configured = d.pop("configured")

        reason = d.pop("reason", UNSET)

        provider_status = cls(
            kind=kind,
            configured=configured,
            reason=reason,
        )

        provider_status.additional_properties = d
        return provider_status

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
