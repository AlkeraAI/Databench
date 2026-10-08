from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.package_info import PackageInfo


T = TypeVar("T", bound="EnvPackages")


@_attrs_define
class EnvPackages:
    """
    Attributes:
        env_id (str):
        packages (list[PackageInfo] | Unset):
        requirements (list[str] | Unset):
    """

    env_id: str
    packages: list[PackageInfo] | Unset = UNSET
    requirements: list[str] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        env_id = self.env_id

        packages: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.packages, Unset):
            packages = []
            for packages_item_data in self.packages:
                packages_item = packages_item_data.to_dict()
                packages.append(packages_item)

        requirements: list[str] | Unset = UNSET
        if not isinstance(self.requirements, Unset):
            requirements = self.requirements

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "env_id": env_id,
            }
        )
        if packages is not UNSET:
            field_dict["packages"] = packages
        if requirements is not UNSET:
            field_dict["requirements"] = requirements

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.package_info import PackageInfo

        d = dict(src_dict)
        env_id = d.pop("env_id")

        _packages = d.pop("packages", UNSET)
        packages: list[PackageInfo] | Unset = UNSET
        if _packages is not UNSET:
            packages = []
            for packages_item_data in _packages:
                packages_item = PackageInfo.from_dict(packages_item_data)

                packages.append(packages_item)

        requirements = cast(list[str], d.pop("requirements", UNSET))

        env_packages = cls(
            env_id=env_id,
            packages=packages,
            requirements=requirements,
        )

        env_packages.additional_properties = d
        return env_packages

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
