from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.actor_ref_kind import ActorRefKind
from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.acting_for_ref import ActingForRef


T = TypeVar("T", bound="ActorRef")


@_attrs_define
class ActorRef:
    """Who did something, named: a person by their name, an agent as
    "<the brand's agent name> for <the person's name>" with that person in
    ``acting_for``, the platform itself by the product's name.

        Attributes:
            kind (ActorRefKind):
            id (str):
            display_name (str):
            acting_for (ActingForRef | None | Unset):
    """

    kind: ActorRefKind
    id: str
    display_name: str
    acting_for: ActingForRef | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.acting_for_ref import ActingForRef

        kind = self.kind.value

        id = self.id

        display_name = self.display_name

        acting_for: dict[str, Any] | None | Unset
        if isinstance(self.acting_for, Unset):
            acting_for = UNSET
        elif isinstance(self.acting_for, ActingForRef):
            acting_for = self.acting_for.to_dict()
        else:
            acting_for = self.acting_for

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "kind": kind,
                "id": id,
                "display_name": display_name,
            }
        )
        if acting_for is not UNSET:
            field_dict["acting_for"] = acting_for

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.acting_for_ref import ActingForRef

        d = dict(src_dict)
        kind = ActorRefKind(d.pop("kind"))

        id = d.pop("id")

        display_name = d.pop("display_name")

        def _parse_acting_for(data: object) -> ActingForRef | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                acting_for_type_0 = ActingForRef.from_dict(data)

                return acting_for_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(ActingForRef | None | Unset, data)

        acting_for = _parse_acting_for(d.pop("acting_for", UNSET))

        actor_ref = cls(
            kind=kind,
            id=id,
            display_name=display_name,
            acting_for=acting_for,
        )

        actor_ref.additional_properties = d
        return actor_ref

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
