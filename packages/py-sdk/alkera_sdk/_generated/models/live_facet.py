from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.live_facet_content import LiveFacetContent
from ..models.live_facet_state_type_0 import LiveFacetStateType0
from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.live_facet_metadata import LiveFacetMetadata


T = TypeVar("T", bound="LiveFacet")


@_attrs_define
class LiveFacet:
    """What ONE node is on the machine holding the lease, against the drive.

    Present on a node the holder reported (``content``, ``holder_size``,
    ``holder_mtime``) or one it is moving right now (``state`` and the ``box_*``
    pair); absent on every other node, including a folder that merely sits
    inside a leased subtree, which carries the lease facet alone.

    1.1.0 adds the holder's report. ``content`` is derived per read by
    ``alkera_core.files.freshness`` and never stored: ``on_drive`` (the drive
    has the holder's bytes), ``behind`` (it has an older version), ``unlanded``
    (it has none yet), ``unsynced`` (the holder's lease is gone and these bytes
    never arrived) or ``none``. ``state`` became optional with it: a file the
    holder reported and is not moving has no state, and a default of
    ``writing`` would have said it was being written.

        Attributes:
            schema_version (str | Unset):
            metadata (LiveFacetMetadata | Unset):
            state (LiveFacetStateType0 | None | Unset):
            box_size (int | None | Unset):
            box_mtime (datetime.datetime | None | Unset):
            updated_at (datetime.datetime | None | Unset):
            content (LiveFacetContent | Unset):  Default: LiveFacetContent.NONE.
            holder_size (int | None | Unset):
            holder_mtime (datetime.datetime | None | Unset):
    """

    schema_version: str | Unset = UNSET
    metadata: LiveFacetMetadata | Unset = UNSET
    state: LiveFacetStateType0 | None | Unset = UNSET
    box_size: int | None | Unset = UNSET
    box_mtime: datetime.datetime | None | Unset = UNSET
    updated_at: datetime.datetime | None | Unset = UNSET
    content: LiveFacetContent | Unset = LiveFacetContent.NONE
    holder_size: int | None | Unset = UNSET
    holder_mtime: datetime.datetime | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        schema_version = self.schema_version

        metadata: dict[str, Any] | Unset = UNSET
        if not isinstance(self.metadata, Unset):
            metadata = self.metadata.to_dict()

        state: None | str | Unset
        if isinstance(self.state, Unset):
            state = UNSET
        elif isinstance(self.state, LiveFacetStateType0):
            state = self.state.value
        else:
            state = self.state

        box_size: int | None | Unset
        if isinstance(self.box_size, Unset):
            box_size = UNSET
        else:
            box_size = self.box_size

        box_mtime: None | str | Unset
        if isinstance(self.box_mtime, Unset):
            box_mtime = UNSET
        elif isinstance(self.box_mtime, datetime.datetime):
            box_mtime = self.box_mtime.isoformat()
        else:
            box_mtime = self.box_mtime

        updated_at: None | str | Unset
        if isinstance(self.updated_at, Unset):
            updated_at = UNSET
        elif isinstance(self.updated_at, datetime.datetime):
            updated_at = self.updated_at.isoformat()
        else:
            updated_at = self.updated_at

        content: str | Unset = UNSET
        if not isinstance(self.content, Unset):
            content = self.content.value

        holder_size: int | None | Unset
        if isinstance(self.holder_size, Unset):
            holder_size = UNSET
        else:
            holder_size = self.holder_size

        holder_mtime: None | str | Unset
        if isinstance(self.holder_mtime, Unset):
            holder_mtime = UNSET
        elif isinstance(self.holder_mtime, datetime.datetime):
            holder_mtime = self.holder_mtime.isoformat()
        else:
            holder_mtime = self.holder_mtime

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if schema_version is not UNSET:
            field_dict["schema_version"] = schema_version
        if metadata is not UNSET:
            field_dict["metadata"] = metadata
        if state is not UNSET:
            field_dict["state"] = state
        if box_size is not UNSET:
            field_dict["box_size"] = box_size
        if box_mtime is not UNSET:
            field_dict["box_mtime"] = box_mtime
        if updated_at is not UNSET:
            field_dict["updated_at"] = updated_at
        if content is not UNSET:
            field_dict["content"] = content
        if holder_size is not UNSET:
            field_dict["holder_size"] = holder_size
        if holder_mtime is not UNSET:
            field_dict["holder_mtime"] = holder_mtime

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.live_facet_metadata import LiveFacetMetadata

        d = dict(src_dict)
        schema_version = d.pop("schema_version", UNSET)

        _metadata = d.pop("metadata", UNSET)
        metadata: LiveFacetMetadata | Unset
        if isinstance(_metadata, Unset):
            metadata = UNSET
        else:
            metadata = LiveFacetMetadata.from_dict(_metadata)

        def _parse_state(data: object) -> LiveFacetStateType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                state_type_0 = LiveFacetStateType0(data)

                return state_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(LiveFacetStateType0 | None | Unset, data)

        state = _parse_state(d.pop("state", UNSET))

        def _parse_box_size(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        box_size = _parse_box_size(d.pop("box_size", UNSET))

        def _parse_box_mtime(data: object) -> datetime.datetime | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                box_mtime_type_0 = datetime.datetime.fromisoformat(data)

                return box_mtime_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None | Unset, data)

        box_mtime = _parse_box_mtime(d.pop("box_mtime", UNSET))

        def _parse_updated_at(data: object) -> datetime.datetime | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                updated_at_type_0 = datetime.datetime.fromisoformat(data)

                return updated_at_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None | Unset, data)

        updated_at = _parse_updated_at(d.pop("updated_at", UNSET))

        _content = d.pop("content", UNSET)
        content: LiveFacetContent | Unset
        if isinstance(_content, Unset):
            content = UNSET
        else:
            content = LiveFacetContent(_content)

        def _parse_holder_size(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        holder_size = _parse_holder_size(d.pop("holder_size", UNSET))

        def _parse_holder_mtime(data: object) -> datetime.datetime | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                holder_mtime_type_0 = datetime.datetime.fromisoformat(data)

                return holder_mtime_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None | Unset, data)

        holder_mtime = _parse_holder_mtime(d.pop("holder_mtime", UNSET))

        live_facet = cls(
            schema_version=schema_version,
            metadata=metadata,
            state=state,
            box_size=box_size,
            box_mtime=box_mtime,
            updated_at=updated_at,
            content=content,
            holder_size=holder_size,
            holder_mtime=holder_mtime,
        )

        live_facet.additional_properties = d
        return live_facet

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
