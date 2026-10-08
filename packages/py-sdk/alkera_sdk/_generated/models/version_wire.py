from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="VersionWire")


@_attrs_define
class VersionWire:
    """One row of a file's history. `isHead` is the version the node points at.

    Attributes:
        id (str):
        seq (int):
        size (int):
        created_at (str):
        content_hash (None | str | Unset):
        mime_type (None | str | Unset):
        scan_state (None | str | Unset):
        source (None | str | Unset):
        is_head (bool | Unset):  Default: False.
        author (None | str | Unset):
        machine (None | str | Unset):
    """

    id: str
    seq: int
    size: int
    created_at: str
    content_hash: None | str | Unset = UNSET
    mime_type: None | str | Unset = UNSET
    scan_state: None | str | Unset = UNSET
    source: None | str | Unset = UNSET
    is_head: bool | Unset = False
    author: None | str | Unset = UNSET
    machine: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        id = self.id

        seq = self.seq

        size = self.size

        created_at = self.created_at

        content_hash: None | str | Unset
        if isinstance(self.content_hash, Unset):
            content_hash = UNSET
        else:
            content_hash = self.content_hash

        mime_type: None | str | Unset
        if isinstance(self.mime_type, Unset):
            mime_type = UNSET
        else:
            mime_type = self.mime_type

        scan_state: None | str | Unset
        if isinstance(self.scan_state, Unset):
            scan_state = UNSET
        else:
            scan_state = self.scan_state

        source: None | str | Unset
        if isinstance(self.source, Unset):
            source = UNSET
        else:
            source = self.source

        is_head = self.is_head

        author: None | str | Unset
        if isinstance(self.author, Unset):
            author = UNSET
        else:
            author = self.author

        machine: None | str | Unset
        if isinstance(self.machine, Unset):
            machine = UNSET
        else:
            machine = self.machine

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "seq": seq,
                "size": size,
                "createdAt": created_at,
            }
        )
        if content_hash is not UNSET:
            field_dict["contentHash"] = content_hash
        if mime_type is not UNSET:
            field_dict["mimeType"] = mime_type
        if scan_state is not UNSET:
            field_dict["scanState"] = scan_state
        if source is not UNSET:
            field_dict["source"] = source
        if is_head is not UNSET:
            field_dict["isHead"] = is_head
        if author is not UNSET:
            field_dict["author"] = author
        if machine is not UNSET:
            field_dict["machine"] = machine

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        id = d.pop("id")

        seq = d.pop("seq")

        size = d.pop("size")

        created_at = d.pop("createdAt")

        def _parse_content_hash(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        content_hash = _parse_content_hash(d.pop("contentHash", UNSET))

        def _parse_mime_type(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        mime_type = _parse_mime_type(d.pop("mimeType", UNSET))

        def _parse_scan_state(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        scan_state = _parse_scan_state(d.pop("scanState", UNSET))

        def _parse_source(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        source = _parse_source(d.pop("source", UNSET))

        is_head = d.pop("isHead", UNSET)

        def _parse_author(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        author = _parse_author(d.pop("author", UNSET))

        def _parse_machine(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        machine = _parse_machine(d.pop("machine", UNSET))

        version_wire = cls(
            id=id,
            seq=seq,
            size=size,
            created_at=created_at,
            content_hash=content_hash,
            mime_type=mime_type,
            scan_state=scan_state,
            source=source,
            is_head=is_head,
            author=author,
            machine=machine,
        )

        version_wire.additional_properties = d
        return version_wire

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
