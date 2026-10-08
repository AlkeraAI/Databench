from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.env_info import EnvInfo


T = TypeVar("T", bound="EnvListing")


@_attrs_define
class EnvListing:
    """The notebook's environment and every environment found for it, and
    whether the workspace's members share its environments on this machine.

        Attributes:
            current (EnvInfo):
            envs (list[EnvInfo] | Unset):
            shared (bool | Unset):  Default: True.
    """

    current: EnvInfo
    envs: list[EnvInfo] | Unset = UNSET
    shared: bool | Unset = True
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        current = self.current.to_dict()

        envs: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.envs, Unset):
            envs = []
            for envs_item_data in self.envs:
                envs_item = envs_item_data.to_dict()
                envs.append(envs_item)

        shared = self.shared

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "current": current,
            }
        )
        if envs is not UNSET:
            field_dict["envs"] = envs
        if shared is not UNSET:
            field_dict["shared"] = shared

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.env_info import EnvInfo

        d = dict(src_dict)
        current = EnvInfo.from_dict(d.pop("current"))

        _envs = d.pop("envs", UNSET)
        envs: list[EnvInfo] | Unset = UNSET
        if _envs is not UNSET:
            envs = []
            for envs_item_data in _envs:
                envs_item = EnvInfo.from_dict(envs_item_data)

                envs.append(envs_item)

        shared = d.pop("shared", UNSET)

        env_listing = cls(
            current=current,
            envs=envs,
            shared=shared,
        )

        env_listing.additional_properties = d
        return env_listing

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
