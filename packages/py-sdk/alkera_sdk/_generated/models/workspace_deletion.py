from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar
from uuid import UUID

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.workspace_deletion_state import WorkspaceDeletionState

T = TypeVar("T", bound="WorkspaceDeletion")


@_attrs_define
class WorkspaceDeletion:
    """Where a deleted workspace's deletion stands. The workspace and its chats
    are gone from every read the moment it is deleted; ``deleting`` means the
    background pass is still ending its chats and trashing their folders.

        Attributes:
            id (UUID):
            state (WorkspaceDeletionState):
            chats_remaining (int):
    """

    id: UUID
    state: WorkspaceDeletionState
    chats_remaining: int
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        id = str(self.id)

        state = self.state.value

        chats_remaining = self.chats_remaining

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "state": state,
                "chats_remaining": chats_remaining,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        id = UUID(d.pop("id"))

        state = WorkspaceDeletionState(d.pop("state"))

        chats_remaining = d.pop("chats_remaining")

        workspace_deletion = cls(
            id=id,
            state=state,
            chats_remaining=chats_remaining,
        )

        workspace_deletion.additional_properties = d
        return workspace_deletion

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
