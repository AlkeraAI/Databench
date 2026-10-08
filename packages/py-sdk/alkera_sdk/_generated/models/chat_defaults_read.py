from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.chat_defaults_read_permission_mode_type_0 import ChatDefaultsReadPermissionModeType0
from ..types import UNSET, Unset

T = TypeVar("T", bound="ChatDefaultsRead")


@_attrs_define
class ChatDefaultsRead:
    """The new-chat seed: the saved Default Chat Model + Effort, resolved
    against the live catalog.

    ``None`` on either field means "nothing to seed" — the composer falls back
    to the first catalog model. A gateway outage returns the SAVED values
    untouched rather than resetting them, which is the whole reason this is
    resolved on the server and not in the browser.

    ``permission_mode`` is the stance a chat created NOW would open in, resolved
    by the same function the create route uses. A composer that has no chat yet
    can only state the stance by asking someone who knows, and the only thing
    that knows is the resolver — a browser-side constant would be a second
    answer to a question the server already decides, and the two disagree the
    moment a reader saves a default.

        Attributes:
            model (None | str | Unset):
            effort (None | str | Unset):
            permission_mode (ChatDefaultsReadPermissionModeType0 | None | Unset):
    """

    model: None | str | Unset = UNSET
    effort: None | str | Unset = UNSET
    permission_mode: ChatDefaultsReadPermissionModeType0 | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
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
        elif isinstance(self.permission_mode, ChatDefaultsReadPermissionModeType0):
            permission_mode = self.permission_mode.value
        else:
            permission_mode = self.permission_mode

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if model is not UNSET:
            field_dict["model"] = model
        if effort is not UNSET:
            field_dict["effort"] = effort
        if permission_mode is not UNSET:
            field_dict["permission_mode"] = permission_mode

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)

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

        def _parse_permission_mode(
            data: object,
        ) -> ChatDefaultsReadPermissionModeType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                permission_mode_type_0 = ChatDefaultsReadPermissionModeType0(data)

                return permission_mode_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(ChatDefaultsReadPermissionModeType0 | None | Unset, data)

        permission_mode = _parse_permission_mode(d.pop("permission_mode", UNSET))

        chat_defaults_read = cls(
            model=model,
            effort=effort,
            permission_mode=permission_mode,
        )

        chat_defaults_read.additional_properties = d
        return chat_defaults_read

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
