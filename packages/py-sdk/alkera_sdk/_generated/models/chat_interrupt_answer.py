from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="ChatInterruptAnswer")


@_attrs_define
class ChatInterruptAnswer:
    """An answer to an ask the agent is blocked on.

    Not a message: an ask is answered, not said, so this makes no transcript
    entry — the machine records the resolution as the harness settles it. The
    shape is the relay the daemon validates: it names the outstanding ask by id
    and carries exactly one answer, which is either a permission option the ask
    itself offered, a set of answers to a question's prompts, or a rejection.

    The server does not judge whether the answer is allowed to have that effect;
    the machine does (it refuses an option the ask never offered, an id that is
    not outstanding, and any approval of a write on a read-only session). What
    the shape enforces is that exactly one answer arrives, so an ambiguous relay
    never reaches the machine to be resolved by field order.

        Attributes:
            interrupt_id (str):
            option_id (None | str | Unset):
            answers (list[list[str]] | None | Unset):
            reject (bool | Unset):  Default: False.
            reason (None | str | Unset):
            note (None | str | Unset):
    """

    interrupt_id: str
    option_id: None | str | Unset = UNSET
    answers: list[list[str]] | None | Unset = UNSET
    reject: bool | Unset = False
    reason: None | str | Unset = UNSET
    note: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        interrupt_id = self.interrupt_id

        option_id: None | str | Unset
        if isinstance(self.option_id, Unset):
            option_id = UNSET
        else:
            option_id = self.option_id

        answers: list[list[str]] | None | Unset
        if isinstance(self.answers, Unset):
            answers = UNSET
        elif isinstance(self.answers, list):
            answers = []
            for answers_type_0_item_data in self.answers:
                answers_type_0_item = answers_type_0_item_data

                answers.append(answers_type_0_item)

        else:
            answers = self.answers

        reject = self.reject

        reason: None | str | Unset
        if isinstance(self.reason, Unset):
            reason = UNSET
        else:
            reason = self.reason

        note: None | str | Unset
        if isinstance(self.note, Unset):
            note = UNSET
        else:
            note = self.note

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "interrupt_id": interrupt_id,
            }
        )
        if option_id is not UNSET:
            field_dict["option_id"] = option_id
        if answers is not UNSET:
            field_dict["answers"] = answers
        if reject is not UNSET:
            field_dict["reject"] = reject
        if reason is not UNSET:
            field_dict["reason"] = reason
        if note is not UNSET:
            field_dict["note"] = note

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        interrupt_id = d.pop("interrupt_id")

        def _parse_option_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        option_id = _parse_option_id(d.pop("option_id", UNSET))

        def _parse_answers(data: object) -> list[list[str]] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                answers_type_0 = []
                _answers_type_0 = data
                for answers_type_0_item_data in _answers_type_0:
                    answers_type_0_item = cast(list[str], answers_type_0_item_data)

                    answers_type_0.append(answers_type_0_item)

                return answers_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[list[str]] | None | Unset, data)

        answers = _parse_answers(d.pop("answers", UNSET))

        reject = d.pop("reject", UNSET)

        def _parse_reason(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        reason = _parse_reason(d.pop("reason", UNSET))

        def _parse_note(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        note = _parse_note(d.pop("note", UNSET))

        chat_interrupt_answer = cls(
            interrupt_id=interrupt_id,
            option_id=option_id,
            answers=answers,
            reject=reject,
            reason=reason,
            note=note,
        )

        chat_interrupt_answer.additional_properties = d
        return chat_interrupt_answer

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
