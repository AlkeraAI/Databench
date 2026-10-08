from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import Any, TypeVar, cast
from uuid import UUID

from attrs import define as _attrs_define
from attrs import field as _attrs_field

T = TypeVar("T", bound="OrgChatInsight")


@_attrs_define
class OrgChatInsight:
    """One of the org's chats as the admin activity list shows it.

    Attributes:
        id (UUID):
        title (str):
        owner_email (str):
        machine_id (None | str):
        machine_name (None | str):
        machine_status (str):
        mirror_state (None | str):
        machine_refusal (None | str):
        last_activity_at (datetime.datetime | None):
        created_at (datetime.datetime):
    """

    id: UUID
    title: str
    owner_email: str
    machine_id: None | str
    machine_name: None | str
    machine_status: str
    mirror_state: None | str
    machine_refusal: None | str
    last_activity_at: datetime.datetime | None
    created_at: datetime.datetime
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        id = str(self.id)

        title = self.title

        owner_email = self.owner_email

        machine_id: None | str
        machine_id = self.machine_id

        machine_name: None | str
        machine_name = self.machine_name

        machine_status = self.machine_status

        mirror_state: None | str
        mirror_state = self.mirror_state

        machine_refusal: None | str
        machine_refusal = self.machine_refusal

        last_activity_at: None | str
        if isinstance(self.last_activity_at, datetime.datetime):
            last_activity_at = self.last_activity_at.isoformat()
        else:
            last_activity_at = self.last_activity_at

        created_at = self.created_at.isoformat()

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "title": title,
                "owner_email": owner_email,
                "machine_id": machine_id,
                "machine_name": machine_name,
                "machine_status": machine_status,
                "mirror_state": mirror_state,
                "machine_refusal": machine_refusal,
                "last_activity_at": last_activity_at,
                "created_at": created_at,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        id = UUID(d.pop("id"))

        title = d.pop("title")

        owner_email = d.pop("owner_email")

        def _parse_machine_id(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        machine_id = _parse_machine_id(d.pop("machine_id"))

        def _parse_machine_name(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        machine_name = _parse_machine_name(d.pop("machine_name"))

        machine_status = d.pop("machine_status")

        def _parse_mirror_state(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        mirror_state = _parse_mirror_state(d.pop("mirror_state"))

        def _parse_machine_refusal(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        machine_refusal = _parse_machine_refusal(d.pop("machine_refusal"))

        def _parse_last_activity_at(data: object) -> datetime.datetime | None:
            if data is None:
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                last_activity_at_type_0 = datetime.datetime.fromisoformat(data)

                return last_activity_at_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None, data)

        last_activity_at = _parse_last_activity_at(d.pop("last_activity_at"))

        created_at = datetime.datetime.fromisoformat(d.pop("created_at"))

        org_chat_insight = cls(
            id=id,
            title=title,
            owner_email=owner_email,
            machine_id=machine_id,
            machine_name=machine_name,
            machine_status=machine_status,
            mirror_state=mirror_state,
            machine_refusal=machine_refusal,
            last_activity_at=last_activity_at,
            created_at=created_at,
        )

        org_chat_insight.additional_properties = d
        return org_chat_insight

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
