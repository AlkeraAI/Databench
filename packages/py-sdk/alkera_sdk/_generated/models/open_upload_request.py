from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast
from uuid import UUID

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="OpenUploadRequest")


@_attrs_define
class OpenUploadRequest:
    """
    Attributes:
        declared_size (int):
        name (str):
        parent_id (UUID):
        mime (None | str | Unset):
        conflict_of (None | Unset | UUID):
        conflict_copy (bool | Unset):  Default: False.
    """

    declared_size: int
    name: str
    parent_id: UUID
    mime: None | str | Unset = UNSET
    conflict_of: None | Unset | UUID = UNSET
    conflict_copy: bool | Unset = False
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        declared_size = self.declared_size

        name = self.name

        parent_id = str(self.parent_id)

        mime: None | str | Unset
        if isinstance(self.mime, Unset):
            mime = UNSET
        else:
            mime = self.mime

        conflict_of: None | str | Unset
        if isinstance(self.conflict_of, Unset):
            conflict_of = UNSET
        elif isinstance(self.conflict_of, UUID):
            conflict_of = str(self.conflict_of)
        else:
            conflict_of = self.conflict_of

        conflict_copy = self.conflict_copy

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "declaredSize": declared_size,
                "name": name,
                "parentId": parent_id,
            }
        )
        if mime is not UNSET:
            field_dict["mime"] = mime
        if conflict_of is not UNSET:
            field_dict["conflictOf"] = conflict_of
        if conflict_copy is not UNSET:
            field_dict["conflictCopy"] = conflict_copy

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        declared_size = d.pop("declaredSize")

        name = d.pop("name")

        parent_id = UUID(d.pop("parentId"))

        def _parse_mime(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        mime = _parse_mime(d.pop("mime", UNSET))

        def _parse_conflict_of(data: object) -> None | Unset | UUID:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                conflict_of_type_0 = UUID(data)

                return conflict_of_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | Unset | UUID, data)

        conflict_of = _parse_conflict_of(d.pop("conflictOf", UNSET))

        conflict_copy = d.pop("conflictCopy", UNSET)

        open_upload_request = cls(
            declared_size=declared_size,
            name=name,
            parent_id=parent_id,
            mime=mime,
            conflict_of=conflict_of,
            conflict_copy=conflict_copy,
        )

        open_upload_request.additional_properties = d
        return open_upload_request

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
