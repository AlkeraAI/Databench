from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast
from uuid import UUID

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.chat_session_read_machine_refusal_kind_type_0 import (
    ChatSessionReadMachineRefusalKindType0,
)
from ..models.chat_session_read_machine_status import ChatSessionReadMachineStatus
from ..models.chat_session_read_permission_mode import ChatSessionReadPermissionMode
from ..models.chat_session_read_session_state import ChatSessionReadSessionState
from ..models.chat_session_read_workspace_layout_type_0 import ChatSessionReadWorkspaceLayoutType0
from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.chat_model_pin import ChatModelPin
    from ..models.status_fact import StatusFact


T = TypeVar("T", bound="ChatSessionRead")


@_attrs_define
class ChatSessionRead:
    """A conversation as a list or a detail view shows it.

    Attributes:
        id (UUID):
        title (str):
        owner_user_id (UUID):
        machine_id (None | str):
        machine_status (ChatSessionReadMachineStatus):
        created_at (datetime.datetime):
        updated_at (datetime.datetime):
        last_seq (int):
        owner_display_name (str | Unset):  Default: ''.
        org_id (None | Unset | UUID):
        machine_refusal_reason (None | str | Unset):
        machine_refusal_kind (ChatSessionReadMachineRefusalKindType0 | None | Unset):
        machine_refusal_final (bool | Unset):  Default: False.
        wake_requested_at (datetime.datetime | None | Unset):
        end_seq (int | Unset):  Default: 0.
        pending_turn (bool | Unset):  Default: False.
        last_activity_at (datetime.datetime | None | Unset):
        sandbox_vcpu (int | None | Unset):
        sandbox_memory_mb (int | None | Unset):
        model (ChatModelPin | None | Unset):
        permission_mode (ChatSessionReadPermissionMode | Unset):  Default: ChatSessionReadPermissionMode.READ_ONLY.
        approval_refusal (None | str | Unset):
        session_state (ChatSessionReadSessionState | Unset):  Default: ChatSessionReadSessionState.ASLEEP.
        status (None | StatusFact | Unset):
        can_send (bool | Unset):  Default: False.
        can_delete (bool | Unset):  Default: False.
        can_answer_always (bool | Unset):  Default: False.
        unread (bool | Unset):  Default: False.
        needs_you (bool | Unset):  Default: False.
        files_node_id (None | Unset | UUID):
        files_drive_id (None | Unset | UUID):
        attachments (list[str] | Unset):
        attachment_count (int | Unset):  Default: 0.
        source_node_id (None | Unset | UUID):
        source_object_id (None | Unset | UUID):
        files_node_trashed (bool | Unset):  Default: False.
        workspace_id (None | Unset | UUID):
        workspace_layout (ChatSessionReadWorkspaceLayoutType0 | None | Unset):
        workspace_node_id (None | Unset | UUID):
        workspace_files_node_id (None | Unset | UUID):
        working_node_id (None | Unset | UUID):
    """

    id: UUID
    title: str
    owner_user_id: UUID
    machine_id: None | str
    machine_status: ChatSessionReadMachineStatus
    created_at: datetime.datetime
    updated_at: datetime.datetime
    last_seq: int
    owner_display_name: str | Unset = ""
    org_id: None | Unset | UUID = UNSET
    machine_refusal_reason: None | str | Unset = UNSET
    machine_refusal_kind: ChatSessionReadMachineRefusalKindType0 | None | Unset = UNSET
    machine_refusal_final: bool | Unset = False
    wake_requested_at: datetime.datetime | None | Unset = UNSET
    end_seq: int | Unset = 0
    pending_turn: bool | Unset = False
    last_activity_at: datetime.datetime | None | Unset = UNSET
    sandbox_vcpu: int | None | Unset = UNSET
    sandbox_memory_mb: int | None | Unset = UNSET
    model: ChatModelPin | None | Unset = UNSET
    permission_mode: ChatSessionReadPermissionMode | Unset = ChatSessionReadPermissionMode.READ_ONLY
    approval_refusal: None | str | Unset = UNSET
    session_state: ChatSessionReadSessionState | Unset = ChatSessionReadSessionState.ASLEEP
    status: None | StatusFact | Unset = UNSET
    can_send: bool | Unset = False
    can_delete: bool | Unset = False
    can_answer_always: bool | Unset = False
    unread: bool | Unset = False
    needs_you: bool | Unset = False
    files_node_id: None | Unset | UUID = UNSET
    files_drive_id: None | Unset | UUID = UNSET
    attachments: list[str] | Unset = UNSET
    attachment_count: int | Unset = 0
    source_node_id: None | Unset | UUID = UNSET
    source_object_id: None | Unset | UUID = UNSET
    files_node_trashed: bool | Unset = False
    workspace_id: None | Unset | UUID = UNSET
    workspace_layout: ChatSessionReadWorkspaceLayoutType0 | None | Unset = UNSET
    workspace_node_id: None | Unset | UUID = UNSET
    workspace_files_node_id: None | Unset | UUID = UNSET
    working_node_id: None | Unset | UUID = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.chat_model_pin import ChatModelPin
        from ..models.status_fact import StatusFact

        id = str(self.id)

        title = self.title

        owner_user_id = str(self.owner_user_id)

        machine_id: None | str
        machine_id = self.machine_id

        machine_status = self.machine_status.value

        created_at = self.created_at.isoformat()

        updated_at = self.updated_at.isoformat()

        last_seq = self.last_seq

        owner_display_name = self.owner_display_name

        org_id: None | str | Unset
        if isinstance(self.org_id, Unset):
            org_id = UNSET
        elif isinstance(self.org_id, UUID):
            org_id = str(self.org_id)
        else:
            org_id = self.org_id

        machine_refusal_reason: None | str | Unset
        if isinstance(self.machine_refusal_reason, Unset):
            machine_refusal_reason = UNSET
        else:
            machine_refusal_reason = self.machine_refusal_reason

        machine_refusal_kind: None | str | Unset
        if isinstance(self.machine_refusal_kind, Unset):
            machine_refusal_kind = UNSET
        elif isinstance(self.machine_refusal_kind, ChatSessionReadMachineRefusalKindType0):
            machine_refusal_kind = self.machine_refusal_kind.value
        else:
            machine_refusal_kind = self.machine_refusal_kind

        machine_refusal_final = self.machine_refusal_final

        wake_requested_at: None | str | Unset
        if isinstance(self.wake_requested_at, Unset):
            wake_requested_at = UNSET
        elif isinstance(self.wake_requested_at, datetime.datetime):
            wake_requested_at = self.wake_requested_at.isoformat()
        else:
            wake_requested_at = self.wake_requested_at

        end_seq = self.end_seq

        pending_turn = self.pending_turn

        last_activity_at: None | str | Unset
        if isinstance(self.last_activity_at, Unset):
            last_activity_at = UNSET
        elif isinstance(self.last_activity_at, datetime.datetime):
            last_activity_at = self.last_activity_at.isoformat()
        else:
            last_activity_at = self.last_activity_at

        sandbox_vcpu: int | None | Unset
        if isinstance(self.sandbox_vcpu, Unset):
            sandbox_vcpu = UNSET
        else:
            sandbox_vcpu = self.sandbox_vcpu

        sandbox_memory_mb: int | None | Unset
        if isinstance(self.sandbox_memory_mb, Unset):
            sandbox_memory_mb = UNSET
        else:
            sandbox_memory_mb = self.sandbox_memory_mb

        model: dict[str, Any] | None | Unset
        if isinstance(self.model, Unset):
            model = UNSET
        elif isinstance(self.model, ChatModelPin):
            model = self.model.to_dict()
        else:
            model = self.model

        permission_mode: str | Unset = UNSET
        if not isinstance(self.permission_mode, Unset):
            permission_mode = self.permission_mode.value

        approval_refusal: None | str | Unset
        if isinstance(self.approval_refusal, Unset):
            approval_refusal = UNSET
        else:
            approval_refusal = self.approval_refusal

        session_state: str | Unset = UNSET
        if not isinstance(self.session_state, Unset):
            session_state = self.session_state.value

        status: dict[str, Any] | None | Unset
        if isinstance(self.status, Unset):
            status = UNSET
        elif isinstance(self.status, StatusFact):
            status = self.status.to_dict()
        else:
            status = self.status

        can_send = self.can_send

        can_delete = self.can_delete

        can_answer_always = self.can_answer_always

        unread = self.unread

        needs_you = self.needs_you

        files_node_id: None | str | Unset
        if isinstance(self.files_node_id, Unset):
            files_node_id = UNSET
        elif isinstance(self.files_node_id, UUID):
            files_node_id = str(self.files_node_id)
        else:
            files_node_id = self.files_node_id

        files_drive_id: None | str | Unset
        if isinstance(self.files_drive_id, Unset):
            files_drive_id = UNSET
        elif isinstance(self.files_drive_id, UUID):
            files_drive_id = str(self.files_drive_id)
        else:
            files_drive_id = self.files_drive_id

        attachments: list[str] | Unset = UNSET
        if not isinstance(self.attachments, Unset):
            attachments = self.attachments

        attachment_count = self.attachment_count

        source_node_id: None | str | Unset
        if isinstance(self.source_node_id, Unset):
            source_node_id = UNSET
        elif isinstance(self.source_node_id, UUID):
            source_node_id = str(self.source_node_id)
        else:
            source_node_id = self.source_node_id

        source_object_id: None | str | Unset
        if isinstance(self.source_object_id, Unset):
            source_object_id = UNSET
        elif isinstance(self.source_object_id, UUID):
            source_object_id = str(self.source_object_id)
        else:
            source_object_id = self.source_object_id

        files_node_trashed = self.files_node_trashed

        workspace_id: None | str | Unset
        if isinstance(self.workspace_id, Unset):
            workspace_id = UNSET
        elif isinstance(self.workspace_id, UUID):
            workspace_id = str(self.workspace_id)
        else:
            workspace_id = self.workspace_id

        workspace_layout: None | str | Unset
        if isinstance(self.workspace_layout, Unset):
            workspace_layout = UNSET
        elif isinstance(self.workspace_layout, ChatSessionReadWorkspaceLayoutType0):
            workspace_layout = self.workspace_layout.value
        else:
            workspace_layout = self.workspace_layout

        workspace_node_id: None | str | Unset
        if isinstance(self.workspace_node_id, Unset):
            workspace_node_id = UNSET
        elif isinstance(self.workspace_node_id, UUID):
            workspace_node_id = str(self.workspace_node_id)
        else:
            workspace_node_id = self.workspace_node_id

        workspace_files_node_id: None | str | Unset
        if isinstance(self.workspace_files_node_id, Unset):
            workspace_files_node_id = UNSET
        elif isinstance(self.workspace_files_node_id, UUID):
            workspace_files_node_id = str(self.workspace_files_node_id)
        else:
            workspace_files_node_id = self.workspace_files_node_id

        working_node_id: None | str | Unset
        if isinstance(self.working_node_id, Unset):
            working_node_id = UNSET
        elif isinstance(self.working_node_id, UUID):
            working_node_id = str(self.working_node_id)
        else:
            working_node_id = self.working_node_id

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "title": title,
                "owner_user_id": owner_user_id,
                "machine_id": machine_id,
                "machine_status": machine_status,
                "created_at": created_at,
                "updated_at": updated_at,
                "last_seq": last_seq,
            }
        )
        if owner_display_name is not UNSET:
            field_dict["owner_display_name"] = owner_display_name
        if org_id is not UNSET:
            field_dict["org_id"] = org_id
        if machine_refusal_reason is not UNSET:
            field_dict["machine_refusal_reason"] = machine_refusal_reason
        if machine_refusal_kind is not UNSET:
            field_dict["machine_refusal_kind"] = machine_refusal_kind
        if machine_refusal_final is not UNSET:
            field_dict["machine_refusal_final"] = machine_refusal_final
        if wake_requested_at is not UNSET:
            field_dict["wake_requested_at"] = wake_requested_at
        if end_seq is not UNSET:
            field_dict["end_seq"] = end_seq
        if pending_turn is not UNSET:
            field_dict["pending_turn"] = pending_turn
        if last_activity_at is not UNSET:
            field_dict["last_activity_at"] = last_activity_at
        if sandbox_vcpu is not UNSET:
            field_dict["sandbox_vcpu"] = sandbox_vcpu
        if sandbox_memory_mb is not UNSET:
            field_dict["sandbox_memory_mb"] = sandbox_memory_mb
        if model is not UNSET:
            field_dict["model"] = model
        if permission_mode is not UNSET:
            field_dict["permission_mode"] = permission_mode
        if approval_refusal is not UNSET:
            field_dict["approval_refusal"] = approval_refusal
        if session_state is not UNSET:
            field_dict["session_state"] = session_state
        if status is not UNSET:
            field_dict["status"] = status
        if can_send is not UNSET:
            field_dict["can_send"] = can_send
        if can_delete is not UNSET:
            field_dict["can_delete"] = can_delete
        if can_answer_always is not UNSET:
            field_dict["can_answer_always"] = can_answer_always
        if unread is not UNSET:
            field_dict["unread"] = unread
        if needs_you is not UNSET:
            field_dict["needs_you"] = needs_you
        if files_node_id is not UNSET:
            field_dict["files_node_id"] = files_node_id
        if files_drive_id is not UNSET:
            field_dict["files_drive_id"] = files_drive_id
        if attachments is not UNSET:
            field_dict["attachments"] = attachments
        if attachment_count is not UNSET:
            field_dict["attachment_count"] = attachment_count
        if source_node_id is not UNSET:
            field_dict["source_node_id"] = source_node_id
        if source_object_id is not UNSET:
            field_dict["source_object_id"] = source_object_id
        if files_node_trashed is not UNSET:
            field_dict["files_node_trashed"] = files_node_trashed
        if workspace_id is not UNSET:
            field_dict["workspace_id"] = workspace_id
        if workspace_layout is not UNSET:
            field_dict["workspace_layout"] = workspace_layout
        if workspace_node_id is not UNSET:
            field_dict["workspace_node_id"] = workspace_node_id
        if workspace_files_node_id is not UNSET:
            field_dict["workspace_files_node_id"] = workspace_files_node_id
        if working_node_id is not UNSET:
            field_dict["working_node_id"] = working_node_id

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.chat_model_pin import ChatModelPin
        from ..models.status_fact import StatusFact

        d = dict(src_dict)
        id = UUID(d.pop("id"))

        title = d.pop("title")

        owner_user_id = UUID(d.pop("owner_user_id"))

        def _parse_machine_id(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        machine_id = _parse_machine_id(d.pop("machine_id"))

        machine_status = ChatSessionReadMachineStatus(d.pop("machine_status"))

        created_at = datetime.datetime.fromisoformat(d.pop("created_at"))

        updated_at = datetime.datetime.fromisoformat(d.pop("updated_at"))

        last_seq = d.pop("last_seq")

        owner_display_name = d.pop("owner_display_name", UNSET)

        def _parse_org_id(data: object) -> None | Unset | UUID:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                org_id_type_0 = UUID(data)

                return org_id_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | Unset | UUID, data)

        org_id = _parse_org_id(d.pop("org_id", UNSET))

        def _parse_machine_refusal_reason(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        machine_refusal_reason = _parse_machine_refusal_reason(
            d.pop("machine_refusal_reason", UNSET)
        )

        def _parse_machine_refusal_kind(
            data: object,
        ) -> ChatSessionReadMachineRefusalKindType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                machine_refusal_kind_type_0 = ChatSessionReadMachineRefusalKindType0(data)

                return machine_refusal_kind_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(ChatSessionReadMachineRefusalKindType0 | None | Unset, data)

        machine_refusal_kind = _parse_machine_refusal_kind(d.pop("machine_refusal_kind", UNSET))

        machine_refusal_final = d.pop("machine_refusal_final", UNSET)

        def _parse_wake_requested_at(data: object) -> datetime.datetime | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                wake_requested_at_type_0 = datetime.datetime.fromisoformat(data)

                return wake_requested_at_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None | Unset, data)

        wake_requested_at = _parse_wake_requested_at(d.pop("wake_requested_at", UNSET))

        end_seq = d.pop("end_seq", UNSET)

        pending_turn = d.pop("pending_turn", UNSET)

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

        def _parse_sandbox_vcpu(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        sandbox_vcpu = _parse_sandbox_vcpu(d.pop("sandbox_vcpu", UNSET))

        def _parse_sandbox_memory_mb(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        sandbox_memory_mb = _parse_sandbox_memory_mb(d.pop("sandbox_memory_mb", UNSET))

        def _parse_model(data: object) -> ChatModelPin | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                model_type_0 = ChatModelPin.from_dict(data)

                return model_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(ChatModelPin | None | Unset, data)

        model = _parse_model(d.pop("model", UNSET))

        _permission_mode = d.pop("permission_mode", UNSET)
        permission_mode: ChatSessionReadPermissionMode | Unset
        if isinstance(_permission_mode, Unset):
            permission_mode = UNSET
        else:
            permission_mode = ChatSessionReadPermissionMode(_permission_mode)

        def _parse_approval_refusal(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        approval_refusal = _parse_approval_refusal(d.pop("approval_refusal", UNSET))

        _session_state = d.pop("session_state", UNSET)
        session_state: ChatSessionReadSessionState | Unset
        if isinstance(_session_state, Unset):
            session_state = UNSET
        else:
            session_state = ChatSessionReadSessionState(_session_state)

        def _parse_status(data: object) -> None | StatusFact | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                status_type_0 = StatusFact.from_dict(data)

                return status_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | StatusFact | Unset, data)

        status = _parse_status(d.pop("status", UNSET))

        can_send = d.pop("can_send", UNSET)

        can_delete = d.pop("can_delete", UNSET)

        can_answer_always = d.pop("can_answer_always", UNSET)

        unread = d.pop("unread", UNSET)

        needs_you = d.pop("needs_you", UNSET)

        def _parse_files_node_id(data: object) -> None | Unset | UUID:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                files_node_id_type_0 = UUID(data)

                return files_node_id_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | Unset | UUID, data)

        files_node_id = _parse_files_node_id(d.pop("files_node_id", UNSET))

        def _parse_files_drive_id(data: object) -> None | Unset | UUID:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                files_drive_id_type_0 = UUID(data)

                return files_drive_id_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | Unset | UUID, data)

        files_drive_id = _parse_files_drive_id(d.pop("files_drive_id", UNSET))

        attachments = cast(list[str], d.pop("attachments", UNSET))

        attachment_count = d.pop("attachment_count", UNSET)

        def _parse_source_node_id(data: object) -> None | Unset | UUID:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                source_node_id_type_0 = UUID(data)

                return source_node_id_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | Unset | UUID, data)

        source_node_id = _parse_source_node_id(d.pop("source_node_id", UNSET))

        def _parse_source_object_id(data: object) -> None | Unset | UUID:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                source_object_id_type_0 = UUID(data)

                return source_object_id_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | Unset | UUID, data)

        source_object_id = _parse_source_object_id(d.pop("source_object_id", UNSET))

        files_node_trashed = d.pop("files_node_trashed", UNSET)

        def _parse_workspace_id(data: object) -> None | Unset | UUID:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                workspace_id_type_0 = UUID(data)

                return workspace_id_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | Unset | UUID, data)

        workspace_id = _parse_workspace_id(d.pop("workspace_id", UNSET))

        def _parse_workspace_layout(
            data: object,
        ) -> ChatSessionReadWorkspaceLayoutType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                workspace_layout_type_0 = ChatSessionReadWorkspaceLayoutType0(data)

                return workspace_layout_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(ChatSessionReadWorkspaceLayoutType0 | None | Unset, data)

        workspace_layout = _parse_workspace_layout(d.pop("workspace_layout", UNSET))

        def _parse_workspace_node_id(data: object) -> None | Unset | UUID:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                workspace_node_id_type_0 = UUID(data)

                return workspace_node_id_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | Unset | UUID, data)

        workspace_node_id = _parse_workspace_node_id(d.pop("workspace_node_id", UNSET))

        def _parse_workspace_files_node_id(data: object) -> None | Unset | UUID:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                workspace_files_node_id_type_0 = UUID(data)

                return workspace_files_node_id_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | Unset | UUID, data)

        workspace_files_node_id = _parse_workspace_files_node_id(
            d.pop("workspace_files_node_id", UNSET)
        )

        def _parse_working_node_id(data: object) -> None | Unset | UUID:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                working_node_id_type_0 = UUID(data)

                return working_node_id_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | Unset | UUID, data)

        working_node_id = _parse_working_node_id(d.pop("working_node_id", UNSET))

        chat_session_read = cls(
            id=id,
            title=title,
            owner_user_id=owner_user_id,
            machine_id=machine_id,
            machine_status=machine_status,
            created_at=created_at,
            updated_at=updated_at,
            last_seq=last_seq,
            owner_display_name=owner_display_name,
            org_id=org_id,
            machine_refusal_reason=machine_refusal_reason,
            machine_refusal_kind=machine_refusal_kind,
            machine_refusal_final=machine_refusal_final,
            wake_requested_at=wake_requested_at,
            end_seq=end_seq,
            pending_turn=pending_turn,
            last_activity_at=last_activity_at,
            sandbox_vcpu=sandbox_vcpu,
            sandbox_memory_mb=sandbox_memory_mb,
            model=model,
            permission_mode=permission_mode,
            approval_refusal=approval_refusal,
            session_state=session_state,
            status=status,
            can_send=can_send,
            can_delete=can_delete,
            can_answer_always=can_answer_always,
            unread=unread,
            needs_you=needs_you,
            files_node_id=files_node_id,
            files_drive_id=files_drive_id,
            attachments=attachments,
            attachment_count=attachment_count,
            source_node_id=source_node_id,
            source_object_id=source_object_id,
            files_node_trashed=files_node_trashed,
            workspace_id=workspace_id,
            workspace_layout=workspace_layout,
            workspace_node_id=workspace_node_id,
            workspace_files_node_id=workspace_files_node_id,
            working_node_id=working_node_id,
        )

        chat_session_read.additional_properties = d
        return chat_session_read

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
