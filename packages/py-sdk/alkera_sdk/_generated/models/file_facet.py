from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.file_facet_scan_state import FileFacetScanState
from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.file_facet_metadata import FileFacetMetadata


T = TypeVar("T", bound="FileFacet")


@_attrs_define
class FileFacet:
    """Content facts. `mime_type` is what the server sniffed, never what a client claimed.

    Attributes:
        schema_version (str | Unset):
        metadata (FileFacetMetadata | Unset):
        mime_type (str | Unset):  Default: 'application/octet-stream'.
        size (int | Unset):  Default: 0.
        content_hash (str | Unset):  Default: ''.
        block_hash (None | str | Unset):
        scan_state (FileFacetScanState | Unset):  Default: FileFacetScanState.PENDING.
        provider (str | Unset):  Default: 'bytes'.
    """

    schema_version: str | Unset = UNSET
    metadata: FileFacetMetadata | Unset = UNSET
    mime_type: str | Unset = "application/octet-stream"
    size: int | Unset = 0
    content_hash: str | Unset = ""
    block_hash: None | str | Unset = UNSET
    scan_state: FileFacetScanState | Unset = FileFacetScanState.PENDING
    provider: str | Unset = "bytes"
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        schema_version = self.schema_version

        metadata: dict[str, Any] | Unset = UNSET
        if not isinstance(self.metadata, Unset):
            metadata = self.metadata.to_dict()

        mime_type = self.mime_type

        size = self.size

        content_hash = self.content_hash

        block_hash: None | str | Unset
        if isinstance(self.block_hash, Unset):
            block_hash = UNSET
        else:
            block_hash = self.block_hash

        scan_state: str | Unset = UNSET
        if not isinstance(self.scan_state, Unset):
            scan_state = self.scan_state.value

        provider = self.provider

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if schema_version is not UNSET:
            field_dict["schema_version"] = schema_version
        if metadata is not UNSET:
            field_dict["metadata"] = metadata
        if mime_type is not UNSET:
            field_dict["mime_type"] = mime_type
        if size is not UNSET:
            field_dict["size"] = size
        if content_hash is not UNSET:
            field_dict["content_hash"] = content_hash
        if block_hash is not UNSET:
            field_dict["block_hash"] = block_hash
        if scan_state is not UNSET:
            field_dict["scan_state"] = scan_state
        if provider is not UNSET:
            field_dict["provider"] = provider

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.file_facet_metadata import FileFacetMetadata

        d = dict(src_dict)
        schema_version = d.pop("schema_version", UNSET)

        _metadata = d.pop("metadata", UNSET)
        metadata: FileFacetMetadata | Unset
        if isinstance(_metadata, Unset):
            metadata = UNSET
        else:
            metadata = FileFacetMetadata.from_dict(_metadata)

        mime_type = d.pop("mime_type", UNSET)

        size = d.pop("size", UNSET)

        content_hash = d.pop("content_hash", UNSET)

        def _parse_block_hash(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        block_hash = _parse_block_hash(d.pop("block_hash", UNSET))

        _scan_state = d.pop("scan_state", UNSET)
        scan_state: FileFacetScanState | Unset
        if isinstance(_scan_state, Unset):
            scan_state = UNSET
        else:
            scan_state = FileFacetScanState(_scan_state)

        provider = d.pop("provider", UNSET)

        file_facet = cls(
            schema_version=schema_version,
            metadata=metadata,
            mime_type=mime_type,
            size=size,
            content_hash=content_hash,
            block_hash=block_hash,
            scan_state=scan_state,
            provider=provider,
        )

        file_facet.additional_properties = d
        return file_facet

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
