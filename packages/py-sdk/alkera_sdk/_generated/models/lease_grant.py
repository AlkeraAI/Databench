from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.live_cadence_wire import LiveCadenceWire


T = TypeVar("T", bound="LeaseGrant")


@_attrs_define
class LeaseGrant:
    """What a holder is handed: the epoch it writes under and the cadences it
    must keep. The cadences are served rather than compiled into the client
    so a deployment can slow them down without shipping a new one.

    ``inboundPending`` rides every grant — the acquire AND every beat — so a
    holder learns that a person dropped a file into the chat without asking a
    second question at its own cadence: the beat it already sends is the poll.

        Attributes:
            epoch (int):
            expires_at (datetime.datetime):
            heartbeat_every (float):
            sync_interval (float):
            live (LiveCadenceWire): How the holder is told to run the live plane: how long to wait for a file
                to stop changing, how often to report, and the three ceilings past which it
                must skeleton or defer rather than upload.

                Served rather than compiled into the client for the same reason the two
                lease cadences are: a deployment that has to slow the plane down — a busy
                org, a store under pressure — changes a setting instead of waiting for
                every box in the field to update. ``inbound`` is what this folder does with
                a write from someone who is not the holder, so a client reads whether to
                drain at all off the same block it reads its cadence from.
            forced (bool | Unset):  Default: False.
            inbound_pending (int | Unset):  Default: 0.
    """

    epoch: int
    expires_at: datetime.datetime
    heartbeat_every: float
    sync_interval: float
    live: LiveCadenceWire
    forced: bool | Unset = False
    inbound_pending: int | Unset = 0
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        epoch = self.epoch

        expires_at = self.expires_at.isoformat()

        heartbeat_every = self.heartbeat_every

        sync_interval = self.sync_interval

        live = self.live.to_dict()

        forced = self.forced

        inbound_pending = self.inbound_pending

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "epoch": epoch,
                "expiresAt": expires_at,
                "heartbeatEvery": heartbeat_every,
                "syncInterval": sync_interval,
                "live": live,
            }
        )
        if forced is not UNSET:
            field_dict["forced"] = forced
        if inbound_pending is not UNSET:
            field_dict["inboundPending"] = inbound_pending

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.live_cadence_wire import LiveCadenceWire

        d = dict(src_dict)
        epoch = d.pop("epoch")

        expires_at = datetime.datetime.fromisoformat(d.pop("expiresAt"))

        heartbeat_every = d.pop("heartbeatEvery")

        sync_interval = d.pop("syncInterval")

        live = LiveCadenceWire.from_dict(d.pop("live"))

        forced = d.pop("forced", UNSET)

        inbound_pending = d.pop("inboundPending", UNSET)

        lease_grant = cls(
            epoch=epoch,
            expires_at=expires_at,
            heartbeat_every=heartbeat_every,
            sync_interval=sync_interval,
            live=live,
            forced=forced,
            inbound_pending=inbound_pending,
        )

        lease_grant.additional_properties = d
        return lease_grant

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
