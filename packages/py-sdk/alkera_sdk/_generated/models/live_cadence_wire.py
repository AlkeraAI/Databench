from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

T = TypeVar("T", bound="LiveCadenceWire")


@_attrs_define
class LiveCadenceWire:
    """How the holder is told to run the live plane: how long to wait for a file
    to stop changing, how often to report, and the three ceilings past which it
    must skeleton or defer rather than upload.

    Served rather than compiled into the client for the same reason the two
    lease cadences are: a deployment that has to slow the plane down — a busy
    org, a store under pressure — changes a setting instead of waiting for
    every box in the field to update. ``inbound`` is what this folder does with
    a write from someone who is not the holder, so a client reads whether to
    drain at all off the same block it reads its cadence from.

        Attributes:
            debounce_ms (int):
            batch_every_ms (int):
            max_batch_entries (int):
            max_file_bytes (int):
            bandwidth_bytes_per_minute (int):
            max_pending_entries (int):
            inbound (bool):
            metadata_every_ms (int):
            metadata_max_entries (int):
            metadata_gzip_bytes (int):
            release_drain_ms (int):
    """

    debounce_ms: int
    batch_every_ms: int
    max_batch_entries: int
    max_file_bytes: int
    bandwidth_bytes_per_minute: int
    max_pending_entries: int
    inbound: bool
    metadata_every_ms: int
    metadata_max_entries: int
    metadata_gzip_bytes: int
    release_drain_ms: int
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        debounce_ms = self.debounce_ms

        batch_every_ms = self.batch_every_ms

        max_batch_entries = self.max_batch_entries

        max_file_bytes = self.max_file_bytes

        bandwidth_bytes_per_minute = self.bandwidth_bytes_per_minute

        max_pending_entries = self.max_pending_entries

        inbound = self.inbound

        metadata_every_ms = self.metadata_every_ms

        metadata_max_entries = self.metadata_max_entries

        metadata_gzip_bytes = self.metadata_gzip_bytes

        release_drain_ms = self.release_drain_ms

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "debounceMs": debounce_ms,
                "batchEveryMs": batch_every_ms,
                "maxBatchEntries": max_batch_entries,
                "maxFileBytes": max_file_bytes,
                "bandwidthBytesPerMinute": bandwidth_bytes_per_minute,
                "maxPendingEntries": max_pending_entries,
                "inbound": inbound,
                "metadataEveryMs": metadata_every_ms,
                "metadataMaxEntries": metadata_max_entries,
                "metadataGzipBytes": metadata_gzip_bytes,
                "releaseDrainMs": release_drain_ms,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        debounce_ms = d.pop("debounceMs")

        batch_every_ms = d.pop("batchEveryMs")

        max_batch_entries = d.pop("maxBatchEntries")

        max_file_bytes = d.pop("maxFileBytes")

        bandwidth_bytes_per_minute = d.pop("bandwidthBytesPerMinute")

        max_pending_entries = d.pop("maxPendingEntries")

        inbound = d.pop("inbound")

        metadata_every_ms = d.pop("metadataEveryMs")

        metadata_max_entries = d.pop("metadataMaxEntries")

        metadata_gzip_bytes = d.pop("metadataGzipBytes")

        release_drain_ms = d.pop("releaseDrainMs")

        live_cadence_wire = cls(
            debounce_ms=debounce_ms,
            batch_every_ms=batch_every_ms,
            max_batch_entries=max_batch_entries,
            max_file_bytes=max_file_bytes,
            bandwidth_bytes_per_minute=bandwidth_bytes_per_minute,
            max_pending_entries=max_pending_entries,
            inbound=inbound,
            metadata_every_ms=metadata_every_ms,
            metadata_max_entries=metadata_max_entries,
            metadata_gzip_bytes=metadata_gzip_bytes,
            release_drain_ms=release_drain_ms,
        )

        live_cadence_wire.additional_properties = d
        return live_cadence_wire

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
