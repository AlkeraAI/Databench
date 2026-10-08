from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Literal, TypeVar, cast

from attrs import define as _attrs_define

if TYPE_CHECKING:
    from ..models.cell_config_changes import CellConfigChanges


T = TypeVar("T", bound="SetCellConfig")


@_attrs_define
class SetCellConfig:
    """
    Attributes:
        op (Literal['set_config']):
        cell_id (str):
        config (CellConfigChanges):
    """

    op: Literal["set_config"]
    cell_id: str
    config: CellConfigChanges

    def to_dict(self) -> dict[str, Any]:
        op = self.op

        cell_id = self.cell_id

        config = self.config.to_dict()

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "op": op,
                "cell_id": cell_id,
                "config": config,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.cell_config_changes import CellConfigChanges

        d = dict(src_dict)
        op = cast(Literal["set_config"], d.pop("op"))
        if op != "set_config":
            raise ValueError(f"op must match const 'set_config', got '{op}'")

        cell_id = d.pop("cell_id")

        config = CellConfigChanges.from_dict(d.pop("config"))

        set_cell_config = cls(
            op=op,
            cell_id=cell_id,
            config=config,
        )

        return set_cell_config
