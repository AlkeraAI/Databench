from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import Any, TypeVar
from uuid import UUID

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.org_issue_kind import OrgIssueKind

T = TypeVar("T", bound="OrgIssue")


@_attrs_define
class OrgIssue:
    """A thing that went wrong in one of the org's chats.

    ``error``: a turn that ended in an error, or a session that reported one.
    ``refusal``: the model refused. ``denied``: a person or policy rejected a
    permission ask. ``machine_refused``: the bound machine said it cannot
    publish the chat.

        Attributes:
            kind (OrgIssueKind):
            chat_id (UUID):
            chat_title (str):
            detail (str):
            at (datetime.datetime):
    """

    kind: OrgIssueKind
    chat_id: UUID
    chat_title: str
    detail: str
    at: datetime.datetime
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        kind = self.kind.value

        chat_id = str(self.chat_id)

        chat_title = self.chat_title

        detail = self.detail

        at = self.at.isoformat()

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "kind": kind,
                "chat_id": chat_id,
                "chat_title": chat_title,
                "detail": detail,
                "at": at,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        kind = OrgIssueKind(d.pop("kind"))

        chat_id = UUID(d.pop("chat_id"))

        chat_title = d.pop("chat_title")

        detail = d.pop("detail")

        at = datetime.datetime.fromisoformat(d.pop("at"))

        org_issue = cls(
            kind=kind,
            chat_id=chat_id,
            chat_title=chat_title,
            detail=detail,
            at=at,
        )

        org_issue.additional_properties = d
        return org_issue

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
