from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast
from uuid import UUID

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="DeviceApproveRequest")


@_attrs_define
class DeviceApproveRequest:
    """SPA approve body: the user code, and optionally which of the approver's
    orgs the device acts in. Omitted, it is the org the approving browser
    session is in. Checked against the approver's own active memberships; it
    never grants an org a membership does not.

        Attributes:
            user_code (str):
            org_team_id (None | Unset | UUID):
    """

    user_code: str
    org_team_id: None | Unset | UUID = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        user_code = self.user_code

        org_team_id: None | str | Unset
        if isinstance(self.org_team_id, Unset):
            org_team_id = UNSET
        elif isinstance(self.org_team_id, UUID):
            org_team_id = str(self.org_team_id)
        else:
            org_team_id = self.org_team_id

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "user_code": user_code,
            }
        )
        if org_team_id is not UNSET:
            field_dict["org_team_id"] = org_team_id

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        user_code = d.pop("user_code")

        def _parse_org_team_id(data: object) -> None | Unset | UUID:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                org_team_id_type_0 = UUID(data)

                return org_team_id_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | Unset | UUID, data)

        org_team_id = _parse_org_team_id(d.pop("org_team_id", UNSET))

        device_approve_request = cls(
            user_code=user_code,
            org_team_id=org_team_id,
        )

        device_approve_request.additional_properties = d
        return device_approve_request

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
