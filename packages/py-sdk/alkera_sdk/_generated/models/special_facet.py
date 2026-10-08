from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.special_facet_metadata import SpecialFacetMetadata


T = TypeVar("T", bound="SpecialFacet")


@_attrs_define
class SpecialFacet:
    """A device, fifo or socket node.

    ``rdev`` is the POSIX device number a materializer needs to recreate the
    node; the server never opens one, so nothing here is followed.

        Attributes:
            schema_version (str | Unset):
            metadata (SpecialFacetMetadata | Unset):
            type_ (str | Unset):  Default: ''.
            rdev (int | Unset):  Default: 0.
    """

    schema_version: str | Unset = UNSET
    metadata: SpecialFacetMetadata | Unset = UNSET
    type_: str | Unset = ""
    rdev: int | Unset = 0
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        schema_version = self.schema_version

        metadata: dict[str, Any] | Unset = UNSET
        if not isinstance(self.metadata, Unset):
            metadata = self.metadata.to_dict()

        type_ = self.type_

        rdev = self.rdev

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if schema_version is not UNSET:
            field_dict["schema_version"] = schema_version
        if metadata is not UNSET:
            field_dict["metadata"] = metadata
        if type_ is not UNSET:
            field_dict["type"] = type_
        if rdev is not UNSET:
            field_dict["rdev"] = rdev

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.special_facet_metadata import SpecialFacetMetadata

        d = dict(src_dict)
        schema_version = d.pop("schema_version", UNSET)

        _metadata = d.pop("metadata", UNSET)
        metadata: SpecialFacetMetadata | Unset
        if isinstance(_metadata, Unset):
            metadata = UNSET
        else:
            metadata = SpecialFacetMetadata.from_dict(_metadata)

        type_ = d.pop("type", UNSET)

        rdev = d.pop("rdev", UNSET)

        special_facet = cls(
            schema_version=schema_version,
            metadata=metadata,
            type_=type_,
            rdev=rdev,
        )

        special_facet.additional_properties = d
        return special_facet

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
