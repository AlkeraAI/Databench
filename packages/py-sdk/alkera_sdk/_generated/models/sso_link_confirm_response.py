from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast
from uuid import UUID

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="SsoLinkConfirmResponse")


@_attrs_define
class SsoLinkConfirmResponse:
    """`POST /auth/sso-link/confirm`. ``joined`` is whether the person is now
    an active member of the org; when not, ``message`` says what to do next.

        Attributes:
            linked (bool):
            joined (bool):
            org_team_id (UUID):
            message (None | str | Unset):
    """

    linked: bool
    joined: bool
    org_team_id: UUID
    message: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        linked = self.linked

        joined = self.joined

        org_team_id = str(self.org_team_id)

        message: None | str | Unset
        if isinstance(self.message, Unset):
            message = UNSET
        else:
            message = self.message

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "linked": linked,
                "joined": joined,
                "org_team_id": org_team_id,
            }
        )
        if message is not UNSET:
            field_dict["message"] = message

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        linked = d.pop("linked")

        joined = d.pop("joined")

        org_team_id = UUID(d.pop("org_team_id"))

        def _parse_message(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        message = _parse_message(d.pop("message", UNSET))

        sso_link_confirm_response = cls(
            linked=linked,
            joined=joined,
            org_team_id=org_team_id,
            message=message,
        )

        sso_link_confirm_response.additional_properties = d
        return sso_link_confirm_response

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
