from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.box_machine_card import BoxMachineCard


T = TypeVar("T", bound="MachineHeartbeatResponse")


@_attrs_define
class MachineHeartbeatResponse:
    """The answer to a heartbeat that carries a body: the machine card, or
    ``None`` for a box that backs no org machine.

        Attributes:
            card (BoxMachineCard | None | Unset):
    """

    card: BoxMachineCard | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.box_machine_card import BoxMachineCard

        card: dict[str, Any] | None | Unset
        if isinstance(self.card, Unset):
            card = UNSET
        elif isinstance(self.card, BoxMachineCard):
            card = self.card.to_dict()
        else:
            card = self.card

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if card is not UNSET:
            field_dict["card"] = card

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.box_machine_card import BoxMachineCard

        d = dict(src_dict)

        def _parse_card(data: object) -> BoxMachineCard | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                card_type_0 = BoxMachineCard.from_dict(data)

                return card_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(BoxMachineCard | None | Unset, data)

        card = _parse_card(d.pop("card", UNSET))

        machine_heartbeat_response = cls(
            card=card,
        )

        machine_heartbeat_response.additional_properties = d
        return machine_heartbeat_response

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
