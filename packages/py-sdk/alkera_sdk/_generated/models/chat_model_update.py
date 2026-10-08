from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="ChatModelUpdate")


@_attrs_define
class ChatModelUpdate:
    """The model a reader moves an open chat onto.

    The same two fields a create takes, and resolved against the same catalog by
    the same rule: an id the workspace cannot run a chat on is a 422, never a
    quiet no-op. Naming no model is not spellable here — a create may decline to
    pick one (the box falls back to its own default), but a SWITCH that pins
    nothing would leave the chat on the model it was already on while telling
    the reader it had moved.

        Attributes:
            model (str):
            effort (None | str | Unset):
            expected_model_id (None | str | Unset):
    """

    model: str
    effort: None | str | Unset = UNSET
    expected_model_id: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        model = self.model

        effort: None | str | Unset
        if isinstance(self.effort, Unset):
            effort = UNSET
        else:
            effort = self.effort

        expected_model_id: None | str | Unset
        if isinstance(self.expected_model_id, Unset):
            expected_model_id = UNSET
        else:
            expected_model_id = self.expected_model_id

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "model": model,
            }
        )
        if effort is not UNSET:
            field_dict["effort"] = effort
        if expected_model_id is not UNSET:
            field_dict["expected_model_id"] = expected_model_id

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        model = d.pop("model")

        def _parse_effort(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        effort = _parse_effort(d.pop("effort", UNSET))

        def _parse_expected_model_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        expected_model_id = _parse_expected_model_id(d.pop("expected_model_id", UNSET))

        chat_model_update = cls(
            model=model,
            effort=effort,
            expected_model_id=expected_model_id,
        )

        chat_model_update.additional_properties = d
        return chat_model_update

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
