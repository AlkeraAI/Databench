from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.provision_request_provider import ProvisionRequestProvider
from ..models.provision_request_tenancy import ProvisionRequestTenancy
from ..types import UNSET, Unset

T = TypeVar("T", bound="ProvisionRequest")


@_attrs_define
class ProvisionRequest:
    """
    Attributes:
        provider (ProvisionRequestProvider):
        machine_type_code (str):
        storage_gb (int):
        tenancy (ProvisionRequestTenancy):
        org_id (None | str | Unset):
        name (None | str | Unset):
    """

    provider: ProvisionRequestProvider
    machine_type_code: str
    storage_gb: int
    tenancy: ProvisionRequestTenancy
    org_id: None | str | Unset = UNSET
    name: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        provider = self.provider.value

        machine_type_code = self.machine_type_code

        storage_gb = self.storage_gb

        tenancy = self.tenancy.value

        org_id: None | str | Unset
        if isinstance(self.org_id, Unset):
            org_id = UNSET
        else:
            org_id = self.org_id

        name: None | str | Unset
        if isinstance(self.name, Unset):
            name = UNSET
        else:
            name = self.name

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "provider": provider,
                "machine_type_code": machine_type_code,
                "storage_gb": storage_gb,
                "tenancy": tenancy,
            }
        )
        if org_id is not UNSET:
            field_dict["org_id"] = org_id
        if name is not UNSET:
            field_dict["name"] = name

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        provider = ProvisionRequestProvider(d.pop("provider"))

        machine_type_code = d.pop("machine_type_code")

        storage_gb = d.pop("storage_gb")

        tenancy = ProvisionRequestTenancy(d.pop("tenancy"))

        def _parse_org_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        org_id = _parse_org_id(d.pop("org_id", UNSET))

        def _parse_name(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        name = _parse_name(d.pop("name", UNSET))

        provision_request = cls(
            provider=provider,
            machine_type_code=machine_type_code,
            storage_gb=storage_gb,
            tenancy=tenancy,
            org_id=org_id,
            name=name,
        )

        provision_request.additional_properties = d
        return provision_request

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
