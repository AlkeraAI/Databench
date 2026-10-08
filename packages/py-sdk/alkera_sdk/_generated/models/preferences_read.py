from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.user_preferences import UserPreferences


T = TypeVar("T", bound="PreferencesRead")


@_attrs_define
class PreferencesRead:
    """The caller's stored preferences, whole.

    A free-form object on purpose: :class:`Preferences` allows unknown fields so
    a newer client's key survives an older one's read, and a typed mirror here
    would be the thing that dropped it.

        Attributes:
            preferences (UserPreferences | Unset):
            chat_model_defaulted (bool | Unset): True when `default_chat_model` / `default_chat_effort` in this answer are
                the platform default rather than the caller's own pick — they never chose one, or the one they chose is no
                longer offered. A client that edits the document shows this as 'no preference' and must not save the answered
                model back as a choice. Default: False.
    """

    preferences: UserPreferences | Unset = UNSET
    chat_model_defaulted: bool | Unset = False
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        preferences: dict[str, Any] | Unset = UNSET
        if not isinstance(self.preferences, Unset):
            preferences = self.preferences.to_dict()

        chat_model_defaulted = self.chat_model_defaulted

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if preferences is not UNSET:
            field_dict["preferences"] = preferences
        if chat_model_defaulted is not UNSET:
            field_dict["chat_model_defaulted"] = chat_model_defaulted

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.user_preferences import UserPreferences

        d = dict(src_dict)
        _preferences = d.pop("preferences", UNSET)
        preferences: UserPreferences | Unset
        if isinstance(_preferences, Unset):
            preferences = UNSET
        else:
            preferences = UserPreferences.from_dict(_preferences)

        chat_model_defaulted = d.pop("chat_model_defaulted", UNSET)

        preferences_read = cls(
            preferences=preferences,
            chat_model_defaulted=chat_model_defaulted,
        )

        preferences_read.additional_properties = d
        return preferences_read

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
