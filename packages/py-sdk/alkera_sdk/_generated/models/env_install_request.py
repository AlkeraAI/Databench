from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define

T = TypeVar("T", bound="EnvInstallRequest")


@_attrs_define
class EnvInstallRequest:
    """
    Attributes:
        packages (list[str]):
    """

    packages: list[str]

    def to_dict(self) -> dict[str, Any]:
        packages = self.packages

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "packages": packages,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        packages = cast(list[str], d.pop("packages"))

        env_install_request = cls(
            packages=packages,
        )

        return env_install_request
