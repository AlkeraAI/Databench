from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.user_preferences_patch import UserPreferencesPatch


T = TypeVar("T", bound="PreferencesUpdate")


@_attrs_define
class PreferencesUpdate:
    """The fields to apply. MERGED onto what is stored, never a replacement —
    so a key a newer client wrote survives an older client's save.

    ``extra="forbid"`` where the persisted :class:`Preferences` allows extras,
    and the two are not in tension: the STORED document must keep a field it
    cannot name (a newer client wrote it), while a REQUEST that misses the
    envelope has nothing to preserve and everything to lose. A client PATCHing
    the flat ``{"default_permission_mode": …}`` instead of
    ``{"preferences": {…}}`` gets a 422 rather than a 200 with an empty merge
    that silently drops the setting.

        Attributes:
            preferences (UserPreferencesPatch | Unset):
    """

    preferences: UserPreferencesPatch | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        preferences: dict[str, Any] | Unset = UNSET
        if not isinstance(self.preferences, Unset):
            preferences = self.preferences.to_dict()

        field_dict: dict[str, Any] = {}

        field_dict.update({})
        if preferences is not UNSET:
            field_dict["preferences"] = preferences

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.user_preferences_patch import UserPreferencesPatch

        d = dict(src_dict)
        _preferences = d.pop("preferences", UNSET)
        preferences: UserPreferencesPatch | Unset
        if isinstance(_preferences, Unset):
            preferences = UNSET
        else:
            preferences = UserPreferencesPatch.from_dict(_preferences)

        preferences_update = cls(
            preferences=preferences,
        )

        return preferences_update
