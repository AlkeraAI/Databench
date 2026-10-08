from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast
from uuid import UUID

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.chat_template_read_permission_mode import ChatTemplateReadPermissionMode
from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.chat_model_pin import ChatModelPin


T = TypeVar("T", bound="ChatTemplateRead")


@_attrs_define
class ChatTemplateRead:
    """A chat template as a list row or a detail view shows it.

    Attributes:
        id (UUID):
        title (str):
        version (int):
        owner_user_id (UUID):
        created_at (datetime.datetime):
        updated_at (datetime.datetime):
        files_node_id (None | Unset | UUID):
        brief (str | Unset):  Default: ''.
        model (ChatModelPin | None | Unset):
        permission_mode (ChatTemplateReadPermissionMode | Unset):  Default: ChatTemplateReadPermissionMode.READ_ONLY.
        source_chat_id (None | Unset | UUID):
        saved_from_seq (int | Unset):  Default: 0.
    """

    id: UUID
    title: str
    version: int
    owner_user_id: UUID
    created_at: datetime.datetime
    updated_at: datetime.datetime
    files_node_id: None | Unset | UUID = UNSET
    brief: str | Unset = ""
    model: ChatModelPin | None | Unset = UNSET
    permission_mode: ChatTemplateReadPermissionMode | Unset = (
        ChatTemplateReadPermissionMode.READ_ONLY
    )
    source_chat_id: None | Unset | UUID = UNSET
    saved_from_seq: int | Unset = 0
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.chat_model_pin import ChatModelPin

        id = str(self.id)

        title = self.title

        version = self.version

        owner_user_id = str(self.owner_user_id)

        created_at = self.created_at.isoformat()

        updated_at = self.updated_at.isoformat()

        files_node_id: None | str | Unset
        if isinstance(self.files_node_id, Unset):
            files_node_id = UNSET
        elif isinstance(self.files_node_id, UUID):
            files_node_id = str(self.files_node_id)
        else:
            files_node_id = self.files_node_id

        brief = self.brief

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

        source_chat_id: None | str | Unset
        if isinstance(self.source_chat_id, Unset):
            source_chat_id = UNSET
        elif isinstance(self.source_chat_id, UUID):
            source_chat_id = str(self.source_chat_id)
        else:
            source_chat_id = self.source_chat_id

        saved_from_seq = self.saved_from_seq

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "title": title,
                "version": version,
                "owner_user_id": owner_user_id,
                "created_at": created_at,
                "updated_at": updated_at,
            }
        )
        if files_node_id is not UNSET:
            field_dict["files_node_id"] = files_node_id
        if brief is not UNSET:
            field_dict["brief"] = brief
        if model is not UNSET:
            field_dict["model"] = model
        if permission_mode is not UNSET:
            field_dict["permission_mode"] = permission_mode
        if source_chat_id is not UNSET:
            field_dict["source_chat_id"] = source_chat_id
        if saved_from_seq is not UNSET:
            field_dict["saved_from_seq"] = saved_from_seq

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.chat_model_pin import ChatModelPin

        d = dict(src_dict)
        id = UUID(d.pop("id"))

        title = d.pop("title")

        version = d.pop("version")

        owner_user_id = UUID(d.pop("owner_user_id"))

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

        brief = d.pop("brief", UNSET)

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
        permission_mode: ChatTemplateReadPermissionMode | Unset
        if isinstance(_permission_mode, Unset):
            permission_mode = UNSET
        else:
            permission_mode = ChatTemplateReadPermissionMode(_permission_mode)

        def _parse_source_chat_id(data: object) -> None | Unset | UUID:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                source_chat_id_type_0 = UUID(data)

                return source_chat_id_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | Unset | UUID, data)

        source_chat_id = _parse_source_chat_id(d.pop("source_chat_id", UNSET))

        saved_from_seq = d.pop("saved_from_seq", UNSET)

        chat_template_read = cls(
            id=id,
            title=title,
            version=version,
            owner_user_id=owner_user_id,
            created_at=created_at,
            updated_at=updated_at,
            files_node_id=files_node_id,
            brief=brief,
            model=model,
            permission_mode=permission_mode,
            source_chat_id=source_chat_id,
            saved_from_seq=saved_from_seq,
        )

        chat_template_read.additional_properties = d
        return chat_template_read

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
