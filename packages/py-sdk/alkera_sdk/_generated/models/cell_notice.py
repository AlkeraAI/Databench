from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.data import Data


T = TypeVar("T", bound="CellNotice")


@_attrs_define
class CellNotice:
    """Something a reader of the document should know about a cell.

    ``kind`` is an open string. Core kinds: ``cell_running``,
    ``edited_deleted_cell``, ``external_conflict``, ``kind_changed_by_other``,
    ``upstream_being_edited``. The engine also emits ``stale_base``,
    ``duplicate_name``, ``invalid_file``,
    ``file_deleted``, ``corrupt_snapshot``, ``memory_warning``.

        Attributes:
            kind (str):
            cell_id (None | str | Unset):
            message (str | Unset):  Default: ''.
            by (None | str | Unset):
            data (Data | Unset):
    """

    kind: str
    cell_id: None | str | Unset = UNSET
    message: str | Unset = ""
    by: None | str | Unset = UNSET
    data: Data | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        kind = self.kind

        cell_id: None | str | Unset
        if isinstance(self.cell_id, Unset):
            cell_id = UNSET
        else:
            cell_id = self.cell_id

        message = self.message

        by: None | str | Unset
        if isinstance(self.by, Unset):
            by = UNSET
        else:
            by = self.by

        data: dict[str, Any] | Unset = UNSET
        if not isinstance(self.data, Unset):
            data = self.data.to_dict()

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "kind": kind,
            }
        )
        if cell_id is not UNSET:
            field_dict["cell_id"] = cell_id
        if message is not UNSET:
            field_dict["message"] = message
        if by is not UNSET:
            field_dict["by"] = by
        if data is not UNSET:
            field_dict["data"] = data

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.data import Data

        d = dict(src_dict)
        kind = d.pop("kind")

        def _parse_cell_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        cell_id = _parse_cell_id(d.pop("cell_id", UNSET))

        message = d.pop("message", UNSET)

        def _parse_by(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        by = _parse_by(d.pop("by", UNSET))

        _data = d.pop("data", UNSET)
        data: Data | Unset
        if isinstance(_data, Unset):
            data = UNSET
        else:
            data = Data.from_dict(_data)

        cell_notice = cls(
            kind=kind,
            cell_id=cell_id,
            message=message,
            by=by,
            data=data,
        )

        cell_notice.additional_properties = d
        return cell_notice

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
