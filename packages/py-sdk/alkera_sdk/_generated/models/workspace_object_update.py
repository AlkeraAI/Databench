from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.workspace_object_update_spec_type_0 import WorkspaceObjectUpdateSpecType0


T = TypeVar("T", bound="WorkspaceObjectUpdate")


@_attrs_define
class WorkspaceObjectUpdate:
    """``expected_version`` is required and never optional: a write that does
    not say which row it read is a write that did not read one.

        Attributes:
            expected_version (int):
            title (None | str | Unset):
            spec (None | Unset | WorkspaceObjectUpdateSpecType0):
    """

    expected_version: int
    title: None | str | Unset = UNSET
    spec: None | Unset | WorkspaceObjectUpdateSpecType0 = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.workspace_object_update_spec_type_0 import (
            WorkspaceObjectUpdateSpecType0,
        )

        expected_version = self.expected_version

        title: None | str | Unset
        if isinstance(self.title, Unset):
            title = UNSET
        else:
            title = self.title

        spec: dict[str, Any] | None | Unset
        if isinstance(self.spec, Unset):
            spec = UNSET
        elif isinstance(self.spec, WorkspaceObjectUpdateSpecType0):
            spec = self.spec.to_dict()
        else:
            spec = self.spec

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "expected_version": expected_version,
            }
        )
        if title is not UNSET:
            field_dict["title"] = title
        if spec is not UNSET:
            field_dict["spec"] = spec

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.workspace_object_update_spec_type_0 import (
            WorkspaceObjectUpdateSpecType0,
        )

        d = dict(src_dict)
        expected_version = d.pop("expected_version")

        def _parse_title(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        title = _parse_title(d.pop("title", UNSET))

        def _parse_spec(data: object) -> None | Unset | WorkspaceObjectUpdateSpecType0:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                spec_type_0 = WorkspaceObjectUpdateSpecType0.from_dict(data)

                return spec_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | Unset | WorkspaceObjectUpdateSpecType0, data)

        spec = _parse_spec(d.pop("spec", UNSET))

        workspace_object_update = cls(
            expected_version=expected_version,
            title=title,
            spec=spec,
        )

        workspace_object_update.additional_properties = d
        return workspace_object_update

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
