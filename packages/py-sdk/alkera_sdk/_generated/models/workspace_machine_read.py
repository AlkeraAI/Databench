from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.lost_machine_read import LostMachineRead
    from ..models.machine_card import MachineCard
    from ..models.workspace_machine_move_read import WorkspaceMachineMoveRead


T = TypeVar("T", bound="WorkspaceMachineRead")


@_attrs_define
class WorkspaceMachineRead:
    """
    Attributes:
        card (MachineCard): One machine as every surface draws it: an org machine, the shared
            machines, a person's own box, or a box a member registered for the org.
        can_move (bool):
        targets (list[MachineCard]):
        pin (None | str | Unset):
        lost (LostMachineRead | None | Unset):
        active_move (None | Unset | WorkspaceMachineMoveRead):
        last_move (None | Unset | WorkspaceMachineMoveRead):
    """

    card: MachineCard
    can_move: bool
    targets: list[MachineCard]
    pin: None | str | Unset = UNSET
    lost: LostMachineRead | None | Unset = UNSET
    active_move: None | Unset | WorkspaceMachineMoveRead = UNSET
    last_move: None | Unset | WorkspaceMachineMoveRead = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.lost_machine_read import LostMachineRead
        from ..models.workspace_machine_move_read import WorkspaceMachineMoveRead

        card = self.card.to_dict()

        can_move = self.can_move

        targets = []
        for targets_item_data in self.targets:
            targets_item = targets_item_data.to_dict()
            targets.append(targets_item)

        pin: None | str | Unset
        if isinstance(self.pin, Unset):
            pin = UNSET
        else:
            pin = self.pin

        lost: dict[str, Any] | None | Unset
        if isinstance(self.lost, Unset):
            lost = UNSET
        elif isinstance(self.lost, LostMachineRead):
            lost = self.lost.to_dict()
        else:
            lost = self.lost

        active_move: dict[str, Any] | None | Unset
        if isinstance(self.active_move, Unset):
            active_move = UNSET
        elif isinstance(self.active_move, WorkspaceMachineMoveRead):
            active_move = self.active_move.to_dict()
        else:
            active_move = self.active_move

        last_move: dict[str, Any] | None | Unset
        if isinstance(self.last_move, Unset):
            last_move = UNSET
        elif isinstance(self.last_move, WorkspaceMachineMoveRead):
            last_move = self.last_move.to_dict()
        else:
            last_move = self.last_move

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "card": card,
                "can_move": can_move,
                "targets": targets,
            }
        )
        if pin is not UNSET:
            field_dict["pin"] = pin
        if lost is not UNSET:
            field_dict["lost"] = lost
        if active_move is not UNSET:
            field_dict["active_move"] = active_move
        if last_move is not UNSET:
            field_dict["last_move"] = last_move

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.lost_machine_read import LostMachineRead
        from ..models.machine_card import MachineCard
        from ..models.workspace_machine_move_read import WorkspaceMachineMoveRead

        d = dict(src_dict)
        card = MachineCard.from_dict(d.pop("card"))

        can_move = d.pop("can_move")

        targets = []
        _targets = d.pop("targets")
        for targets_item_data in _targets:
            targets_item = MachineCard.from_dict(targets_item_data)

            targets.append(targets_item)

        def _parse_pin(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        pin = _parse_pin(d.pop("pin", UNSET))

        def _parse_lost(data: object) -> LostMachineRead | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                lost_type_0 = LostMachineRead.from_dict(data)

                return lost_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(LostMachineRead | None | Unset, data)

        lost = _parse_lost(d.pop("lost", UNSET))

        def _parse_active_move(data: object) -> None | Unset | WorkspaceMachineMoveRead:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                active_move_type_0 = WorkspaceMachineMoveRead.from_dict(data)

                return active_move_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | Unset | WorkspaceMachineMoveRead, data)

        active_move = _parse_active_move(d.pop("active_move", UNSET))

        def _parse_last_move(data: object) -> None | Unset | WorkspaceMachineMoveRead:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                last_move_type_0 = WorkspaceMachineMoveRead.from_dict(data)

                return last_move_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | Unset | WorkspaceMachineMoveRead, data)

        last_move = _parse_last_move(d.pop("last_move", UNSET))

        workspace_machine_read = cls(
            card=card,
            can_move=can_move,
            targets=targets,
            pin=pin,
            lost=lost,
            active_move=active_move,
            last_move=last_move,
        )

        workspace_machine_read.additional_properties = d
        return workspace_machine_read

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
