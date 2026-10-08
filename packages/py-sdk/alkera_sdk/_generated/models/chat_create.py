from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast
from uuid import UUID

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.chat_create_permission_mode_type_0 import ChatCreatePermissionModeType0
from ..types import UNSET, Unset

T = TypeVar("T", bound="ChatCreate")


@_attrs_define
class ChatCreate:
    """
    Attributes:
        title (None | str | Unset):
        client_id (None | str | Unset):
        model (None | str | Unset):
        effort (None | str | Unset):
        permission_mode (ChatCreatePermissionModeType0 | None | Unset):
        source_node_id (None | Unset | UUID):
        claim_spare (bool | Unset):  Default: False.
        workspace_id (None | Unset | UUID):
    """

    title: None | str | Unset = UNSET
    client_id: None | str | Unset = UNSET
    model: None | str | Unset = UNSET
    effort: None | str | Unset = UNSET
    permission_mode: ChatCreatePermissionModeType0 | None | Unset = UNSET
    source_node_id: None | Unset | UUID = UNSET
    claim_spare: bool | Unset = False
    workspace_id: None | Unset | UUID = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        title: None | str | Unset
        if isinstance(self.title, Unset):
            title = UNSET
        else:
            title = self.title

        client_id: None | str | Unset
        if isinstance(self.client_id, Unset):
            client_id = UNSET
        else:
            client_id = self.client_id

        model: None | str | Unset
        if isinstance(self.model, Unset):
            model = UNSET
        else:
            model = self.model

        effort: None | str | Unset
        if isinstance(self.effort, Unset):
            effort = UNSET
        else:
            effort = self.effort

        permission_mode: None | str | Unset
        if isinstance(self.permission_mode, Unset):
            permission_mode = UNSET
        elif isinstance(self.permission_mode, ChatCreatePermissionModeType0):
            permission_mode = self.permission_mode.value
        else:
            permission_mode = self.permission_mode

        source_node_id: None | str | Unset
        if isinstance(self.source_node_id, Unset):
            source_node_id = UNSET
        elif isinstance(self.source_node_id, UUID):
            source_node_id = str(self.source_node_id)
        else:
            source_node_id = self.source_node_id

        claim_spare = self.claim_spare

        workspace_id: None | str | Unset
        if isinstance(self.workspace_id, Unset):
            workspace_id = UNSET
        elif isinstance(self.workspace_id, UUID):
            workspace_id = str(self.workspace_id)
        else:
            workspace_id = self.workspace_id

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if title is not UNSET:
            field_dict["title"] = title
        if client_id is not UNSET:
            field_dict["client_id"] = client_id
        if model is not UNSET:
            field_dict["model"] = model
        if effort is not UNSET:
            field_dict["effort"] = effort
        if permission_mode is not UNSET:
            field_dict["permission_mode"] = permission_mode
        if source_node_id is not UNSET:
            field_dict["source_node_id"] = source_node_id
        if claim_spare is not UNSET:
            field_dict["claim_spare"] = claim_spare
        if workspace_id is not UNSET:
            field_dict["workspace_id"] = workspace_id

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)

        def _parse_title(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        title = _parse_title(d.pop("title", UNSET))

        def _parse_client_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        client_id = _parse_client_id(d.pop("client_id", UNSET))

        def _parse_model(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        model = _parse_model(d.pop("model", UNSET))

        def _parse_effort(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        effort = _parse_effort(d.pop("effort", UNSET))

        def _parse_permission_mode(data: object) -> ChatCreatePermissionModeType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                permission_mode_type_0 = ChatCreatePermissionModeType0(data)

                return permission_mode_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(ChatCreatePermissionModeType0 | None | Unset, data)

        permission_mode = _parse_permission_mode(d.pop("permission_mode", UNSET))

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

        claim_spare = d.pop("claim_spare", UNSET)

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

        chat_create = cls(
            title=title,
            client_id=client_id,
            model=model,
            effort=effort,
            permission_mode=permission_mode,
            source_node_id=source_node_id,
            claim_spare=claim_spare,
            workspace_id=workspace_id,
        )

        chat_create.additional_properties = d
        return chat_create

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
