from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define

from ..models.kernel_request_action import KernelRequestAction

T = TypeVar("T", bound="KernelRequest")


@_attrs_define
class KernelRequest:
    """
    Attributes:
        action (KernelRequestAction):
    """

    action: KernelRequestAction

    def to_dict(self) -> dict[str, Any]:
        action = self.action.value

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "action": action,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        action = KernelRequestAction(d.pop("action"))

        kernel_request = cls(
            action=action,
        )

        return kernel_request
