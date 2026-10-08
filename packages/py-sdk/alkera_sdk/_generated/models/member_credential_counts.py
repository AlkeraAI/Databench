from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="MemberCredentialCounts")


@_attrs_define
class MemberCredentialCounts:
    """How a per-user row's members stand with their own credentials — the one
    number an admin needs to know that the row works for everybody but Dana.

        Attributes:
            authorized (int | Unset):  Default: 0.
            needs_reauth (int | Unset):  Default: 0.
    """

    authorized: int | Unset = 0
    needs_reauth: int | Unset = 0
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        authorized = self.authorized

        needs_reauth = self.needs_reauth

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if authorized is not UNSET:
            field_dict["authorized"] = authorized
        if needs_reauth is not UNSET:
            field_dict["needs_reauth"] = needs_reauth

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        authorized = d.pop("authorized", UNSET)

        needs_reauth = d.pop("needs_reauth", UNSET)

        member_credential_counts = cls(
            authorized=authorized,
            needs_reauth=needs_reauth,
        )

        member_credential_counts.additional_properties = d
        return member_credential_counts

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
