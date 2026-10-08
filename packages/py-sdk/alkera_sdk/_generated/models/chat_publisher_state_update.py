from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.chat_publisher_state_update_ending import ChatPublisherStateUpdateEnding
from ..models.chat_publisher_state_update_refusal_kind_type_0 import (
    ChatPublisherStateUpdateRefusalKindType0,
)
from ..models.chat_publisher_state_update_state import ChatPublisherStateUpdateState
from ..models.chat_publisher_state_update_workspace_sandbox_type_0 import (
    ChatPublisherStateUpdateWorkspaceSandboxType0,
)
from ..types import UNSET, Unset

T = TypeVar("T", bound="ChatPublisherStateUpdate")


@_attrs_define
class ChatPublisherStateUpdate:
    """What the machine bound to a chat reports about it: ``refused`` (with
    the gateway's reason) when it cannot publish, ``publishing`` when it holds
    the chat's session (again), ``asleep`` when it closed that session — the
    folder pushed and released, the chat resumable by any box. Only a machine
    may say it — the route decides who. ``waiting``: it has the chat's
    message but no free slot to open it in yet.

        Attributes:
            state (ChatPublisherStateUpdateState):
            reason (str | Unset):  Default: ''.
            refusal_kind (ChatPublisherStateUpdateRefusalKindType0 | None | Unset):
            ending (ChatPublisherStateUpdateEnding | Unset):  Default: ChatPublisherStateUpdateEnding.IDLE.
            workspace_sandbox (ChatPublisherStateUpdateWorkspaceSandboxType0 | None | Unset):
            workspace_memory_mb (int | None | Unset):
    """

    state: ChatPublisherStateUpdateState
    reason: str | Unset = ""
    refusal_kind: ChatPublisherStateUpdateRefusalKindType0 | None | Unset = UNSET
    ending: ChatPublisherStateUpdateEnding | Unset = ChatPublisherStateUpdateEnding.IDLE
    workspace_sandbox: ChatPublisherStateUpdateWorkspaceSandboxType0 | None | Unset = UNSET
    workspace_memory_mb: int | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        state = self.state.value

        reason = self.reason

        refusal_kind: None | str | Unset
        if isinstance(self.refusal_kind, Unset):
            refusal_kind = UNSET
        elif isinstance(self.refusal_kind, ChatPublisherStateUpdateRefusalKindType0):
            refusal_kind = self.refusal_kind.value
        else:
            refusal_kind = self.refusal_kind

        ending: str | Unset = UNSET
        if not isinstance(self.ending, Unset):
            ending = self.ending.value

        workspace_sandbox: None | str | Unset
        if isinstance(self.workspace_sandbox, Unset):
            workspace_sandbox = UNSET
        elif isinstance(self.workspace_sandbox, ChatPublisherStateUpdateWorkspaceSandboxType0):
            workspace_sandbox = self.workspace_sandbox.value
        else:
            workspace_sandbox = self.workspace_sandbox

        workspace_memory_mb: int | None | Unset
        if isinstance(self.workspace_memory_mb, Unset):
            workspace_memory_mb = UNSET
        else:
            workspace_memory_mb = self.workspace_memory_mb

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "state": state,
            }
        )
        if reason is not UNSET:
            field_dict["reason"] = reason
        if refusal_kind is not UNSET:
            field_dict["refusal_kind"] = refusal_kind
        if ending is not UNSET:
            field_dict["ending"] = ending
        if workspace_sandbox is not UNSET:
            field_dict["workspace_sandbox"] = workspace_sandbox
        if workspace_memory_mb is not UNSET:
            field_dict["workspace_memory_mb"] = workspace_memory_mb

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        state = ChatPublisherStateUpdateState(d.pop("state"))

        reason = d.pop("reason", UNSET)

        def _parse_refusal_kind(
            data: object,
        ) -> ChatPublisherStateUpdateRefusalKindType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                refusal_kind_type_0 = ChatPublisherStateUpdateRefusalKindType0(data)

                return refusal_kind_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(ChatPublisherStateUpdateRefusalKindType0 | None | Unset, data)

        refusal_kind = _parse_refusal_kind(d.pop("refusal_kind", UNSET))

        _ending = d.pop("ending", UNSET)
        ending: ChatPublisherStateUpdateEnding | Unset
        if isinstance(_ending, Unset):
            ending = UNSET
        else:
            ending = ChatPublisherStateUpdateEnding(_ending)

        def _parse_workspace_sandbox(
            data: object,
        ) -> ChatPublisherStateUpdateWorkspaceSandboxType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                workspace_sandbox_type_0 = ChatPublisherStateUpdateWorkspaceSandboxType0(data)

                return workspace_sandbox_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(ChatPublisherStateUpdateWorkspaceSandboxType0 | None | Unset, data)

        workspace_sandbox = _parse_workspace_sandbox(d.pop("workspace_sandbox", UNSET))

        def _parse_workspace_memory_mb(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        workspace_memory_mb = _parse_workspace_memory_mb(d.pop("workspace_memory_mb", UNSET))

        chat_publisher_state_update = cls(
            state=state,
            reason=reason,
            refusal_kind=refusal_kind,
            ending=ending,
            workspace_sandbox=workspace_sandbox,
            workspace_memory_mb=workspace_memory_mb,
        )

        chat_publisher_state_update.additional_properties = d
        return chat_publisher_state_update

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
