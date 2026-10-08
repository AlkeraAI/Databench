from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.name_flags_metadata import NameFlagsMetadata


T = TypeVar("T", bound="NameFlagsWire")


@_attrs_define
class NameFlagsWire:
    """What the server knows about how this name behaves on a client's filesystem.

    Attributes:
        schema_version (str | Unset):
        metadata (NameFlagsMetadata | Unset):
        windows_safe (bool | Unset):  Default: True.
        macos_safe (bool | Unset):  Default: True.
        display_warning (None | str | Unset):
    """

    schema_version: str | Unset = UNSET
    metadata: NameFlagsMetadata | Unset = UNSET
    windows_safe: bool | Unset = True
    macos_safe: bool | Unset = True
    display_warning: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        schema_version = self.schema_version

        metadata: dict[str, Any] | Unset = UNSET
        if not isinstance(self.metadata, Unset):
            metadata = self.metadata.to_dict()

        windows_safe = self.windows_safe

        macos_safe = self.macos_safe

        display_warning: None | str | Unset
        if isinstance(self.display_warning, Unset):
            display_warning = UNSET
        else:
            display_warning = self.display_warning

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if schema_version is not UNSET:
            field_dict["schema_version"] = schema_version
        if metadata is not UNSET:
            field_dict["metadata"] = metadata
        if windows_safe is not UNSET:
            field_dict["windows_safe"] = windows_safe
        if macos_safe is not UNSET:
            field_dict["macos_safe"] = macos_safe
        if display_warning is not UNSET:
            field_dict["display_warning"] = display_warning

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.name_flags_metadata import NameFlagsMetadata

        d = dict(src_dict)
        schema_version = d.pop("schema_version", UNSET)

        _metadata = d.pop("metadata", UNSET)
        metadata: NameFlagsMetadata | Unset
        if isinstance(_metadata, Unset):
            metadata = UNSET
        else:
            metadata = NameFlagsMetadata.from_dict(_metadata)

        windows_safe = d.pop("windows_safe", UNSET)

        macos_safe = d.pop("macos_safe", UNSET)

        def _parse_display_warning(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        display_warning = _parse_display_warning(d.pop("display_warning", UNSET))

        name_flags_wire = cls(
            schema_version=schema_version,
            metadata=metadata,
            windows_safe=windows_safe,
            macos_safe=macos_safe,
            display_warning=display_warning,
        )

        name_flags_wire.additional_properties = d
        return name_flags_wire

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
