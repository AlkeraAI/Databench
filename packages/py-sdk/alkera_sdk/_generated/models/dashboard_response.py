from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.invitation_read import InvitationRead
    from ..models.team_read import TeamRead
    from ..models.user_read import UserRead


T = TypeVar("T", bound="DashboardResponse")


@_attrs_define
class DashboardResponse:
    """
    Attributes:
        user (UserRead):
        org (TeamRead):
        teams (list[TeamRead]):
        pending_invitations (list[InvitationRead]):
        is_org_admin (bool | Unset):  Default: False.
        entitled_features (list[str] | Unset):
        enterprise_features_enabled (bool | Unset):  Default: False.
    """

    user: UserRead
    org: TeamRead
    teams: list[TeamRead]
    pending_invitations: list[InvitationRead]
    is_org_admin: bool | Unset = False
    entitled_features: list[str] | Unset = UNSET
    enterprise_features_enabled: bool | Unset = False
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        user = self.user.to_dict()

        org = self.org.to_dict()

        teams = []
        for teams_item_data in self.teams:
            teams_item = teams_item_data.to_dict()
            teams.append(teams_item)

        pending_invitations = []
        for pending_invitations_item_data in self.pending_invitations:
            pending_invitations_item = pending_invitations_item_data.to_dict()
            pending_invitations.append(pending_invitations_item)

        is_org_admin = self.is_org_admin

        entitled_features: list[str] | Unset = UNSET
        if not isinstance(self.entitled_features, Unset):
            entitled_features = self.entitled_features

        enterprise_features_enabled = self.enterprise_features_enabled

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "user": user,
                "org": org,
                "teams": teams,
                "pending_invitations": pending_invitations,
            }
        )
        if is_org_admin is not UNSET:
            field_dict["is_org_admin"] = is_org_admin
        if entitled_features is not UNSET:
            field_dict["entitled_features"] = entitled_features
        if enterprise_features_enabled is not UNSET:
            field_dict["enterprise_features_enabled"] = enterprise_features_enabled

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.invitation_read import InvitationRead
        from ..models.team_read import TeamRead
        from ..models.user_read import UserRead

        d = dict(src_dict)
        user = UserRead.from_dict(d.pop("user"))

        org = TeamRead.from_dict(d.pop("org"))

        teams = []
        _teams = d.pop("teams")
        for teams_item_data in _teams:
            teams_item = TeamRead.from_dict(teams_item_data)

            teams.append(teams_item)

        pending_invitations = []
        _pending_invitations = d.pop("pending_invitations")
        for pending_invitations_item_data in _pending_invitations:
            pending_invitations_item = InvitationRead.from_dict(pending_invitations_item_data)

            pending_invitations.append(pending_invitations_item)

        is_org_admin = d.pop("is_org_admin", UNSET)

        entitled_features = cast(list[str], d.pop("entitled_features", UNSET))

        enterprise_features_enabled = d.pop("enterprise_features_enabled", UNSET)

        dashboard_response = cls(
            user=user,
            org=org,
            teams=teams,
            pending_invitations=pending_invitations,
            is_org_admin=is_org_admin,
            entitled_features=entitled_features,
            enterprise_features_enabled=enterprise_features_enabled,
        )

        dashboard_response.additional_properties = d
        return dashboard_response

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
