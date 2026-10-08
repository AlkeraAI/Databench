from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

T = TypeVar("T", bound="UploadStatusResponse")


@_attrs_define
class UploadStatusResponse:
    """
    Attributes:
        upload_id (str):
        state (str):
        offset (int):
        length (int):
        complete (bool):
        parts_done (int):
        parts_total (int):
        accepted_parts (list[int]):
        expires_at (str):
    """

    upload_id: str
    state: str
    offset: int
    length: int
    complete: bool
    parts_done: int
    parts_total: int
    accepted_parts: list[int]
    expires_at: str
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        upload_id = self.upload_id

        state = self.state

        offset = self.offset

        length = self.length

        complete = self.complete

        parts_done = self.parts_done

        parts_total = self.parts_total

        accepted_parts = self.accepted_parts

        expires_at = self.expires_at

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "uploadId": upload_id,
                "state": state,
                "offset": offset,
                "length": length,
                "complete": complete,
                "partsDone": parts_done,
                "partsTotal": parts_total,
                "acceptedParts": accepted_parts,
                "expiresAt": expires_at,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        upload_id = d.pop("uploadId")

        state = d.pop("state")

        offset = d.pop("offset")

        length = d.pop("length")

        complete = d.pop("complete")

        parts_done = d.pop("partsDone")

        parts_total = d.pop("partsTotal")

        accepted_parts = cast(list[int], d.pop("acceptedParts"))

        expires_at = d.pop("expiresAt")

        upload_status_response = cls(
            upload_id=upload_id,
            state=state,
            offset=offset,
            length=length,
            complete=complete,
            parts_done=parts_done,
            parts_total=parts_total,
            accepted_parts=accepted_parts,
            expires_at=expires_at,
        )

        upload_status_response.additional_properties = d
        return upload_status_response

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
