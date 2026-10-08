from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.machine_mint_request_tenancy import MachineMintRequestTenancy
from ..types import UNSET, Unset

T = TypeVar("T", bound="MachineMintRequest")


@_attrs_define
class MachineMintRequest:
    """Mint a credential for a box a platform admin provisioned by hand.

    Attributes:
        label (str):
        instance_type (str):
        provider (str | Unset):  Default: 'ec2'.
        region (str | Unset):  Default: ''.
        tenancy (MachineMintRequestTenancy | Unset):  Default: MachineMintRequestTenancy.DEDICATED.
    """

    label: str
    instance_type: str
    provider: str | Unset = "ec2"
    region: str | Unset = ""
    tenancy: MachineMintRequestTenancy | Unset = MachineMintRequestTenancy.DEDICATED
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        label = self.label

        instance_type = self.instance_type

        provider = self.provider

        region = self.region

        tenancy: str | Unset = UNSET
        if not isinstance(self.tenancy, Unset):
            tenancy = self.tenancy.value

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "label": label,
                "instance_type": instance_type,
            }
        )
        if provider is not UNSET:
            field_dict["provider"] = provider
        if region is not UNSET:
            field_dict["region"] = region
        if tenancy is not UNSET:
            field_dict["tenancy"] = tenancy

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        label = d.pop("label")

        instance_type = d.pop("instance_type")

        provider = d.pop("provider", UNSET)

        region = d.pop("region", UNSET)

        _tenancy = d.pop("tenancy", UNSET)
        tenancy: MachineMintRequestTenancy | Unset
        if isinstance(_tenancy, Unset):
            tenancy = UNSET
        else:
            tenancy = MachineMintRequestTenancy(_tenancy)

        machine_mint_request = cls(
            label=label,
            instance_type=instance_type,
            provider=provider,
            region=region,
            tenancy=tenancy,
        )

        machine_mint_request.additional_properties = d
        return machine_mint_request

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
