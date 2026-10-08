from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.org_machine_grant_use_mode import OrgMachineGrantUseMode
from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.audience_grant import AudienceGrant


T = TypeVar("T", bound="OrgMachineGrant")


@_attrs_define
class OrgMachineGrant:
    """A machine Alkera gives an org, free until ``free_until``. Platform only.

    Attributes:
        offering_id (str):
        name (str):
        free_until (datetime.datetime):
        storage_gb (int):
        use_mode (OrgMachineGrantUseMode | Unset):  Default: OrgMachineGrantUseMode.POOL.
        audience (list[AudienceGrant] | Unset):
    """

    offering_id: str
    name: str
    free_until: datetime.datetime
    storage_gb: int
    use_mode: OrgMachineGrantUseMode | Unset = OrgMachineGrantUseMode.POOL
    audience: list[AudienceGrant] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        offering_id = self.offering_id

        name = self.name

        free_until = self.free_until.isoformat()

        storage_gb = self.storage_gb

        use_mode: str | Unset = UNSET
        if not isinstance(self.use_mode, Unset):
            use_mode = self.use_mode.value

        audience: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.audience, Unset):
            audience = []
            for audience_item_data in self.audience:
                audience_item = audience_item_data.to_dict()
                audience.append(audience_item)

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "offering_id": offering_id,
                "name": name,
                "free_until": free_until,
                "storage_gb": storage_gb,
            }
        )
        if use_mode is not UNSET:
            field_dict["use_mode"] = use_mode
        if audience is not UNSET:
            field_dict["audience"] = audience

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.audience_grant import AudienceGrant

        d = dict(src_dict)
        offering_id = d.pop("offering_id")

        name = d.pop("name")

        free_until = datetime.datetime.fromisoformat(d.pop("free_until"))

        storage_gb = d.pop("storage_gb")

        _use_mode = d.pop("use_mode", UNSET)
        use_mode: OrgMachineGrantUseMode | Unset
        if isinstance(_use_mode, Unset):
            use_mode = UNSET
        else:
            use_mode = OrgMachineGrantUseMode(_use_mode)

        _audience = d.pop("audience", UNSET)
        audience: list[AudienceGrant] | Unset = UNSET
        if _audience is not UNSET:
            audience = []
            for audience_item_data in _audience:
                audience_item = AudienceGrant.from_dict(audience_item_data)

                audience.append(audience_item)

        org_machine_grant = cls(
            offering_id=offering_id,
            name=name,
            free_until=free_until,
            storage_gb=storage_gb,
            use_mode=use_mode,
            audience=audience,
        )

        org_machine_grant.additional_properties = d
        return org_machine_grant

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
