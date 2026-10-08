from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast
from uuid import UUID

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.workspace_read_kind import WorkspaceReadKind
from ..models.workspace_read_layout import WorkspaceReadLayout
from ..models.workspace_read_machine_status import WorkspaceReadMachineStatus
from ..models.workspace_read_mirror_state_type_0 import WorkspaceReadMirrorStateType0
from ..models.workspace_read_sandbox_state_type_0 import WorkspaceReadSandboxStateType0
from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.status_fact import StatusFact


T = TypeVar("T", bound="WorkspaceRead")


@_attrs_define
class WorkspaceRead:
    """One workspace as a list or a detail view shows it.

    Attributes:
        id (UUID):
        title (str):
        kind (WorkspaceReadKind):
        layout (WorkspaceReadLayout):
        owner_user_id (UUID):
        version (int):
        created_at (datetime.datetime):
        updated_at (datetime.datetime):
        files_node_id (None | Unset | UUID):
        files_drive_id (None | Unset | UUID):
        working_node_id (None | Unset | UUID):
        adopted_chat_id (None | Unset | UUID):
        chat_count (int | Unset):  Default: 0.
        unread_count (int | Unset):  Default: 0.
        machine_id (None | str | Unset):
        machine_status (WorkspaceReadMachineStatus | Unset):  Default: WorkspaceReadMachineStatus.NONE.
        mirror_state (None | Unset | WorkspaceReadMirrorStateType0):
        wake_requested_at (datetime.datetime | None | Unset):
        writable (bool | Unset):  Default: False.
        sandbox_state (None | Unset | WorkspaceReadSandboxStateType0):
        sandbox_memory_used_mb (int | None | Unset):
        status (None | StatusFact | Unset):
        can_rename (bool | Unset):  Default: False.
        can_delete (bool | Unset):  Default: False.
        can_add_chat (bool | Unset):  Default: False.
    """

    id: UUID
    title: str
    kind: WorkspaceReadKind
    layout: WorkspaceReadLayout
    owner_user_id: UUID
    version: int
    created_at: datetime.datetime
    updated_at: datetime.datetime
    files_node_id: None | Unset | UUID = UNSET
    files_drive_id: None | Unset | UUID = UNSET
    working_node_id: None | Unset | UUID = UNSET
    adopted_chat_id: None | Unset | UUID = UNSET
    chat_count: int | Unset = 0
    unread_count: int | Unset = 0
    machine_id: None | str | Unset = UNSET
    machine_status: WorkspaceReadMachineStatus | Unset = WorkspaceReadMachineStatus.NONE
    mirror_state: None | Unset | WorkspaceReadMirrorStateType0 = UNSET
    wake_requested_at: datetime.datetime | None | Unset = UNSET
    writable: bool | Unset = False
    sandbox_state: None | Unset | WorkspaceReadSandboxStateType0 = UNSET
    sandbox_memory_used_mb: int | None | Unset = UNSET
    status: None | StatusFact | Unset = UNSET
    can_rename: bool | Unset = False
    can_delete: bool | Unset = False
    can_add_chat: bool | Unset = False
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.status_fact import StatusFact

        id = str(self.id)

        title = self.title

        kind = self.kind.value

        layout = self.layout.value

        owner_user_id = str(self.owner_user_id)

        version = self.version

        created_at = self.created_at.isoformat()

        updated_at = self.updated_at.isoformat()

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

        working_node_id: None | str | Unset
        if isinstance(self.working_node_id, Unset):
            working_node_id = UNSET
        elif isinstance(self.working_node_id, UUID):
            working_node_id = str(self.working_node_id)
        else:
            working_node_id = self.working_node_id

        adopted_chat_id: None | str | Unset
        if isinstance(self.adopted_chat_id, Unset):
            adopted_chat_id = UNSET
        elif isinstance(self.adopted_chat_id, UUID):
            adopted_chat_id = str(self.adopted_chat_id)
        else:
            adopted_chat_id = self.adopted_chat_id

        chat_count = self.chat_count

        unread_count = self.unread_count

        machine_id: None | str | Unset
        if isinstance(self.machine_id, Unset):
            machine_id = UNSET
        else:
            machine_id = self.machine_id

        machine_status: str | Unset = UNSET
        if not isinstance(self.machine_status, Unset):
            machine_status = self.machine_status.value

        mirror_state: None | str | Unset
        if isinstance(self.mirror_state, Unset):
            mirror_state = UNSET
        elif isinstance(self.mirror_state, WorkspaceReadMirrorStateType0):
            mirror_state = self.mirror_state.value
        else:
            mirror_state = self.mirror_state

        wake_requested_at: None | str | Unset
        if isinstance(self.wake_requested_at, Unset):
            wake_requested_at = UNSET
        elif isinstance(self.wake_requested_at, datetime.datetime):
            wake_requested_at = self.wake_requested_at.isoformat()
        else:
            wake_requested_at = self.wake_requested_at

        writable = self.writable

        sandbox_state: None | str | Unset
        if isinstance(self.sandbox_state, Unset):
            sandbox_state = UNSET
        elif isinstance(self.sandbox_state, WorkspaceReadSandboxStateType0):
            sandbox_state = self.sandbox_state.value
        else:
            sandbox_state = self.sandbox_state

        sandbox_memory_used_mb: int | None | Unset
        if isinstance(self.sandbox_memory_used_mb, Unset):
            sandbox_memory_used_mb = UNSET
        else:
            sandbox_memory_used_mb = self.sandbox_memory_used_mb

        status: dict[str, Any] | None | Unset
        if isinstance(self.status, Unset):
            status = UNSET
        elif isinstance(self.status, StatusFact):
            status = self.status.to_dict()
        else:
            status = self.status

        can_rename = self.can_rename

        can_delete = self.can_delete

        can_add_chat = self.can_add_chat

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "title": title,
                "kind": kind,
                "layout": layout,
                "owner_user_id": owner_user_id,
                "version": version,
                "created_at": created_at,
                "updated_at": updated_at,
            }
        )
        if files_node_id is not UNSET:
            field_dict["files_node_id"] = files_node_id
        if files_drive_id is not UNSET:
            field_dict["files_drive_id"] = files_drive_id
        if working_node_id is not UNSET:
            field_dict["working_node_id"] = working_node_id
        if adopted_chat_id is not UNSET:
            field_dict["adopted_chat_id"] = adopted_chat_id
        if chat_count is not UNSET:
            field_dict["chat_count"] = chat_count
        if unread_count is not UNSET:
            field_dict["unread_count"] = unread_count
        if machine_id is not UNSET:
            field_dict["machine_id"] = machine_id
        if machine_status is not UNSET:
            field_dict["machine_status"] = machine_status
        if mirror_state is not UNSET:
            field_dict["mirror_state"] = mirror_state
        if wake_requested_at is not UNSET:
            field_dict["wake_requested_at"] = wake_requested_at
        if writable is not UNSET:
            field_dict["writable"] = writable
        if sandbox_state is not UNSET:
            field_dict["sandbox_state"] = sandbox_state
        if sandbox_memory_used_mb is not UNSET:
            field_dict["sandbox_memory_used_mb"] = sandbox_memory_used_mb
        if status is not UNSET:
            field_dict["status"] = status
        if can_rename is not UNSET:
            field_dict["can_rename"] = can_rename
        if can_delete is not UNSET:
            field_dict["can_delete"] = can_delete
        if can_add_chat is not UNSET:
            field_dict["can_add_chat"] = can_add_chat

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.status_fact import StatusFact

        d = dict(src_dict)
        id = UUID(d.pop("id"))

        title = d.pop("title")

        kind = WorkspaceReadKind(d.pop("kind"))

        layout = WorkspaceReadLayout(d.pop("layout"))

        owner_user_id = UUID(d.pop("owner_user_id"))

        version = d.pop("version")

        created_at = datetime.datetime.fromisoformat(d.pop("created_at"))

        updated_at = datetime.datetime.fromisoformat(d.pop("updated_at"))

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

        def _parse_adopted_chat_id(data: object) -> None | Unset | UUID:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                adopted_chat_id_type_0 = UUID(data)

                return adopted_chat_id_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | Unset | UUID, data)

        adopted_chat_id = _parse_adopted_chat_id(d.pop("adopted_chat_id", UNSET))

        chat_count = d.pop("chat_count", UNSET)

        unread_count = d.pop("unread_count", UNSET)

        def _parse_machine_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        machine_id = _parse_machine_id(d.pop("machine_id", UNSET))

        _machine_status = d.pop("machine_status", UNSET)
        machine_status: WorkspaceReadMachineStatus | Unset
        if isinstance(_machine_status, Unset):
            machine_status = UNSET
        else:
            machine_status = WorkspaceReadMachineStatus(_machine_status)

        def _parse_mirror_state(data: object) -> None | Unset | WorkspaceReadMirrorStateType0:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                mirror_state_type_0 = WorkspaceReadMirrorStateType0(data)

                return mirror_state_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | Unset | WorkspaceReadMirrorStateType0, data)

        mirror_state = _parse_mirror_state(d.pop("mirror_state", UNSET))

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

        writable = d.pop("writable", UNSET)

        def _parse_sandbox_state(data: object) -> None | Unset | WorkspaceReadSandboxStateType0:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                sandbox_state_type_0 = WorkspaceReadSandboxStateType0(data)

                return sandbox_state_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | Unset | WorkspaceReadSandboxStateType0, data)

        sandbox_state = _parse_sandbox_state(d.pop("sandbox_state", UNSET))

        def _parse_sandbox_memory_used_mb(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        sandbox_memory_used_mb = _parse_sandbox_memory_used_mb(
            d.pop("sandbox_memory_used_mb", UNSET)
        )

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

        can_rename = d.pop("can_rename", UNSET)

        can_delete = d.pop("can_delete", UNSET)

        can_add_chat = d.pop("can_add_chat", UNSET)

        workspace_read = cls(
            id=id,
            title=title,
            kind=kind,
            layout=layout,
            owner_user_id=owner_user_id,
            version=version,
            created_at=created_at,
            updated_at=updated_at,
            files_node_id=files_node_id,
            files_drive_id=files_drive_id,
            working_node_id=working_node_id,
            adopted_chat_id=adopted_chat_id,
            chat_count=chat_count,
            unread_count=unread_count,
            machine_id=machine_id,
            machine_status=machine_status,
            mirror_state=mirror_state,
            wake_requested_at=wake_requested_at,
            writable=writable,
            sandbox_state=sandbox_state,
            sandbox_memory_used_mb=sandbox_memory_used_mb,
            status=status,
            can_rename=can_rename,
            can_delete=can_delete,
            can_add_chat=can_add_chat,
        )

        workspace_read.additional_properties = d
        return workspace_read

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
