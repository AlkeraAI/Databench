from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

T = TypeVar("T", bound="OutputsClearRequest")


@_attrs_define
class OutputsClearRequest:
    """Clear these cells' outputs for everyone, or every cell's when
    ``cell_ids`` is ``None``.

        Attributes:
            cell_ids (list[str] | None | Unset):
    """

    cell_ids: list[str] | None | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        cell_ids: list[str] | None | Unset
        if isinstance(self.cell_ids, Unset):
            cell_ids = UNSET
        elif isinstance(self.cell_ids, list):
            cell_ids = self.cell_ids

        else:
            cell_ids = self.cell_ids

        field_dict: dict[str, Any] = {}

        field_dict.update({})
        if cell_ids is not UNSET:
            field_dict["cell_ids"] = cell_ids

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)

        def _parse_cell_ids(data: object) -> list[str] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                cell_ids_type_0 = cast(list[str], data)

                return cell_ids_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[str] | None | Unset, data)

        cell_ids = _parse_cell_ids(d.pop("cell_ids", UNSET))

        outputs_clear_request = cls(
            cell_ids=cell_ids,
        )

        return outputs_clear_request
