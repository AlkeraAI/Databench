from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

T = TypeVar("T", bound="EnvChangeRequest")


@_attrs_define
class EnvChangeRequest:
    """
    Attributes:
        packages (list[str] | Unset):
    """

    packages: list[str] | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        packages: list[str] | Unset = UNSET
        if not isinstance(self.packages, Unset):
            packages = self.packages

        field_dict: dict[str, Any] = {}

        field_dict.update({})
        if packages is not UNSET:
            field_dict["packages"] = packages

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        packages = cast(list[str], d.pop("packages", UNSET))

        env_change_request = cls(
            packages=packages,
        )

        return env_change_request
