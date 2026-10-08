from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.run_actor_kind import RunActorKind
from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.acting_for import ActingFor


T = TypeVar("T", bound="RunActor")


@_attrs_define
class RunActor:
    """Who asked for a run: the person or agent whose action caused it (for
    a widget-triggered or autorun run too), ``system`` only for what nobody
    asked for.

        Attributes:
            kind (RunActorKind):
            id (str):
            display_name (str):
            acting_for (ActingFor | None | Unset):
    """

    kind: RunActorKind
    id: str
    display_name: str
    acting_for: ActingFor | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.acting_for import ActingFor

        kind = self.kind.value

        id = self.id

        display_name = self.display_name

        acting_for: dict[str, Any] | None | Unset
        if isinstance(self.acting_for, Unset):
            acting_for = UNSET
        elif isinstance(self.acting_for, ActingFor):
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
        from ..models.acting_for import ActingFor

        d = dict(src_dict)
        kind = RunActorKind(d.pop("kind"))

        id = d.pop("id")

        display_name = d.pop("display_name")

        def _parse_acting_for(data: object) -> ActingFor | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                acting_for_type_0 = ActingFor.from_dict(data)

                return acting_for_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(ActingFor | None | Unset, data)

        acting_for = _parse_acting_for(d.pop("acting_for", UNSET))

        run_actor = cls(
            kind=kind,
            id=id,
            display_name=display_name,
            acting_for=acting_for,
        )

        run_actor.additional_properties = d
        return run_actor

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
