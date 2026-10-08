from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.chat_permission_mode_update_mode import ChatPermissionModeUpdateMode

T = TypeVar("T", bound="ChatPermissionModeUpdate")


@_attrs_define
class ChatPermissionModeUpdate:
    """The stance a reader puts a chat's session in.

    Only a stance a cloud chat may run in is spellable (``CloudPermissionMode``).
    A body naming anything else — a word a newer client invented, a retired one,
    a different casing — is a 422, not a silent downgrade to something the
    reader did not ask for.

        Attributes:
            mode (ChatPermissionModeUpdateMode):
    """

    mode: ChatPermissionModeUpdateMode
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        mode = self.mode.value

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "mode": mode,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        mode = ChatPermissionModeUpdateMode(d.pop("mode"))

        chat_permission_mode_update = cls(
            mode=mode,
        )

        chat_permission_mode_update.additional_properties = d
        return chat_permission_mode_update

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
