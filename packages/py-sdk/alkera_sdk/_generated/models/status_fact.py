from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.status_fact_tone import StatusFactTone
from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.status_action import StatusAction


T = TypeVar("T", bound="StatusFact")


@_attrs_define
class StatusFact:
    """Where one thing stands, as every surface draws it.

    Attributes:
        subject (str):
        state (str):
        label (str):
        tone (StatusFactTone):
        sentence (str):
        reason_code (str | Unset):  Default: ''.
        since (datetime.datetime | None | Unset):
        recheck_at (datetime.datetime | None | Unset):
        action (None | StatusAction | Unset):
    """

    subject: str
    state: str
    label: str
    tone: StatusFactTone
    sentence: str
    reason_code: str | Unset = ""
    since: datetime.datetime | None | Unset = UNSET
    recheck_at: datetime.datetime | None | Unset = UNSET
    action: None | StatusAction | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.status_action import StatusAction

        subject = self.subject

        state = self.state

        label = self.label

        tone = self.tone.value

        sentence = self.sentence

        reason_code = self.reason_code

        since: None | str | Unset
        if isinstance(self.since, Unset):
            since = UNSET
        elif isinstance(self.since, datetime.datetime):
            since = self.since.isoformat()
        else:
            since = self.since

        recheck_at: None | str | Unset
        if isinstance(self.recheck_at, Unset):
            recheck_at = UNSET
        elif isinstance(self.recheck_at, datetime.datetime):
            recheck_at = self.recheck_at.isoformat()
        else:
            recheck_at = self.recheck_at

        action: dict[str, Any] | None | Unset
        if isinstance(self.action, Unset):
            action = UNSET
        elif isinstance(self.action, StatusAction):
            action = self.action.to_dict()
        else:
            action = self.action

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "subject": subject,
                "state": state,
                "label": label,
                "tone": tone,
                "sentence": sentence,
            }
        )
        if reason_code is not UNSET:
            field_dict["reason_code"] = reason_code
        if since is not UNSET:
            field_dict["since"] = since
        if recheck_at is not UNSET:
            field_dict["recheck_at"] = recheck_at
        if action is not UNSET:
            field_dict["action"] = action

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.status_action import StatusAction

        d = dict(src_dict)
        subject = d.pop("subject")

        state = d.pop("state")

        label = d.pop("label")

        tone = StatusFactTone(d.pop("tone"))

        sentence = d.pop("sentence")

        reason_code = d.pop("reason_code", UNSET)

        def _parse_since(data: object) -> datetime.datetime | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                since_type_0 = datetime.datetime.fromisoformat(data)

                return since_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None | Unset, data)

        since = _parse_since(d.pop("since", UNSET))

        def _parse_recheck_at(data: object) -> datetime.datetime | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                recheck_at_type_0 = datetime.datetime.fromisoformat(data)

                return recheck_at_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None | Unset, data)

        recheck_at = _parse_recheck_at(d.pop("recheck_at", UNSET))

        def _parse_action(data: object) -> None | StatusAction | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                action_type_0 = StatusAction.from_dict(data)

                return action_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | StatusAction | Unset, data)

        action = _parse_action(d.pop("action", UNSET))

        status_fact = cls(
            subject=subject,
            state=state,
            label=label,
            tone=tone,
            sentence=sentence,
            reason_code=reason_code,
            since=since,
            recheck_at=recheck_at,
            action=action,
        )

        status_fact.additional_properties = d
        return status_fact

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
