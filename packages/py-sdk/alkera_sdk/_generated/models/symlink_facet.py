from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.symlink_facet_kind import SymlinkFacetKind
from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.symlink_facet_metadata import SymlinkFacetMetadata


T = TypeVar("T", bound="SymlinkFacet")


@_attrs_define
class SymlinkFacet:
    """A symlink's stored target and what its text points at.

    ``kind`` is the tree's own vocabulary (``file_nodes.symlink_kind``:
    ``relative`` — resolved against this directory, ``canonical`` — an absolute
    path in the org's namespace that a materializer rewrites to the local root,
    ``host`` — a path on some machine, stored and recreated verbatim, never
    followed server-side). The wire maps 1:1 to what is stored, so
    ``POST …/children`` takes the kind it reads back. 1.0.0 spelled the three
    ``internal | external | absolute``; the migration maps them forward.

        Attributes:
            schema_version (str | Unset):
            metadata (SymlinkFacetMetadata | Unset):
            target (str | Unset):  Default: ''.
            kind (SymlinkFacetKind | Unset):  Default: SymlinkFacetKind.RELATIVE.
    """

    schema_version: str | Unset = UNSET
    metadata: SymlinkFacetMetadata | Unset = UNSET
    target: str | Unset = ""
    kind: SymlinkFacetKind | Unset = SymlinkFacetKind.RELATIVE
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        schema_version = self.schema_version

        metadata: dict[str, Any] | Unset = UNSET
        if not isinstance(self.metadata, Unset):
            metadata = self.metadata.to_dict()

        target = self.target

        kind: str | Unset = UNSET
        if not isinstance(self.kind, Unset):
            kind = self.kind.value

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if schema_version is not UNSET:
            field_dict["schema_version"] = schema_version
        if metadata is not UNSET:
            field_dict["metadata"] = metadata
        if target is not UNSET:
            field_dict["target"] = target
        if kind is not UNSET:
            field_dict["kind"] = kind

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.symlink_facet_metadata import SymlinkFacetMetadata

        d = dict(src_dict)
        schema_version = d.pop("schema_version", UNSET)

        _metadata = d.pop("metadata", UNSET)
        metadata: SymlinkFacetMetadata | Unset
        if isinstance(_metadata, Unset):
            metadata = UNSET
        else:
            metadata = SymlinkFacetMetadata.from_dict(_metadata)

        target = d.pop("target", UNSET)

        _kind = d.pop("kind", UNSET)
        kind: SymlinkFacetKind | Unset
        if isinstance(_kind, Unset):
            kind = UNSET
        else:
            kind = SymlinkFacetKind(_kind)

        symlink_facet = cls(
            schema_version=schema_version,
            metadata=metadata,
            target=target,
            kind=kind,
        )

        symlink_facet.additional_properties = d
        return symlink_facet

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
