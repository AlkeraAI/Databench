from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

if TYPE_CHECKING:
    from ..models.kernel_info import KernelInfo


T = TypeVar("T", bound="KernelResult")


@_attrs_define
class KernelResult:
    """The kernel as the platform last heard of it (``state`` is ``absent``
    when it has none), and whether the action was handed on.

        Attributes:
            kernel (KernelInfo):
            accepted (bool):
    """

    kernel: KernelInfo
    accepted: bool
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        kernel = self.kernel.to_dict()

        accepted = self.accepted

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "kernel": kernel,
                "accepted": accepted,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.kernel_info import KernelInfo

        d = dict(src_dict)
        kernel = KernelInfo.from_dict(d.pop("kernel"))

        accepted = d.pop("accepted")

        kernel_result = cls(
            kernel=kernel,
            accepted=accepted,
        )

        kernel_result.additional_properties = d
        return kernel_result

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
