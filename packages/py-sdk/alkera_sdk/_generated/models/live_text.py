from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="LiveText")


@_attrs_define
class LiveText:
    """The document as a text peer reads it.

    Attributes:
        live (bool):
        token (None | str | Unset):
        text (None | str | Unset):
        at_token (None | str | Unset):
        at_text (None | str | Unset):
        repeat (bool | Unset):  Default: False.
        saved (bool | Unset):  Default: True.
        reason (None | str | Unset):
    """

    live: bool
    token: None | str | Unset = UNSET
    text: None | str | Unset = UNSET
    at_token: None | str | Unset = UNSET
    at_text: None | str | Unset = UNSET
    repeat: bool | Unset = False
    saved: bool | Unset = True
    reason: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        live = self.live

        token: None | str | Unset
        if isinstance(self.token, Unset):
            token = UNSET
        else:
            token = self.token

        text: None | str | Unset
        if isinstance(self.text, Unset):
            text = UNSET
        else:
            text = self.text

        at_token: None | str | Unset
        if isinstance(self.at_token, Unset):
            at_token = UNSET
        else:
            at_token = self.at_token

        at_text: None | str | Unset
        if isinstance(self.at_text, Unset):
            at_text = UNSET
        else:
            at_text = self.at_text

        repeat = self.repeat

        saved = self.saved

        reason: None | str | Unset
        if isinstance(self.reason, Unset):
            reason = UNSET
        else:
            reason = self.reason

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "live": live,
            }
        )
        if token is not UNSET:
            field_dict["token"] = token
        if text is not UNSET:
            field_dict["text"] = text
        if at_token is not UNSET:
            field_dict["atToken"] = at_token
        if at_text is not UNSET:
            field_dict["atText"] = at_text
        if repeat is not UNSET:
            field_dict["repeat"] = repeat
        if saved is not UNSET:
            field_dict["saved"] = saved
        if reason is not UNSET:
            field_dict["reason"] = reason

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        live = d.pop("live")

        def _parse_token(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        token = _parse_token(d.pop("token", UNSET))

        def _parse_text(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        text = _parse_text(d.pop("text", UNSET))

        def _parse_at_token(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        at_token = _parse_at_token(d.pop("atToken", UNSET))

        def _parse_at_text(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        at_text = _parse_at_text(d.pop("atText", UNSET))

        repeat = d.pop("repeat", UNSET)

        saved = d.pop("saved", UNSET)

        def _parse_reason(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        reason = _parse_reason(d.pop("reason", UNSET))

        live_text = cls(
            live=live,
            token=token,
            text=text,
            at_token=at_token,
            at_text=at_text,
            repeat=repeat,
            saved=saved,
            reason=reason,
        )

        live_text.additional_properties = d
        return live_text

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
