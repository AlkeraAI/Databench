from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast
from uuid import UUID

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="OrgLiveEditingRead")


@_attrs_define
class OrgLiveEditingRead:
    """Where an org stands on live editing, as the platform sees it.

    Attributes:
        org_id (UUID):
        enabled (bool):
        override (bool | None):
        deployment_default (bool):
        sessions_written (int | None | Unset):
        sessions_left_unsaved (int | None | Unset):
    """

    org_id: UUID
    enabled: bool
    override: bool | None
    deployment_default: bool
    sessions_written: int | None | Unset = UNSET
    sessions_left_unsaved: int | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        org_id = str(self.org_id)

        enabled = self.enabled

        override: bool | None
        override = self.override

        deployment_default = self.deployment_default

        sessions_written: int | None | Unset
        if isinstance(self.sessions_written, Unset):
            sessions_written = UNSET
        else:
            sessions_written = self.sessions_written

        sessions_left_unsaved: int | None | Unset
        if isinstance(self.sessions_left_unsaved, Unset):
            sessions_left_unsaved = UNSET
        else:
            sessions_left_unsaved = self.sessions_left_unsaved

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "org_id": org_id,
                "enabled": enabled,
                "override": override,
                "deployment_default": deployment_default,
            }
        )
        if sessions_written is not UNSET:
            field_dict["sessions_written"] = sessions_written
        if sessions_left_unsaved is not UNSET:
            field_dict["sessions_left_unsaved"] = sessions_left_unsaved

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        org_id = UUID(d.pop("org_id"))

        enabled = d.pop("enabled")

        def _parse_override(data: object) -> bool | None:
            if data is None:
                return data
            return cast(bool | None, data)

        override = _parse_override(d.pop("override"))

        deployment_default = d.pop("deployment_default")

        def _parse_sessions_written(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        sessions_written = _parse_sessions_written(d.pop("sessions_written", UNSET))

        def _parse_sessions_left_unsaved(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        sessions_left_unsaved = _parse_sessions_left_unsaved(d.pop("sessions_left_unsaved", UNSET))

        org_live_editing_read = cls(
            org_id=org_id,
            enabled=enabled,
            override=override,
            deployment_default=deployment_default,
            sessions_written=sessions_written,
            sessions_left_unsaved=sessions_left_unsaved,
        )

        org_live_editing_read.additional_properties = d
        return org_live_editing_read

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
