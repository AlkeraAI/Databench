from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.chat_model_option_reason_code_type_0 import ChatModelOptionReasonCodeType0
from ..models.chat_model_option_state import ChatModelOptionState
from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.chat_model_read import ChatModelRead


T = TypeVar("T", bound="ChatModelOption")


@_attrs_define
class ChatModelOption:
    """One model in an open chat's picker, with whether the chat may move to it.

    Attributes:
        model (ChatModelRead): One model the gateway can serve this caller, as the picker reads it.

            The fields are the gateway catalog's own. ``efforts`` is the set of
            reasoning-effort variants the model offers (empty = no variant choice), and
            ``default_effort`` is the one the catalog prefers.
        state (ChatModelOptionState):
        reason_code (ChatModelOptionReasonCodeType0 | None | Unset):
        message (None | str | Unset):
        group_message (None | str | Unset):
        escape_new_chat_model (None | str | Unset):
    """

    model: ChatModelRead
    state: ChatModelOptionState
    reason_code: ChatModelOptionReasonCodeType0 | None | Unset = UNSET
    message: None | str | Unset = UNSET
    group_message: None | str | Unset = UNSET
    escape_new_chat_model: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        model = self.model.to_dict()

        state = self.state.value

        reason_code: None | str | Unset
        if isinstance(self.reason_code, Unset):
            reason_code = UNSET
        elif isinstance(self.reason_code, ChatModelOptionReasonCodeType0):
            reason_code = self.reason_code.value
        else:
            reason_code = self.reason_code

        message: None | str | Unset
        if isinstance(self.message, Unset):
            message = UNSET
        else:
            message = self.message

        group_message: None | str | Unset
        if isinstance(self.group_message, Unset):
            group_message = UNSET
        else:
            group_message = self.group_message

        escape_new_chat_model: None | str | Unset
        if isinstance(self.escape_new_chat_model, Unset):
            escape_new_chat_model = UNSET
        else:
            escape_new_chat_model = self.escape_new_chat_model

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "model": model,
                "state": state,
            }
        )
        if reason_code is not UNSET:
            field_dict["reason_code"] = reason_code
        if message is not UNSET:
            field_dict["message"] = message
        if group_message is not UNSET:
            field_dict["group_message"] = group_message
        if escape_new_chat_model is not UNSET:
            field_dict["escape_new_chat_model"] = escape_new_chat_model

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.chat_model_read import ChatModelRead

        d = dict(src_dict)
        model = ChatModelRead.from_dict(d.pop("model"))

        state = ChatModelOptionState(d.pop("state"))

        def _parse_reason_code(data: object) -> ChatModelOptionReasonCodeType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                reason_code_type_0 = ChatModelOptionReasonCodeType0(data)

                return reason_code_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(ChatModelOptionReasonCodeType0 | None | Unset, data)

        reason_code = _parse_reason_code(d.pop("reason_code", UNSET))

        def _parse_message(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        message = _parse_message(d.pop("message", UNSET))

        def _parse_group_message(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        group_message = _parse_group_message(d.pop("group_message", UNSET))

        def _parse_escape_new_chat_model(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        escape_new_chat_model = _parse_escape_new_chat_model(d.pop("escape_new_chat_model", UNSET))

        chat_model_option = cls(
            model=model,
            state=state,
            reason_code=reason_code,
            message=message,
            group_message=group_message,
            escape_new_chat_model=escape_new_chat_model,
        )

        chat_model_option.additional_properties = d
        return chat_model_option

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
