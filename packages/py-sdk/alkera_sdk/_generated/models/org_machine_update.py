from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.org_machine_update_use_mode_type_0 import OrgMachineUpdateUseModeType0
from ..types import UNSET, Unset

T = TypeVar("T", bound="OrgMachineUpdate")


@_attrs_define
class OrgMachineUpdate:
    """Fields not sent are left as they are (read ``model_fields_set``); a
    field sent as ``null`` clears it where clearing means something
    (``idle_stop_minutes``: never stop; ``monthly_cap_nanos``: no cap).

        Attributes:
            name (None | str | Unset):
            idle_stop_minutes (int | None | Unset):
            monthly_cap_nanos (int | None | Unset):
            use_mode (None | OrgMachineUpdateUseModeType0 | Unset):
    """

    name: None | str | Unset = UNSET
    idle_stop_minutes: int | None | Unset = UNSET
    monthly_cap_nanos: int | None | Unset = UNSET
    use_mode: None | OrgMachineUpdateUseModeType0 | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        name: None | str | Unset
        if isinstance(self.name, Unset):
            name = UNSET
        else:
            name = self.name

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

        use_mode: None | str | Unset
        if isinstance(self.use_mode, Unset):
            use_mode = UNSET
        elif isinstance(self.use_mode, OrgMachineUpdateUseModeType0):
            use_mode = self.use_mode.value
        else:
            use_mode = self.use_mode

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if name is not UNSET:
            field_dict["name"] = name
        if idle_stop_minutes is not UNSET:
            field_dict["idle_stop_minutes"] = idle_stop_minutes
        if monthly_cap_nanos is not UNSET:
            field_dict["monthly_cap_nanos"] = monthly_cap_nanos
        if use_mode is not UNSET:
            field_dict["use_mode"] = use_mode

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)

        def _parse_name(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        name = _parse_name(d.pop("name", UNSET))

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

        def _parse_use_mode(data: object) -> None | OrgMachineUpdateUseModeType0 | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                use_mode_type_0 = OrgMachineUpdateUseModeType0(data)

                return use_mode_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | OrgMachineUpdateUseModeType0 | Unset, data)

        use_mode = _parse_use_mode(d.pop("use_mode", UNSET))

        org_machine_update = cls(
            name=name,
            idle_stop_minutes=idle_stop_minutes,
            monthly_cap_nanos=monthly_cap_nanos,
            use_mode=use_mode,
        )

        org_machine_update.additional_properties = d
        return org_machine_update

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
