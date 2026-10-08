from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.attrs_patch import AttrsPatch


T = TypeVar("T", bound="PatchItem")


@_attrs_define
class PatchItem:
    """The `PATCH` body: a rename, a move, an attrs change, or any combination.

    Attributes:
        name (None | str | Unset):
        parent_id (None | str | Unset):
        attrs (AttrsPatch | None | Unset):
    """

    name: None | str | Unset = UNSET
    parent_id: None | str | Unset = UNSET
    attrs: AttrsPatch | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.attrs_patch import AttrsPatch

        name: None | str | Unset
        if isinstance(self.name, Unset):
            name = UNSET
        else:
            name = self.name

        parent_id: None | str | Unset
        if isinstance(self.parent_id, Unset):
            parent_id = UNSET
        else:
            parent_id = self.parent_id

        attrs: dict[str, Any] | None | Unset
        if isinstance(self.attrs, Unset):
            attrs = UNSET
        elif isinstance(self.attrs, AttrsPatch):
            attrs = self.attrs.to_dict()
        else:
            attrs = self.attrs

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if name is not UNSET:
            field_dict["name"] = name
        if parent_id is not UNSET:
            field_dict["parentId"] = parent_id
        if attrs is not UNSET:
            field_dict["attrs"] = attrs

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.attrs_patch import AttrsPatch

        d = dict(src_dict)

        def _parse_name(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        name = _parse_name(d.pop("name", UNSET))

        def _parse_parent_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        parent_id = _parse_parent_id(d.pop("parentId", UNSET))

        def _parse_attrs(data: object) -> AttrsPatch | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                attrs_type_0 = AttrsPatch.from_dict(data)

                return attrs_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(AttrsPatch | None | Unset, data)

        attrs = _parse_attrs(d.pop("attrs", UNSET))

        patch_item = cls(
            name=name,
            parent_id=parent_id,
            attrs=attrs,
        )

        patch_item.additional_properties = d
        return patch_item

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
