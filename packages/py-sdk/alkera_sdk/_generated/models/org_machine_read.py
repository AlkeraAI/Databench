from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.org_machine_read_acquisition import OrgMachineReadAcquisition
from ..models.org_machine_read_credit_state import OrgMachineReadCreditState
from ..models.org_machine_read_use_mode import OrgMachineReadUseMode
from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.audience_entry import AudienceEntry
    from ..models.disk_grow_read import DiskGrowRead
    from ..models.machine_card import MachineCard


T = TypeVar("T", bound="OrgMachineRead")


@_attrs_define
class OrgMachineRead:
    """
    Attributes:
        id (str):
        name (str):
        card (MachineCard): One machine as every surface draws it: an org machine, the shared
            machines, a person's own box, or a box a member registered for the org.
        use_mode (OrgMachineReadUseMode):
        acquisition (OrgMachineReadAcquisition):
        audience (list[AudienceEntry]):
        owner_team_id (str):
        owner_team_name (str):
        version (int):
        can_manage (bool):
        can_use (bool):
        credit_state (OrgMachineReadCreditState):
        created_at (datetime.datetime):
        free_until (datetime.datetime | None | Unset):
        idle_stop_minutes (int | None | Unset):
        monthly_cap_nanos (int | None | Unset):
        can_replace (bool | Unset):  Default: False.
        org_default (bool | Unset):  Default: False.
        spend_this_cycle_nanos (int | None | Unset):
        runway_minutes (int | None | Unset):
        drain_stops_at (datetime.datetime | None | Unset):
        disk_grow (DiskGrowRead | None | Unset):
    """

    id: str
    name: str
    card: MachineCard
    use_mode: OrgMachineReadUseMode
    acquisition: OrgMachineReadAcquisition
    audience: list[AudienceEntry]
    owner_team_id: str
    owner_team_name: str
    version: int
    can_manage: bool
    can_use: bool
    credit_state: OrgMachineReadCreditState
    created_at: datetime.datetime
    free_until: datetime.datetime | None | Unset = UNSET
    idle_stop_minutes: int | None | Unset = UNSET
    monthly_cap_nanos: int | None | Unset = UNSET
    can_replace: bool | Unset = False
    org_default: bool | Unset = False
    spend_this_cycle_nanos: int | None | Unset = UNSET
    runway_minutes: int | None | Unset = UNSET
    drain_stops_at: datetime.datetime | None | Unset = UNSET
    disk_grow: DiskGrowRead | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.disk_grow_read import DiskGrowRead

        id = self.id

        name = self.name

        card = self.card.to_dict()

        use_mode = self.use_mode.value

        acquisition = self.acquisition.value

        audience = []
        for audience_item_data in self.audience:
            audience_item = audience_item_data.to_dict()
            audience.append(audience_item)

        owner_team_id = self.owner_team_id

        owner_team_name = self.owner_team_name

        version = self.version

        can_manage = self.can_manage

        can_use = self.can_use

        credit_state = self.credit_state.value

        created_at = self.created_at.isoformat()

        free_until: None | str | Unset
        if isinstance(self.free_until, Unset):
            free_until = UNSET
        elif isinstance(self.free_until, datetime.datetime):
            free_until = self.free_until.isoformat()
        else:
            free_until = self.free_until

        idle_stop_minutes: int | None | Unset
        if isinstance(self.idle_stop_minutes, Unset):
            idle_stop_minutes = UNSET
        else:
            idle_stop_minutes = self.idle_stop_minutes

        monthly_cap_nanos: int | None | Unset
        if isinstance(self.monthly_cap_nanos, Unset):
            monthly_cap_nanos = UNSET
        else:
            monthly_cap_nanos = self.monthly_cap_nanos

        can_replace = self.can_replace

        org_default = self.org_default

        spend_this_cycle_nanos: int | None | Unset
        if isinstance(self.spend_this_cycle_nanos, Unset):
            spend_this_cycle_nanos = UNSET
        else:
            spend_this_cycle_nanos = self.spend_this_cycle_nanos

        runway_minutes: int | None | Unset
        if isinstance(self.runway_minutes, Unset):
            runway_minutes = UNSET
        else:
            runway_minutes = self.runway_minutes

        drain_stops_at: None | str | Unset
        if isinstance(self.drain_stops_at, Unset):
            drain_stops_at = UNSET
        elif isinstance(self.drain_stops_at, datetime.datetime):
            drain_stops_at = self.drain_stops_at.isoformat()
        else:
            drain_stops_at = self.drain_stops_at

        disk_grow: dict[str, Any] | None | Unset
        if isinstance(self.disk_grow, Unset):
            disk_grow = UNSET
        elif isinstance(self.disk_grow, DiskGrowRead):
            disk_grow = self.disk_grow.to_dict()
        else:
            disk_grow = self.disk_grow

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "name": name,
                "card": card,
                "use_mode": use_mode,
                "acquisition": acquisition,
                "audience": audience,
                "owner_team_id": owner_team_id,
                "owner_team_name": owner_team_name,
                "version": version,
                "can_manage": can_manage,
                "can_use": can_use,
                "credit_state": credit_state,
                "created_at": created_at,
            }
        )
        if free_until is not UNSET:
            field_dict["free_until"] = free_until
        if idle_stop_minutes is not UNSET:
            field_dict["idle_stop_minutes"] = idle_stop_minutes
        if monthly_cap_nanos is not UNSET:
            field_dict["monthly_cap_nanos"] = monthly_cap_nanos
        if can_replace is not UNSET:
            field_dict["can_replace"] = can_replace
        if org_default is not UNSET:
            field_dict["org_default"] = org_default
        if spend_this_cycle_nanos is not UNSET:
            field_dict["spend_this_cycle_nanos"] = spend_this_cycle_nanos
        if runway_minutes is not UNSET:
            field_dict["runway_minutes"] = runway_minutes
        if drain_stops_at is not UNSET:
            field_dict["drain_stops_at"] = drain_stops_at
        if disk_grow is not UNSET:
            field_dict["disk_grow"] = disk_grow

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.audience_entry import AudienceEntry
        from ..models.disk_grow_read import DiskGrowRead
        from ..models.machine_card import MachineCard

        d = dict(src_dict)
        id = d.pop("id")

        name = d.pop("name")

        card = MachineCard.from_dict(d.pop("card"))

        use_mode = OrgMachineReadUseMode(d.pop("use_mode"))

        acquisition = OrgMachineReadAcquisition(d.pop("acquisition"))

        audience = []
        _audience = d.pop("audience")
        for audience_item_data in _audience:
            audience_item = AudienceEntry.from_dict(audience_item_data)

            audience.append(audience_item)

        owner_team_id = d.pop("owner_team_id")

        owner_team_name = d.pop("owner_team_name")

        version = d.pop("version")

        can_manage = d.pop("can_manage")

        can_use = d.pop("can_use")

        credit_state = OrgMachineReadCreditState(d.pop("credit_state"))

        created_at = datetime.datetime.fromisoformat(d.pop("created_at"))

        def _parse_free_until(data: object) -> datetime.datetime | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                free_until_type_0 = datetime.datetime.fromisoformat(data)

                return free_until_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None | Unset, data)

        free_until = _parse_free_until(d.pop("free_until", UNSET))

        def _parse_idle_stop_minutes(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        idle_stop_minutes = _parse_idle_stop_minutes(d.pop("idle_stop_minutes", UNSET))

        def _parse_monthly_cap_nanos(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        monthly_cap_nanos = _parse_monthly_cap_nanos(d.pop("monthly_cap_nanos", UNSET))

        can_replace = d.pop("can_replace", UNSET)

        org_default = d.pop("org_default", UNSET)

        def _parse_spend_this_cycle_nanos(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        spend_this_cycle_nanos = _parse_spend_this_cycle_nanos(
            d.pop("spend_this_cycle_nanos", UNSET)
        )

        def _parse_runway_minutes(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        runway_minutes = _parse_runway_minutes(d.pop("runway_minutes", UNSET))

        def _parse_drain_stops_at(data: object) -> datetime.datetime | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                drain_stops_at_type_0 = datetime.datetime.fromisoformat(data)

                return drain_stops_at_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None | Unset, data)

        drain_stops_at = _parse_drain_stops_at(d.pop("drain_stops_at", UNSET))

        def _parse_disk_grow(data: object) -> DiskGrowRead | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                disk_grow_type_0 = DiskGrowRead.from_dict(data)

                return disk_grow_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(DiskGrowRead | None | Unset, data)

        disk_grow = _parse_disk_grow(d.pop("disk_grow", UNSET))

        org_machine_read = cls(
            id=id,
            name=name,
            card=card,
            use_mode=use_mode,
            acquisition=acquisition,
            audience=audience,
            owner_team_id=owner_team_id,
            owner_team_name=owner_team_name,
            version=version,
            can_manage=can_manage,
            can_use=can_use,
            credit_state=credit_state,
            created_at=created_at,
            free_until=free_until,
            idle_stop_minutes=idle_stop_minutes,
            monthly_cap_nanos=monthly_cap_nanos,
            can_replace=can_replace,
            org_default=org_default,
            spend_this_cycle_nanos=spend_this_cycle_nanos,
            runway_minutes=runway_minutes,
            drain_stops_at=drain_stops_at,
            disk_grow=disk_grow,
        )

        org_machine_read.additional_properties = d
        return org_machine_read

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
