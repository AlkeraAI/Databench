from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.activity_item_kind import ActivityItemKind
from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.actor_ref import ActorRef


T = TypeVar("T", bound="ActivityItem")


@_attrs_define
class ActivityItem:
    """
    Attributes:
        at (datetime.datetime):
        actor (ActorRef): Who did something, named: a person by their name, an agent as
            "<the brand's agent name> for <the person's name>" with that person in
            ``acting_for``, the platform itself by the product's name.
        kind (ActivityItemKind):
        cell_ids (list[str] | Unset):
        run_id (None | str | Unset):
        status (None | str | Unset):
    """

    at: datetime.datetime
    actor: ActorRef
    kind: ActivityItemKind
    cell_ids: list[str] | Unset = UNSET
    run_id: None | str | Unset = UNSET
    status: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        at = self.at.isoformat()

        actor = self.actor.to_dict()

        kind = self.kind.value

        cell_ids: list[str] | Unset = UNSET
        if not isinstance(self.cell_ids, Unset):
            cell_ids = self.cell_ids

        run_id: None | str | Unset
        if isinstance(self.run_id, Unset):
            run_id = UNSET
        else:
            run_id = self.run_id

        status: None | str | Unset
        if isinstance(self.status, Unset):
            status = UNSET
        else:
            status = self.status

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "at": at,
                "actor": actor,
                "kind": kind,
            }
        )
        if cell_ids is not UNSET:
            field_dict["cell_ids"] = cell_ids
        if run_id is not UNSET:
            field_dict["run_id"] = run_id
        if status is not UNSET:
            field_dict["status"] = status

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.actor_ref import ActorRef

        d = dict(src_dict)
        at = datetime.datetime.fromisoformat(d.pop("at"))

        actor = ActorRef.from_dict(d.pop("actor"))

        kind = ActivityItemKind(d.pop("kind"))

        cell_ids = cast(list[str], d.pop("cell_ids", UNSET))

        def _parse_run_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        run_id = _parse_run_id(d.pop("run_id", UNSET))

        def _parse_status(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        status = _parse_status(d.pop("status", UNSET))

        activity_item = cls(
            at=at,
            actor=actor,
            kind=kind,
            cell_ids=cell_ids,
            run_id=run_id,
            status=status,
        )

        activity_item.additional_properties = d
        return activity_item

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
