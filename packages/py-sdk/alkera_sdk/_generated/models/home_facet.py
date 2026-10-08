from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.home_facet_metadata import HomeFacetMetadata


T = TypeVar("T", bound="HomeFacet")


@_attrs_define
class HomeFacet:
    """Present on a member's home folder, and on nothing else.

    A home's stored name is its owner's id — an address, never a label. What a
    person reads for it is ``owner_name``: the owner's CURRENT display name,
    resolved when the payload is rendered, so a changed name shows at once and
    nothing about the account is ever written into the tree. Every surface
    that shows a node's name shows this instead when it is present.

        Attributes:
            schema_version (str | Unset):
            metadata (HomeFacetMetadata | Unset):
            node_id (str | Unset):  Default: ''.
            owner_id (str | Unset):  Default: ''.
            owner_name (str | Unset):  Default: ''.
    """

    schema_version: str | Unset = UNSET
    metadata: HomeFacetMetadata | Unset = UNSET
    node_id: str | Unset = ""
    owner_id: str | Unset = ""
    owner_name: str | Unset = ""
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        schema_version = self.schema_version

        metadata: dict[str, Any] | Unset = UNSET
        if not isinstance(self.metadata, Unset):
            metadata = self.metadata.to_dict()

        node_id = self.node_id

        owner_id = self.owner_id

        owner_name = self.owner_name

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if schema_version is not UNSET:
            field_dict["schema_version"] = schema_version
        if metadata is not UNSET:
            field_dict["metadata"] = metadata
        if node_id is not UNSET:
            field_dict["node_id"] = node_id
        if owner_id is not UNSET:
            field_dict["owner_id"] = owner_id
        if owner_name is not UNSET:
            field_dict["owner_name"] = owner_name

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.home_facet_metadata import HomeFacetMetadata

        d = dict(src_dict)
        schema_version = d.pop("schema_version", UNSET)

        _metadata = d.pop("metadata", UNSET)
        metadata: HomeFacetMetadata | Unset
        if isinstance(_metadata, Unset):
            metadata = UNSET
        else:
            metadata = HomeFacetMetadata.from_dict(_metadata)

        node_id = d.pop("node_id", UNSET)

        owner_id = d.pop("owner_id", UNSET)

        owner_name = d.pop("owner_name", UNSET)

        home_facet = cls(
            schema_version=schema_version,
            metadata=metadata,
            node_id=node_id,
            owner_id=owner_id,
            owner_name=owner_name,
        )

        home_facet.additional_properties = d
        return home_facet

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
