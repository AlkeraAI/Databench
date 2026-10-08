from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast
from uuid import UUID

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.conflict_entry_arrived_from_type_0 import ConflictEntryArrivedFromType0
from ..types import UNSET, Unset

T = TypeVar("T", bound="ConflictEntry")


@_attrs_define
class ConflictEntry:
    """One divergence, by id. File names never appear here: errors and
    listings carry ids, never names.

    ``state`` is ``open`` for a divergence a sync recorded and ``auto`` for one
    the drive settled itself: the last write to arrive kept the name (``who``
    wrote it, from the side ``arrivedFrom`` names) and the displaced bytes went
    to ``copyNodeId`` — or, when that is null, stayed a version of the node.

        Attributes:
            id (UUID):
            node_id (UUID):
            base_version_id (None | UUID):
            theirs_version_id (UUID):
            mine_version_id (UUID):
            state (str):
            copy_node_id (None | Unset | UUID):
            arrived_from (ConflictEntryArrivedFromType0 | None | Unset):
            who (None | str | Unset):
            displaced_by (None | str | Unset):
            resolved_by (None | Unset | UUID):
    """

    id: UUID
    node_id: UUID
    base_version_id: None | UUID
    theirs_version_id: UUID
    mine_version_id: UUID
    state: str
    copy_node_id: None | Unset | UUID = UNSET
    arrived_from: ConflictEntryArrivedFromType0 | None | Unset = UNSET
    who: None | str | Unset = UNSET
    displaced_by: None | str | Unset = UNSET
    resolved_by: None | Unset | UUID = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        id = str(self.id)

        node_id = str(self.node_id)

        base_version_id: None | str
        if isinstance(self.base_version_id, UUID):
            base_version_id = str(self.base_version_id)
        else:
            base_version_id = self.base_version_id

        theirs_version_id = str(self.theirs_version_id)

        mine_version_id = str(self.mine_version_id)

        state = self.state

        copy_node_id: None | str | Unset
        if isinstance(self.copy_node_id, Unset):
            copy_node_id = UNSET
        elif isinstance(self.copy_node_id, UUID):
            copy_node_id = str(self.copy_node_id)
        else:
            copy_node_id = self.copy_node_id

        arrived_from: None | str | Unset
        if isinstance(self.arrived_from, Unset):
            arrived_from = UNSET
        elif isinstance(self.arrived_from, ConflictEntryArrivedFromType0):
            arrived_from = self.arrived_from.value
        else:
            arrived_from = self.arrived_from

        who: None | str | Unset
        if isinstance(self.who, Unset):
            who = UNSET
        else:
            who = self.who

        displaced_by: None | str | Unset
        if isinstance(self.displaced_by, Unset):
            displaced_by = UNSET
        else:
            displaced_by = self.displaced_by

        resolved_by: None | str | Unset
        if isinstance(self.resolved_by, Unset):
            resolved_by = UNSET
        elif isinstance(self.resolved_by, UUID):
            resolved_by = str(self.resolved_by)
        else:
            resolved_by = self.resolved_by

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "nodeId": node_id,
                "baseVersionId": base_version_id,
                "theirsVersionId": theirs_version_id,
                "mineVersionId": mine_version_id,
                "state": state,
            }
        )
        if copy_node_id is not UNSET:
            field_dict["copyNodeId"] = copy_node_id
        if arrived_from is not UNSET:
            field_dict["arrivedFrom"] = arrived_from
        if who is not UNSET:
            field_dict["who"] = who
        if displaced_by is not UNSET:
            field_dict["displacedBy"] = displaced_by
        if resolved_by is not UNSET:
            field_dict["resolvedBy"] = resolved_by

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        id = UUID(d.pop("id"))

        node_id = UUID(d.pop("nodeId"))

        def _parse_base_version_id(data: object) -> None | UUID:
            if data is None:
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                base_version_id_type_0 = UUID(data)

                return base_version_id_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | UUID, data)

        base_version_id = _parse_base_version_id(d.pop("baseVersionId"))

        theirs_version_id = UUID(d.pop("theirsVersionId"))

        mine_version_id = UUID(d.pop("mineVersionId"))

        state = d.pop("state")

        def _parse_copy_node_id(data: object) -> None | Unset | UUID:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                copy_node_id_type_0 = UUID(data)

                return copy_node_id_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | Unset | UUID, data)

        copy_node_id = _parse_copy_node_id(d.pop("copyNodeId", UNSET))

        def _parse_arrived_from(data: object) -> ConflictEntryArrivedFromType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                arrived_from_type_0 = ConflictEntryArrivedFromType0(data)

                return arrived_from_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(ConflictEntryArrivedFromType0 | None | Unset, data)

        arrived_from = _parse_arrived_from(d.pop("arrivedFrom", UNSET))

        def _parse_who(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        who = _parse_who(d.pop("who", UNSET))

        def _parse_displaced_by(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        displaced_by = _parse_displaced_by(d.pop("displacedBy", UNSET))

        def _parse_resolved_by(data: object) -> None | Unset | UUID:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                resolved_by_type_0 = UUID(data)

                return resolved_by_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | Unset | UUID, data)

        resolved_by = _parse_resolved_by(d.pop("resolvedBy", UNSET))

        conflict_entry = cls(
            id=id,
            node_id=node_id,
            base_version_id=base_version_id,
            theirs_version_id=theirs_version_id,
            mine_version_id=mine_version_id,
            state=state,
            copy_node_id=copy_node_id,
            arrived_from=arrived_from,
            who=who,
            displaced_by=displaced_by,
            resolved_by=resolved_by,
        )

        conflict_entry.additional_properties = d
        return conflict_entry

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
