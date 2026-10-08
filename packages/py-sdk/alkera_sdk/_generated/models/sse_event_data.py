from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast
from uuid import UUID

from attrs import define as _attrs_define

from ..models.realtime_event_type import RealtimeEventType
from ..types import UNSET, Unset

T = TypeVar("T", bound="SseEventData")


@_attrs_define
class SseEventData:
    """The body of one ``event: <type>`` frame; ``id:`` on the frame is the
    outbox row id the client resumes from.

    The optional fields below are the only things a frame carries beyond
    the entity it names, and every one of them is an ID the reader could have
    asked for anyway. They exist so a client can narrow WHICH cached read it
    re-takes: a folder mounted live has to know that this node sits in the
    folder it is showing, and a file open in a tab has to know its own node
    moved, or every frame costs a refetch of everything. They stay optional
    because plenty of writers genuinely do not hold them — a client handed
    ``None`` refreshes wider, which is slower and never wrong.

        Attributes:
            type_ (RealtimeEventType): The events a client may receive over the portal's event stream.

                Every :class:`EventType` except those that exist for the server's own
                use: ``doc.op`` (a doc-sync envelope, delivered over the socket gateway to
                a channel subscriber and never as a bare invalidation), ``notebook.event``
                (a notebook kernel's events, delivered the same way) and
                ``authz.decision`` (an audit record). This enum reaches the OpenAPI schema
                through the stream's response model, so a client-side handler map can be
                checked exhaustive against it; a test pins it equal to the registry minus
                those two.
            entity (str):
            entity_id (str):
            org_id (UUID):
            version (int | Unset):  Default: 0.
            drive_id (None | Unset | UUID):
            parent_id (None | Unset | UUID):
            reason (None | str | Unset):
            lease_node_id (None | Unset | UUID):
            subtree (bool | None | Unset):
    """

    type_: RealtimeEventType
    entity: str
    entity_id: str
    org_id: UUID
    version: int | Unset = 0
    drive_id: None | Unset | UUID = UNSET
    parent_id: None | Unset | UUID = UNSET
    reason: None | str | Unset = UNSET
    lease_node_id: None | Unset | UUID = UNSET
    subtree: bool | None | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        type_ = self.type_.value

        entity = self.entity

        entity_id = self.entity_id

        org_id = str(self.org_id)

        version = self.version

        drive_id: None | str | Unset
        if isinstance(self.drive_id, Unset):
            drive_id = UNSET
        elif isinstance(self.drive_id, UUID):
            drive_id = str(self.drive_id)
        else:
            drive_id = self.drive_id

        parent_id: None | str | Unset
        if isinstance(self.parent_id, Unset):
            parent_id = UNSET
        elif isinstance(self.parent_id, UUID):
            parent_id = str(self.parent_id)
        else:
            parent_id = self.parent_id

        reason: None | str | Unset
        if isinstance(self.reason, Unset):
            reason = UNSET
        else:
            reason = self.reason

        lease_node_id: None | str | Unset
        if isinstance(self.lease_node_id, Unset):
            lease_node_id = UNSET
        elif isinstance(self.lease_node_id, UUID):
            lease_node_id = str(self.lease_node_id)
        else:
            lease_node_id = self.lease_node_id

        subtree: bool | None | Unset
        if isinstance(self.subtree, Unset):
            subtree = UNSET
        else:
            subtree = self.subtree

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "type": type_,
                "entity": entity,
                "entity_id": entity_id,
                "org_id": org_id,
            }
        )
        if version is not UNSET:
            field_dict["version"] = version
        if drive_id is not UNSET:
            field_dict["drive_id"] = drive_id
        if parent_id is not UNSET:
            field_dict["parent_id"] = parent_id
        if reason is not UNSET:
            field_dict["reason"] = reason
        if lease_node_id is not UNSET:
            field_dict["lease_node_id"] = lease_node_id
        if subtree is not UNSET:
            field_dict["subtree"] = subtree

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        type_ = RealtimeEventType(d.pop("type"))

        entity = d.pop("entity")

        entity_id = d.pop("entity_id")

        org_id = UUID(d.pop("org_id"))

        version = d.pop("version", UNSET)

        def _parse_drive_id(data: object) -> None | Unset | UUID:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                drive_id_type_0 = UUID(data)

                return drive_id_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | Unset | UUID, data)

        drive_id = _parse_drive_id(d.pop("drive_id", UNSET))

        def _parse_parent_id(data: object) -> None | Unset | UUID:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                parent_id_type_0 = UUID(data)

                return parent_id_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | Unset | UUID, data)

        parent_id = _parse_parent_id(d.pop("parent_id", UNSET))

        def _parse_reason(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        reason = _parse_reason(d.pop("reason", UNSET))

        def _parse_lease_node_id(data: object) -> None | Unset | UUID:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                lease_node_id_type_0 = UUID(data)

                return lease_node_id_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | Unset | UUID, data)

        lease_node_id = _parse_lease_node_id(d.pop("lease_node_id", UNSET))

        def _parse_subtree(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        subtree = _parse_subtree(d.pop("subtree", UNSET))

        sse_event_data = cls(
            type_=type_,
            entity=entity,
            entity_id=entity_id,
            org_id=org_id,
            version=version,
            drive_id=drive_id,
            parent_id=parent_id,
            reason=reason,
            lease_node_id=lease_node_id,
            subtree=subtree,
        )

        return sse_event_data
