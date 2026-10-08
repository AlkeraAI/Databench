from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="WsTicketResponse")


@_attrs_define
class WsTicketResponse:
    """``POST /api/v1/ws/tickets``: a single-use, short-lived credential for
    one socket handshake. A box a release behind parses it, so a field
    this build does not know is ignored, never refused.

        Attributes:
            ticket (str):
            expires_in (int): Seconds until the ticket dies unused.
            path (str | Unset): The socket path to connect to. Default: '/api/v1/ws'.
    """

    ticket: str
    expires_in: int
    path: str | Unset = "/api/v1/ws"
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        ticket = self.ticket

        expires_in = self.expires_in

        path = self.path

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "ticket": ticket,
                "expires_in": expires_in,
            }
        )
        if path is not UNSET:
            field_dict["path"] = path

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        ticket = d.pop("ticket")

        expires_in = d.pop("expires_in")

        path = d.pop("path", UNSET)

        ws_ticket_response = cls(
            ticket=ticket,
            expires_in=expires_in,
            path=path,
        )

        ws_ticket_response.additional_properties = d
        return ws_ticket_response

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
