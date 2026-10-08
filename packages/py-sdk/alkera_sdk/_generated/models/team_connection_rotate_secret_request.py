from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

T = TypeVar("T", bound="TeamConnectionRotateSecretRequest")


@_attrs_define
class TeamConnectionRotateSecretRequest:
    """Rotate only the primary shared credential.

    Named roles change through the connection edit that owns their endpoint.
    This distinct verb records primary rotations and bumps the bundle version.

        Attributes:
            shared_secret (str):
    """

    shared_secret: str
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        shared_secret = self.shared_secret

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "shared_secret": shared_secret,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        shared_secret = d.pop("shared_secret")

        team_connection_rotate_secret_request = cls(
            shared_secret=shared_secret,
        )

        team_connection_rotate_secret_request.additional_properties = d
        return team_connection_rotate_secret_request

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
