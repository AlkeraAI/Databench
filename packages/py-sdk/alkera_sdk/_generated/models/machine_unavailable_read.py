from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

if TYPE_CHECKING:
    from ..models.lost_machine_read import LostMachineRead
    from ..models.machine_card import MachineCard


T = TypeVar("T", bound="MachineUnavailableRead")


@_attrs_define
class MachineUnavailableRead:
    """A wake held because the workspace's machine is gone: what was lost, and
    where the opener may wake it instead. ``choices`` are the targets the
    opener may move the workspace to (empty when they may not move it), the
    default placement first when it serves the org; ``preselect_default`` is
    whether the opener is offered it as the answer.

        Attributes:
            workspace_id (str):
            lost (LostMachineRead): The org machine a workspace ran on that can no longer serve it: deleted,
                or out of reach of the person whose chat would run there. ``fell_back``:
                a waker that is not a person already moved the workspace to the default
                placement, which the workspace's card now shows.
            choices (list[MachineCard]):
            preselect_default (bool):
    """

    workspace_id: str
    lost: LostMachineRead
    choices: list[MachineCard]
    preselect_default: bool
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        workspace_id = self.workspace_id

        lost = self.lost.to_dict()

        choices = []
        for choices_item_data in self.choices:
            choices_item = choices_item_data.to_dict()
            choices.append(choices_item)

        preselect_default = self.preselect_default

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "workspace_id": workspace_id,
                "lost": lost,
                "choices": choices,
                "preselect_default": preselect_default,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.lost_machine_read import LostMachineRead
        from ..models.machine_card import MachineCard

        d = dict(src_dict)
        workspace_id = d.pop("workspace_id")

        lost = LostMachineRead.from_dict(d.pop("lost"))

        choices = []
        _choices = d.pop("choices")
        for choices_item_data in _choices:
            choices_item = MachineCard.from_dict(choices_item_data)

            choices.append(choices_item)

        preselect_default = d.pop("preselect_default")

        machine_unavailable_read = cls(
            workspace_id=workspace_id,
            lost=lost,
            choices=choices,
            preselect_default=preselect_default,
        )

        machine_unavailable_read.additional_properties = d
        return machine_unavailable_read

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
