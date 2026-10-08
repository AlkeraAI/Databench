from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast
from uuid import UUID

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.lease_facet_purpose import LeaseFacetPurpose
from ..models.lease_facet_served import LeaseFacetServed
from ..models.lease_facet_yours import LeaseFacetYours
from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.lease_facet_metadata import LeaseFacetMetadata
    from ..models.status_fact import StatusFact


T = TypeVar("T", bound="LeaseFacet")


@_attrs_define
class LeaseFacet:
    """Carried by the leased folder and everything under it, so a client can
    explain why a write would be refused before it attempts one.

    1.2.0 names the chat whose folder the lease holds: its id, its current
    title, and whether THIS caller may open it. A folder a box is running a
    chat in is read-only to a browser for the whole lease, and the browser has
    to say which conversation holds it and link there for the people who may
    follow the link. The permission is the chat policy's own READ answer,
    resolved for the caller by the route; a reader of a file six levels down
    who holds no rung on the chat is told the title and given no link.

    1.3.0 carries the two names a person reads. ``holder`` and ``machine`` are
    both ids when a box holds the folder — the same id twice, in fact, because
    a box's lease names itself as both — and a status line built out of them
    reads as two uuids. The names are resolved by the route beside the chat's
    title, and a surface that has neither says what the holder IS rather than
    what it is called.

    1.4.0 says whether the holder is serving and how much is still on its way.
    ``served`` is ``live`` while the lease is streaming and its holder has
    beaten recently, and ``offline`` otherwise -- a box that died keeps its
    lease until the TTL runs out, and a reader must not be told it is watching
    a live machine for those minutes. ``landing_count`` is how many files under
    the lease the drive holds an older copy of, or none at all. ``node_id`` is
    the leased folder itself, so a surface listing a folder deep inside the
    lease can match the lease's own frames without knowing which ancestor
    holds it.

    1.5.0 says whether the holder acts for the reader. A refusal that names
    "someone" when the folder is held by the reader's own chat or box sends
    them looking for a colleague who does not exist. ``yours`` is resolved by
    the route beside the names, and reads ``none`` on a row it did not resolve.

    1.6.0 carries the status the server decided for the folder: live, sync
    paused (and why), or a saved copy, with the words to draw it. A reader
    renders it and never judges the lease's raw fields itself.

        Attributes:
            schema_version (str | Unset):
            metadata (LeaseFacetMetadata | Unset):
            holder (str | Unset):  Default: ''.
            machine (str | Unset):  Default: ''.
            purpose (LeaseFacetPurpose | Unset):  Default: LeaseFacetPurpose.MOUNT.
            since (datetime.datetime | None | Unset):
            expires_at (datetime.datetime | None | Unset):
            last_sync_at (datetime.datetime | None | Unset):
            mine (bool | Unset):  Default: False.
            live (bool | Unset):  Default: False.
            inbound (bool | Unset):  Default: False.
            pending (int | Unset):  Default: 0.
            live_seq (int | Unset):  Default: 0.
            chat_id (None | str | Unset):
            chat_title (str | Unset):  Default: ''.
            can_open_chat (bool | Unset):  Default: False.
            machine_name (str | Unset):  Default: ''.
            holder_name (str | Unset):  Default: ''.
            node_id (None | Unset | UUID):
            served (LeaseFacetServed | Unset):  Default: LeaseFacetServed.OFFLINE.
            landing_count (int | Unset):  Default: 0.
            yours (LeaseFacetYours | Unset):  Default: LeaseFacetYours.NONE.
            status (None | StatusFact | Unset):
    """

    schema_version: str | Unset = UNSET
    metadata: LeaseFacetMetadata | Unset = UNSET
    holder: str | Unset = ""
    machine: str | Unset = ""
    purpose: LeaseFacetPurpose | Unset = LeaseFacetPurpose.MOUNT
    since: datetime.datetime | None | Unset = UNSET
    expires_at: datetime.datetime | None | Unset = UNSET
    last_sync_at: datetime.datetime | None | Unset = UNSET
    mine: bool | Unset = False
    live: bool | Unset = False
    inbound: bool | Unset = False
    pending: int | Unset = 0
    live_seq: int | Unset = 0
    chat_id: None | str | Unset = UNSET
    chat_title: str | Unset = ""
    can_open_chat: bool | Unset = False
    machine_name: str | Unset = ""
    holder_name: str | Unset = ""
    node_id: None | Unset | UUID = UNSET
    served: LeaseFacetServed | Unset = LeaseFacetServed.OFFLINE
    landing_count: int | Unset = 0
    yours: LeaseFacetYours | Unset = LeaseFacetYours.NONE
    status: None | StatusFact | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.status_fact import StatusFact

        schema_version = self.schema_version

        metadata: dict[str, Any] | Unset = UNSET
        if not isinstance(self.metadata, Unset):
            metadata = self.metadata.to_dict()

        holder = self.holder

        machine = self.machine

        purpose: str | Unset = UNSET
        if not isinstance(self.purpose, Unset):
            purpose = self.purpose.value

        since: None | str | Unset
        if isinstance(self.since, Unset):
            since = UNSET
        elif isinstance(self.since, datetime.datetime):
            since = self.since.isoformat()
        else:
            since = self.since

        expires_at: None | str | Unset
        if isinstance(self.expires_at, Unset):
            expires_at = UNSET
        elif isinstance(self.expires_at, datetime.datetime):
            expires_at = self.expires_at.isoformat()
        else:
            expires_at = self.expires_at

        last_sync_at: None | str | Unset
        if isinstance(self.last_sync_at, Unset):
            last_sync_at = UNSET
        elif isinstance(self.last_sync_at, datetime.datetime):
            last_sync_at = self.last_sync_at.isoformat()
        else:
            last_sync_at = self.last_sync_at

        mine = self.mine

        live = self.live

        inbound = self.inbound

        pending = self.pending

        live_seq = self.live_seq

        chat_id: None | str | Unset
        if isinstance(self.chat_id, Unset):
            chat_id = UNSET
        else:
            chat_id = self.chat_id

        chat_title = self.chat_title

        can_open_chat = self.can_open_chat

        machine_name = self.machine_name

        holder_name = self.holder_name

        node_id: None | str | Unset
        if isinstance(self.node_id, Unset):
            node_id = UNSET
        elif isinstance(self.node_id, UUID):
            node_id = str(self.node_id)
        else:
            node_id = self.node_id

        served: str | Unset = UNSET
        if not isinstance(self.served, Unset):
            served = self.served.value

        landing_count = self.landing_count

        yours: str | Unset = UNSET
        if not isinstance(self.yours, Unset):
            yours = self.yours.value

        status: dict[str, Any] | None | Unset
        if isinstance(self.status, Unset):
            status = UNSET
        elif isinstance(self.status, StatusFact):
            status = self.status.to_dict()
        else:
            status = self.status

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if schema_version is not UNSET:
            field_dict["schema_version"] = schema_version
        if metadata is not UNSET:
            field_dict["metadata"] = metadata
        if holder is not UNSET:
            field_dict["holder"] = holder
        if machine is not UNSET:
            field_dict["machine"] = machine
        if purpose is not UNSET:
            field_dict["purpose"] = purpose
        if since is not UNSET:
            field_dict["since"] = since
        if expires_at is not UNSET:
            field_dict["expires_at"] = expires_at
        if last_sync_at is not UNSET:
            field_dict["last_sync_at"] = last_sync_at
        if mine is not UNSET:
            field_dict["mine"] = mine
        if live is not UNSET:
            field_dict["live"] = live
        if inbound is not UNSET:
            field_dict["inbound"] = inbound
        if pending is not UNSET:
            field_dict["pending"] = pending
        if live_seq is not UNSET:
            field_dict["live_seq"] = live_seq
        if chat_id is not UNSET:
            field_dict["chat_id"] = chat_id
        if chat_title is not UNSET:
            field_dict["chat_title"] = chat_title
        if can_open_chat is not UNSET:
            field_dict["can_open_chat"] = can_open_chat
        if machine_name is not UNSET:
            field_dict["machine_name"] = machine_name
        if holder_name is not UNSET:
            field_dict["holder_name"] = holder_name
        if node_id is not UNSET:
            field_dict["node_id"] = node_id
        if served is not UNSET:
            field_dict["served"] = served
        if landing_count is not UNSET:
            field_dict["landing_count"] = landing_count
        if yours is not UNSET:
            field_dict["yours"] = yours
        if status is not UNSET:
            field_dict["status"] = status

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.lease_facet_metadata import LeaseFacetMetadata
        from ..models.status_fact import StatusFact

        d = dict(src_dict)
        schema_version = d.pop("schema_version", UNSET)

        _metadata = d.pop("metadata", UNSET)
        metadata: LeaseFacetMetadata | Unset
        if isinstance(_metadata, Unset):
            metadata = UNSET
        else:
            metadata = LeaseFacetMetadata.from_dict(_metadata)

        holder = d.pop("holder", UNSET)

        machine = d.pop("machine", UNSET)

        _purpose = d.pop("purpose", UNSET)
        purpose: LeaseFacetPurpose | Unset
        if isinstance(_purpose, Unset):
            purpose = UNSET
        else:
            purpose = LeaseFacetPurpose(_purpose)

        def _parse_since(data: object) -> datetime.datetime | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                since_type_0 = datetime.datetime.fromisoformat(data)

                return since_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None | Unset, data)

        since = _parse_since(d.pop("since", UNSET))

        def _parse_expires_at(data: object) -> datetime.datetime | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                expires_at_type_0 = datetime.datetime.fromisoformat(data)

                return expires_at_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None | Unset, data)

        expires_at = _parse_expires_at(d.pop("expires_at", UNSET))

        def _parse_last_sync_at(data: object) -> datetime.datetime | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                last_sync_at_type_0 = datetime.datetime.fromisoformat(data)

                return last_sync_at_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None | Unset, data)

        last_sync_at = _parse_last_sync_at(d.pop("last_sync_at", UNSET))

        mine = d.pop("mine", UNSET)

        live = d.pop("live", UNSET)

        inbound = d.pop("inbound", UNSET)

        pending = d.pop("pending", UNSET)

        live_seq = d.pop("live_seq", UNSET)

        def _parse_chat_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        chat_id = _parse_chat_id(d.pop("chat_id", UNSET))

        chat_title = d.pop("chat_title", UNSET)

        can_open_chat = d.pop("can_open_chat", UNSET)

        machine_name = d.pop("machine_name", UNSET)

        holder_name = d.pop("holder_name", UNSET)

        def _parse_node_id(data: object) -> None | Unset | UUID:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                node_id_type_0 = UUID(data)

                return node_id_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | Unset | UUID, data)

        node_id = _parse_node_id(d.pop("node_id", UNSET))

        _served = d.pop("served", UNSET)
        served: LeaseFacetServed | Unset
        if isinstance(_served, Unset):
            served = UNSET
        else:
            served = LeaseFacetServed(_served)

        landing_count = d.pop("landing_count", UNSET)

        _yours = d.pop("yours", UNSET)
        yours: LeaseFacetYours | Unset
        if isinstance(_yours, Unset):
            yours = UNSET
        else:
            yours = LeaseFacetYours(_yours)

        def _parse_status(data: object) -> None | StatusFact | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                status_type_0 = StatusFact.from_dict(data)

                return status_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | StatusFact | Unset, data)

        status = _parse_status(d.pop("status", UNSET))

        lease_facet = cls(
            schema_version=schema_version,
            metadata=metadata,
            holder=holder,
            machine=machine,
            purpose=purpose,
            since=since,
            expires_at=expires_at,
            last_sync_at=last_sync_at,
            mine=mine,
            live=live,
            inbound=inbound,
            pending=pending,
            live_seq=live_seq,
            chat_id=chat_id,
            chat_title=chat_title,
            can_open_chat=can_open_chat,
            machine_name=machine_name,
            holder_name=holder_name,
            node_id=node_id,
            served=served,
            landing_count=landing_count,
            yours=yours,
            status=status,
        )

        lease_facet.additional_properties = d
        return lease_facet

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
