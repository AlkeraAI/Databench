from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.acquire_body_purpose import AcquireBodyPurpose
from ..types import UNSET, Unset

T = TypeVar("T", bound="AcquireBody")


@_attrs_define
class AcquireBody:
    """What a holder asks for when it takes a folder.

    ``inbound`` is "take what other people write here and keep it for me to
    apply" and ``live`` is "I will run the live plane over this folder": either
    one turns the folder's live plane on, because a folder that keeps drops
    nobody drains and a holder that drains a folder keeping none are the same
    mistake told from opposite ends. A holder that asks for neither gets what
    its purpose means — a chat's or a workspace's lease takes drops, because that is why the box
    holds the folder at all; a mount does not, because the machine owns that
    tree outright.

        Attributes:
            instance_id (str):
            machine_id (str):
            purpose (AcquireBodyPurpose | Unset):  Default: AcquireBodyPurpose.MOUNT.
            ttl (int | None | Unset):
            inbound (bool | Unset):  Default: False.
            live (bool | Unset):  Default: False.
            retake (bool | Unset):  Default: False.
    """

    instance_id: str
    machine_id: str
    purpose: AcquireBodyPurpose | Unset = AcquireBodyPurpose.MOUNT
    ttl: int | None | Unset = UNSET
    inbound: bool | Unset = False
    live: bool | Unset = False
    retake: bool | Unset = False
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        instance_id = self.instance_id

        machine_id = self.machine_id

        purpose: str | Unset = UNSET
        if not isinstance(self.purpose, Unset):
            purpose = self.purpose.value

        ttl: int | None | Unset
        if isinstance(self.ttl, Unset):
            ttl = UNSET
        else:
            ttl = self.ttl

        inbound = self.inbound

        live = self.live

        retake = self.retake

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "instanceId": instance_id,
                "machineId": machine_id,
            }
        )
        if purpose is not UNSET:
            field_dict["purpose"] = purpose
        if ttl is not UNSET:
            field_dict["ttl"] = ttl
        if inbound is not UNSET:
            field_dict["inbound"] = inbound
        if live is not UNSET:
            field_dict["live"] = live
        if retake is not UNSET:
            field_dict["retake"] = retake

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        instance_id = d.pop("instanceId")

        machine_id = d.pop("machineId")

        _purpose = d.pop("purpose", UNSET)
        purpose: AcquireBodyPurpose | Unset
        if isinstance(_purpose, Unset):
            purpose = UNSET
        else:
            purpose = AcquireBodyPurpose(_purpose)

        def _parse_ttl(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        ttl = _parse_ttl(d.pop("ttl", UNSET))

        inbound = d.pop("inbound", UNSET)

        live = d.pop("live", UNSET)

        retake = d.pop("retake", UNSET)

        acquire_body = cls(
            instance_id=instance_id,
            machine_id=machine_id,
            purpose=purpose,
            ttl=ttl,
            inbound=inbound,
            live=live,
            retake=retake,
        )

        acquire_body.additional_properties = d
        return acquire_body

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
