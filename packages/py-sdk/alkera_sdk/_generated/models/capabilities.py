from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.capabilities_metadata import CapabilitiesMetadata
    from ..models.refusals import Refusals


T = TypeVar("T", bound="Capabilities")


@_attrs_define
class Capabilities:
    """What this caller may do with this node, and why not when they may not.

    `refusals` is surface-specific: the same node can be undownloadable on a
    link and downloadable in the portal, and the UI shows the reason rather
    than a dead button.

        Attributes:
            schema_version (str | Unset):
            metadata (CapabilitiesMetadata | Unset):
            can_read (bool | Unset):  Default: False.
            can_write (bool | Unset):  Default: False.
            can_share (bool | Unset):  Default: False.
            can_delete (bool | Unset):  Default: False.
            can_purge (bool | Unset):  Default: False.
            can_rename (bool | Unset):  Default: False.
            can_download (bool | Unset):  Default: False.
            can_lease (bool | Unset):  Default: False.
            can_lease_request (bool | Unset):  Default: False.
            can_lease_force (bool | Unset):  Default: False.
            refusals (Refusals | Unset):
    """

    schema_version: str | Unset = UNSET
    metadata: CapabilitiesMetadata | Unset = UNSET
    can_read: bool | Unset = False
    can_write: bool | Unset = False
    can_share: bool | Unset = False
    can_delete: bool | Unset = False
    can_purge: bool | Unset = False
    can_rename: bool | Unset = False
    can_download: bool | Unset = False
    can_lease: bool | Unset = False
    can_lease_request: bool | Unset = False
    can_lease_force: bool | Unset = False
    refusals: Refusals | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        schema_version = self.schema_version

        metadata: dict[str, Any] | Unset = UNSET
        if not isinstance(self.metadata, Unset):
            metadata = self.metadata.to_dict()

        can_read = self.can_read

        can_write = self.can_write

        can_share = self.can_share

        can_delete = self.can_delete

        can_purge = self.can_purge

        can_rename = self.can_rename

        can_download = self.can_download

        can_lease = self.can_lease

        can_lease_request = self.can_lease_request

        can_lease_force = self.can_lease_force

        refusals: dict[str, Any] | Unset = UNSET
        if not isinstance(self.refusals, Unset):
            refusals = self.refusals.to_dict()

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if schema_version is not UNSET:
            field_dict["schema_version"] = schema_version
        if metadata is not UNSET:
            field_dict["metadata"] = metadata
        if can_read is not UNSET:
            field_dict["can_read"] = can_read
        if can_write is not UNSET:
            field_dict["can_write"] = can_write
        if can_share is not UNSET:
            field_dict["can_share"] = can_share
        if can_delete is not UNSET:
            field_dict["can_delete"] = can_delete
        if can_purge is not UNSET:
            field_dict["can_purge"] = can_purge
        if can_rename is not UNSET:
            field_dict["can_rename"] = can_rename
        if can_download is not UNSET:
            field_dict["can_download"] = can_download
        if can_lease is not UNSET:
            field_dict["can_lease"] = can_lease
        if can_lease_request is not UNSET:
            field_dict["can_lease_request"] = can_lease_request
        if can_lease_force is not UNSET:
            field_dict["can_lease_force"] = can_lease_force
        if refusals is not UNSET:
            field_dict["refusals"] = refusals

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.capabilities_metadata import CapabilitiesMetadata
        from ..models.refusals import Refusals

        d = dict(src_dict)
        schema_version = d.pop("schema_version", UNSET)

        _metadata = d.pop("metadata", UNSET)
        metadata: CapabilitiesMetadata | Unset
        if isinstance(_metadata, Unset):
            metadata = UNSET
        else:
            metadata = CapabilitiesMetadata.from_dict(_metadata)

        can_read = d.pop("can_read", UNSET)

        can_write = d.pop("can_write", UNSET)

        can_share = d.pop("can_share", UNSET)

        can_delete = d.pop("can_delete", UNSET)

        can_purge = d.pop("can_purge", UNSET)

        can_rename = d.pop("can_rename", UNSET)

        can_download = d.pop("can_download", UNSET)

        can_lease = d.pop("can_lease", UNSET)

        can_lease_request = d.pop("can_lease_request", UNSET)

        can_lease_force = d.pop("can_lease_force", UNSET)

        _refusals = d.pop("refusals", UNSET)
        refusals: Refusals | Unset
        if isinstance(_refusals, Unset):
            refusals = UNSET
        else:
            refusals = Refusals.from_dict(_refusals)

        capabilities = cls(
            schema_version=schema_version,
            metadata=metadata,
            can_read=can_read,
            can_write=can_write,
            can_share=can_share,
            can_delete=can_delete,
            can_purge=can_purge,
            can_rename=can_rename,
            can_download=can_download,
            can_lease=can_lease,
            can_lease_request=can_lease_request,
            can_lease_force=can_lease_force,
            refusals=refusals,
        )

        capabilities.additional_properties = d
        return capabilities

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
