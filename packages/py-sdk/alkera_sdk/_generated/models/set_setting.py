from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, TypeVar, cast

from attrs import define as _attrs_define

T = TypeVar("T", bound="SetSetting")


@_attrs_define
class SetSetting:
    """
    Attributes:
        op (Literal['set_setting']):
        key (str):
        value (Any):
    """

    op: Literal["set_setting"]
    key: str
    value: Any

    def to_dict(self) -> dict[str, Any]:
        op = self.op

        key = self.key

        value = self.value

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "op": op,
                "key": key,
                "value": value,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        op = cast(Literal["set_setting"], d.pop("op"))
        if op != "set_setting":
            raise ValueError(f"op must match const 'set_setting', got '{op}'")

        key = d.pop("key")

        value = d.pop("value")

        set_setting = cls(
            op=op,
            key=key,
            value=value,
        )

        return set_setting
