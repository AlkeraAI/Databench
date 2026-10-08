from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar
from uuid import UUID

from attrs import define as _attrs_define
from attrs import field as _attrs_field

if TYPE_CHECKING:
    from ..models.membership_read import MembershipRead


T = TypeVar("T", bound="MembershipListResponse")


@_attrs_define
class MembershipListResponse:
    """`GET /auth/memberships`: the caller's active memberships, most recently
    used first, then any pending ones, and the org the calling credential is
    in.

        Attributes:
            active_org_team_id (UUID):
            memberships (list[MembershipRead]):
    """

    active_org_team_id: UUID
    memberships: list[MembershipRead]
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        active_org_team_id = str(self.active_org_team_id)

        memberships = []
        for memberships_item_data in self.memberships:
            memberships_item = memberships_item_data.to_dict()
            memberships.append(memberships_item)

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "active_org_team_id": active_org_team_id,
                "memberships": memberships,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.membership_read import MembershipRead

        d = dict(src_dict)
        active_org_team_id = UUID(d.pop("active_org_team_id"))

        memberships = []
        _memberships = d.pop("memberships")
        for memberships_item_data in _memberships:
            memberships_item = MembershipRead.from_dict(memberships_item_data)

            memberships.append(memberships_item)

        membership_list_response = cls(
            active_org_team_id=active_org_team_id,
            memberships=memberships,
        )

        membership_list_response.additional_properties = d
        return membership_list_response

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
