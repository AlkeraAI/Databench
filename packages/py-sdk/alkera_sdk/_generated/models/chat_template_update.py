from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="ChatTemplateUpdate")


@_attrs_define
class ChatTemplateUpdate:
    """``expected_version`` is required and never optional: a write that does
    not say which row it read is a write that did not read one.

    Only the two fields a person writes are here. A template's files are
    ordinary files in its folder and are edited there; its spec is not editable
    through the object surface at all.

        Attributes:
            expected_version (int):
            title (None | str | Unset):
            brief (None | str | Unset):
    """

    expected_version: int
    title: None | str | Unset = UNSET
    brief: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        expected_version = self.expected_version

        title: None | str | Unset
        if isinstance(self.title, Unset):
            title = UNSET
        else:
            title = self.title

        brief: None | str | Unset
        if isinstance(self.brief, Unset):
            brief = UNSET
        else:
            brief = self.brief

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "expected_version": expected_version,
            }
        )
        if title is not UNSET:
            field_dict["title"] = title
        if brief is not UNSET:
            field_dict["brief"] = brief

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        expected_version = d.pop("expected_version")

        def _parse_title(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        title = _parse_title(d.pop("title", UNSET))

        def _parse_brief(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        brief = _parse_brief(d.pop("brief", UNSET))

        chat_template_update = cls(
            expected_version=expected_version,
            title=title,
            brief=brief,
        )

        chat_template_update.additional_properties = d
        return chat_template_update

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
