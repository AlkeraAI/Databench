from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="LiveTextSubmit")


@_attrs_define
class LiveTextSubmit:
    """A whole text, and the state it was made on.

    Attributes:
        text (str):
        submit_id (str):
        base_token (None | str | Unset):
        base_etag (int | None | Unset):
    """

    text: str
    submit_id: str
    base_token: None | str | Unset = UNSET
    base_etag: int | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        text = self.text

        submit_id = self.submit_id

        base_token: None | str | Unset
        if isinstance(self.base_token, Unset):
            base_token = UNSET
        else:
            base_token = self.base_token

        base_etag: int | None | Unset
        if isinstance(self.base_etag, Unset):
            base_etag = UNSET
        else:
            base_etag = self.base_etag

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "text": text,
                "submitId": submit_id,
            }
        )
        if base_token is not UNSET:
            field_dict["baseToken"] = base_token
        if base_etag is not UNSET:
            field_dict["baseEtag"] = base_etag

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        text = d.pop("text")

        submit_id = d.pop("submitId")

        def _parse_base_token(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        base_token = _parse_base_token(d.pop("baseToken", UNSET))

        def _parse_base_etag(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        base_etag = _parse_base_etag(d.pop("baseEtag", UNSET))

        live_text_submit = cls(
            text=text,
            submit_id=submit_id,
            base_token=base_token,
            base_etag=base_etag,
        )

        live_text_submit.additional_properties = d
        return live_text_submit

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
