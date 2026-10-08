from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.machine_read_status import MachineReadStatus
from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.box_machine_card import BoxMachineCard
    from ..models.machine_type_info import MachineTypeInfo


T = TypeVar("T", bound="MachineRead")


@_attrs_define
class MachineRead:
    """The org's workspace machine as the daemon and the machine banner see it.

    Attributes:
        id (str):
        name (str):
        provider (str):
        provider_pod_id (str):
        machine_type (MachineTypeInfo): One provisionable machine type from the catalog, priced for the caller.
        status (MachineReadStatus):
        created_at (datetime.datetime):
        lifecycle (str | Unset):  Default: 'workspace'.
        last_heartbeat_at (datetime.datetime | None | Unset):
        card (BoxMachineCard | None | Unset):
    """

    id: str
    name: str
    provider: str
    provider_pod_id: str
    machine_type: MachineTypeInfo
    status: MachineReadStatus
    created_at: datetime.datetime
    lifecycle: str | Unset = "workspace"
    last_heartbeat_at: datetime.datetime | None | Unset = UNSET
    card: BoxMachineCard | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.box_machine_card import BoxMachineCard

        id = self.id

        name = self.name

        provider = self.provider

        provider_pod_id = self.provider_pod_id

        machine_type = self.machine_type.to_dict()

        status = self.status.value

        created_at = self.created_at.isoformat()

        lifecycle = self.lifecycle

        last_heartbeat_at: None | str | Unset
        if isinstance(self.last_heartbeat_at, Unset):
            last_heartbeat_at = UNSET
        elif isinstance(self.last_heartbeat_at, datetime.datetime):
            last_heartbeat_at = self.last_heartbeat_at.isoformat()
        else:
            last_heartbeat_at = self.last_heartbeat_at

        card: dict[str, Any] | None | Unset
        if isinstance(self.card, Unset):
            card = UNSET
        elif isinstance(self.card, BoxMachineCard):
            card = self.card.to_dict()
        else:
            card = self.card

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "name": name,
                "provider": provider,
                "provider_pod_id": provider_pod_id,
                "machine_type": machine_type,
                "status": status,
                "created_at": created_at,
            }
        )
        if lifecycle is not UNSET:
            field_dict["lifecycle"] = lifecycle
        if last_heartbeat_at is not UNSET:
            field_dict["last_heartbeat_at"] = last_heartbeat_at
        if card is not UNSET:
            field_dict["card"] = card

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.box_machine_card import BoxMachineCard
        from ..models.machine_type_info import MachineTypeInfo

        d = dict(src_dict)
        id = d.pop("id")

        name = d.pop("name")

        provider = d.pop("provider")

        provider_pod_id = d.pop("provider_pod_id")

        machine_type = MachineTypeInfo.from_dict(d.pop("machine_type"))

        status = MachineReadStatus(d.pop("status"))

        created_at = datetime.datetime.fromisoformat(d.pop("created_at"))

        lifecycle = d.pop("lifecycle", UNSET)

        def _parse_last_heartbeat_at(data: object) -> datetime.datetime | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                last_heartbeat_at_type_0 = datetime.datetime.fromisoformat(data)

                return last_heartbeat_at_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None | Unset, data)

        last_heartbeat_at = _parse_last_heartbeat_at(d.pop("last_heartbeat_at", UNSET))

        def _parse_card(data: object) -> BoxMachineCard | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                card_type_0 = BoxMachineCard.from_dict(data)

                return card_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(BoxMachineCard | None | Unset, data)

        card = _parse_card(d.pop("card", UNSET))

        machine_read = cls(
            id=id,
            name=name,
            provider=provider,
            provider_pod_id=provider_pod_id,
            machine_type=machine_type,
            status=status,
            created_at=created_at,
            lifecycle=lifecycle,
            last_heartbeat_at=last_heartbeat_at,
            card=card,
        )

        machine_read.additional_properties = d
        return machine_read

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
