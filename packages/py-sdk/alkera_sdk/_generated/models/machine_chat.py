from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.org_ref import OrgRef
    from ..models.user import User


T = TypeVar("T", bound="MachineChat")


@_attrs_define
class MachineChat:
    """
    Attributes:
        id (str):
        org (OrgRef):
        user (User):
        title (str | Unset):  Default: ''.
        machine_status (str | Unset):  Default: ''.
        last_activity_at (datetime.datetime | None | Unset):
    """

    id: str
    org: OrgRef
    user: User
    title: str | Unset = ""
    machine_status: str | Unset = ""
    last_activity_at: datetime.datetime | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        id = self.id

        org = self.org.to_dict()

        user = self.user.to_dict()

        title = self.title

        machine_status = self.machine_status

        last_activity_at: None | str | Unset
        if isinstance(self.last_activity_at, Unset):
            last_activity_at = UNSET
        elif isinstance(self.last_activity_at, datetime.datetime):
            last_activity_at = self.last_activity_at.isoformat()
        else:
            last_activity_at = self.last_activity_at

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "org": org,
                "user": user,
            }
        )
        if title is not UNSET:
            field_dict["title"] = title
        if machine_status is not UNSET:
            field_dict["machine_status"] = machine_status
        if last_activity_at is not UNSET:
            field_dict["last_activity_at"] = last_activity_at

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.org_ref import OrgRef
        from ..models.user import User

        d = dict(src_dict)
        id = d.pop("id")

        org = OrgRef.from_dict(d.pop("org"))

        user = User.from_dict(d.pop("user"))

        title = d.pop("title", UNSET)

        machine_status = d.pop("machine_status", UNSET)

        def _parse_last_activity_at(data: object) -> datetime.datetime | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                last_activity_at_type_0 = datetime.datetime.fromisoformat(data)

                return last_activity_at_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None | Unset, data)

        last_activity_at = _parse_last_activity_at(d.pop("last_activity_at", UNSET))

        machine_chat = cls(
            id=id,
            org=org,
            user=user,
            title=title,
            machine_status=machine_status,
            last_activity_at=last_activity_at,
        )

        machine_chat.additional_properties = d
        return machine_chat

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
