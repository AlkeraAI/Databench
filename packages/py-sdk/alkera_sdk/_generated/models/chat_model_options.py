from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.chat_model_options_applies import ChatModelOptionsApplies
from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.chat_model_current import ChatModelCurrent
    from ..models.chat_model_option import ChatModelOption


T = TypeVar("T", bound="ChatModelOptions")


@_attrs_define
class ChatModelOptions:
    """What an open chat's model picker offers this caller.

    Attributes:
        current (ChatModelCurrent):
        can_switch (bool):
        applies (ChatModelOptionsApplies | Unset):  Default: ChatModelOptionsApplies.NEXT_TURN.
        options (list[ChatModelOption] | Unset):
        billed_to_owner (bool | Unset):  Default: False.
    """

    current: ChatModelCurrent
    can_switch: bool
    applies: ChatModelOptionsApplies | Unset = ChatModelOptionsApplies.NEXT_TURN
    options: list[ChatModelOption] | Unset = UNSET
    billed_to_owner: bool | Unset = False
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        current = self.current.to_dict()

        can_switch = self.can_switch

        applies: str | Unset = UNSET
        if not isinstance(self.applies, Unset):
            applies = self.applies.value

        options: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.options, Unset):
            options = []
            for options_item_data in self.options:
                options_item = options_item_data.to_dict()
                options.append(options_item)

        billed_to_owner = self.billed_to_owner

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "current": current,
                "can_switch": can_switch,
            }
        )
        if applies is not UNSET:
            field_dict["applies"] = applies
        if options is not UNSET:
            field_dict["options"] = options
        if billed_to_owner is not UNSET:
            field_dict["billed_to_owner"] = billed_to_owner

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.chat_model_current import ChatModelCurrent
        from ..models.chat_model_option import ChatModelOption

        d = dict(src_dict)
        current = ChatModelCurrent.from_dict(d.pop("current"))

        can_switch = d.pop("can_switch")

        _applies = d.pop("applies", UNSET)
        applies: ChatModelOptionsApplies | Unset
        if isinstance(_applies, Unset):
            applies = UNSET
        else:
            applies = ChatModelOptionsApplies(_applies)

        _options = d.pop("options", UNSET)
        options: list[ChatModelOption] | Unset = UNSET
        if _options is not UNSET:
            options = []
            for options_item_data in _options:
                options_item = ChatModelOption.from_dict(options_item_data)

                options.append(options_item)

        billed_to_owner = d.pop("billed_to_owner", UNSET)

        chat_model_options = cls(
            current=current,
            can_switch=can_switch,
            applies=applies,
            options=options,
            billed_to_owner=billed_to_owner,
        )

        chat_model_options.additional_properties = d
        return chat_model_options

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
