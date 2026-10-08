from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast
from uuid import UUID

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.invitation_read import InvitationRead


T = TypeVar("T", bound="InvitationAcceptResponse")


@_attrs_define
class InvitationAcceptResponse:
    """An accepted invitation and the teams it seated the account in.

    ``org_team_id`` / ``org_name`` name the organization joined, so a client
    whose session is in another organization can offer to switch into it.

        Attributes:
            invitation (InvitationRead): Authenticated read shape.

                The raw `token` is intentionally NOT exposed — only its keyed hash is stored,
                so a DB read can't yield a usable invite link. The recipient's
                `/dashboard/invites` page accepts/rejects by `id` (`/{invitation_id}/...`);
                the raw token only ever travels in the emailed `/signup?invite=<token>` link.
            joined_team_ids (list[UUID]):
            org_team_id (None | Unset | UUID):
            org_name (None | str | Unset):
    """

    invitation: InvitationRead
    joined_team_ids: list[UUID]
    org_team_id: None | Unset | UUID = UNSET
    org_name: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        invitation = self.invitation.to_dict()

        joined_team_ids = []
        for joined_team_ids_item_data in self.joined_team_ids:
            joined_team_ids_item = str(joined_team_ids_item_data)
            joined_team_ids.append(joined_team_ids_item)

        org_team_id: None | str | Unset
        if isinstance(self.org_team_id, Unset):
            org_team_id = UNSET
        elif isinstance(self.org_team_id, UUID):
            org_team_id = str(self.org_team_id)
        else:
            org_team_id = self.org_team_id

        org_name: None | str | Unset
        if isinstance(self.org_name, Unset):
            org_name = UNSET
        else:
            org_name = self.org_name

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "invitation": invitation,
                "joined_team_ids": joined_team_ids,
            }
        )
        if org_team_id is not UNSET:
            field_dict["org_team_id"] = org_team_id
        if org_name is not UNSET:
            field_dict["org_name"] = org_name

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.invitation_read import InvitationRead

        d = dict(src_dict)
        invitation = InvitationRead.from_dict(d.pop("invitation"))

        joined_team_ids = []
        _joined_team_ids = d.pop("joined_team_ids")
        for joined_team_ids_item_data in _joined_team_ids:
            joined_team_ids_item = UUID(joined_team_ids_item_data)

            joined_team_ids.append(joined_team_ids_item)

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

        def _parse_org_name(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        org_name = _parse_org_name(d.pop("org_name", UNSET))

        invitation_accept_response = cls(
            invitation=invitation,
            joined_team_ids=joined_team_ids,
            org_team_id=org_team_id,
            org_name=org_name,
        )

        invitation_accept_response.additional_properties = d
        return invitation_accept_response

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
