from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define

from ..models.realtime_protocol_descriptor_doc_types_item import (
    RealtimeProtocolDescriptorDocTypesItem,
)
from ..models.realtime_protocol_descriptor_envelope_kinds_item import (
    RealtimeProtocolDescriptorEnvelopeKindsItem,
)
from ..models.realtime_protocol_descriptor_op_intents_item import (
    RealtimeProtocolDescriptorOpIntentsItem,
)
from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.close_codes import CloseCodes


T = TypeVar("T", bound="RealtimeProtocolDescriptor")


@_attrs_define
class RealtimeProtocolDescriptor:
    """``GET /api/v1/ws/protocol``: the vocabulary this server speaks, so a
    client can pin itself against it.

        Attributes:
            schema_version (str): The DocEnvelope schema version this server writes.
            envelope_kinds (list[RealtimeProtocolDescriptorEnvelopeKindsItem]):
            doc_types (list[RealtimeProtocolDescriptorDocTypesItem]):
            op_intents (list[RealtimeProtocolDescriptorOpIntentsItem]):
            close_codes (CloseCodes): Close-code names to their numeric codes.
            channel_pattern (str): The regular expression a channel name must match.
            path (str | Unset):  Default: '/api/v1/ws'.
            subprotocol (str | Unset):  Default: 'alkera-v1'.
            ticket_subprotocol_prefix (str | Unset):  Default: 'alkera-ticket.'.
    """

    schema_version: str
    envelope_kinds: list[RealtimeProtocolDescriptorEnvelopeKindsItem]
    doc_types: list[RealtimeProtocolDescriptorDocTypesItem]
    op_intents: list[RealtimeProtocolDescriptorOpIntentsItem]
    close_codes: CloseCodes
    channel_pattern: str
    path: str | Unset = "/api/v1/ws"
    subprotocol: str | Unset = "alkera-v1"
    ticket_subprotocol_prefix: str | Unset = "alkera-ticket."

    def to_dict(self) -> dict[str, Any]:
        schema_version = self.schema_version

        envelope_kinds = []
        for envelope_kinds_item_data in self.envelope_kinds:
            envelope_kinds_item = envelope_kinds_item_data.value
            envelope_kinds.append(envelope_kinds_item)

        doc_types = []
        for doc_types_item_data in self.doc_types:
            doc_types_item = doc_types_item_data.value
            doc_types.append(doc_types_item)

        op_intents = []
        for op_intents_item_data in self.op_intents:
            op_intents_item = op_intents_item_data.value
            op_intents.append(op_intents_item)

        close_codes = self.close_codes.to_dict()

        channel_pattern = self.channel_pattern

        path = self.path

        subprotocol = self.subprotocol

        ticket_subprotocol_prefix = self.ticket_subprotocol_prefix

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "schema_version": schema_version,
                "envelope_kinds": envelope_kinds,
                "doc_types": doc_types,
                "op_intents": op_intents,
                "close_codes": close_codes,
                "channel_pattern": channel_pattern,
            }
        )
        if path is not UNSET:
            field_dict["path"] = path
        if subprotocol is not UNSET:
            field_dict["subprotocol"] = subprotocol
        if ticket_subprotocol_prefix is not UNSET:
            field_dict["ticket_subprotocol_prefix"] = ticket_subprotocol_prefix

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.close_codes import CloseCodes

        d = dict(src_dict)
        schema_version = d.pop("schema_version")

        envelope_kinds = []
        _envelope_kinds = d.pop("envelope_kinds")
        for envelope_kinds_item_data in _envelope_kinds:
            envelope_kinds_item = RealtimeProtocolDescriptorEnvelopeKindsItem(
                envelope_kinds_item_data
            )

            envelope_kinds.append(envelope_kinds_item)

        doc_types = []
        _doc_types = d.pop("doc_types")
        for doc_types_item_data in _doc_types:
            doc_types_item = RealtimeProtocolDescriptorDocTypesItem(doc_types_item_data)

            doc_types.append(doc_types_item)

        op_intents = []
        _op_intents = d.pop("op_intents")
        for op_intents_item_data in _op_intents:
            op_intents_item = RealtimeProtocolDescriptorOpIntentsItem(op_intents_item_data)

            op_intents.append(op_intents_item)

        close_codes = CloseCodes.from_dict(d.pop("close_codes"))

        channel_pattern = d.pop("channel_pattern")

        path = d.pop("path", UNSET)

        subprotocol = d.pop("subprotocol", UNSET)

        ticket_subprotocol_prefix = d.pop("ticket_subprotocol_prefix", UNSET)

        realtime_protocol_descriptor = cls(
            schema_version=schema_version,
            envelope_kinds=envelope_kinds,
            doc_types=doc_types,
            op_intents=op_intents,
            close_codes=close_codes,
            channel_pattern=channel_pattern,
            path=path,
            subprotocol=subprotocol,
            ticket_subprotocol_prefix=ticket_subprotocol_prefix,
        )

        return realtime_protocol_descriptor
