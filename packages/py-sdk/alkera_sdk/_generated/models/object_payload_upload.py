from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

if TYPE_CHECKING:
    from ..models.result_blob_envelope_document import ResultBlobEnvelopeDocument
    from ..models.result_receipt_document import ResultReceiptDocument


T = TypeVar("T", bound="ObjectPayloadUpload")


@_attrs_define
class ObjectPayloadUpload:
    """The daemon's promote upload: the envelope and the receipt that proves it.

    Attributes:
        envelope (ResultBlobEnvelopeDocument):
        receipt (ResultReceiptDocument):
    """

    envelope: ResultBlobEnvelopeDocument
    receipt: ResultReceiptDocument
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        envelope = self.envelope.to_dict()

        receipt = self.receipt.to_dict()

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "envelope": envelope,
                "receipt": receipt,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.result_blob_envelope_document import (
            ResultBlobEnvelopeDocument,
        )
        from ..models.result_receipt_document import ResultReceiptDocument

        d = dict(src_dict)
        envelope = ResultBlobEnvelopeDocument.from_dict(d.pop("envelope"))

        receipt = ResultReceiptDocument.from_dict(d.pop("receipt"))

        object_payload_upload = cls(
            envelope=envelope,
            receipt=receipt,
        )

        object_payload_upload.additional_properties = d
        return object_payload_upload

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
