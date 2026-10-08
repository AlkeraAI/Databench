from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.invitation_refusal_code import InvitationRefusalCode

T = TypeVar("T", bound="InvitationRefusal")


@_attrs_define
class InvitationRefusal:
    """The reason an invitation cannot be accepted, and what would have to
    change for it to be — the same sentence the accept route answers with.

        Attributes:
            code (InvitationRefusalCode): Why a pending invitation cannot be accepted by the account reading it.

                A standing fact about the recipient, not a transient failure: the same
                answer comes back on every accept until something outside the invitation
                changes. ``other_org`` is the single-org rule — the account already belongs
                to a different organization than the one the invitation is into.
                ``email_verification_required`` asks the account to prove its address
                before it joins another organization, and ``membership_deactivated`` says
                the organization offboarded this account, which an invitation does not
                undo.
            message (str):
    """

    code: InvitationRefusalCode
    message: str
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        code = self.code.value

        message = self.message

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "code": code,
                "message": message,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        code = InvitationRefusalCode(d.pop("code"))

        message = d.pop("message")

        invitation_refusal = cls(
            code=code,
            message=message,
        )

        invitation_refusal.additional_properties = d
        return invitation_refusal

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
