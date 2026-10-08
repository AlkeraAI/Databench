from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.cell_after_op_status_type_0 import CellAfterOpStatusType0
from ..types import UNSET, Unset

T = TypeVar("T", bound="CellAfterOp")


@_attrs_define
class CellAfterOp:
    """A cell a batch touched, as it is after the batch.

    ``index`` is its place among the live cells, ``None`` once it is deleted.
    ``status`` is its run status when the writer knows it: the engine's client
    fills it; a document store, which holds no run state, leaves it ``None``.

        Attributes:
            id (str):
            name (str):
            kind (str):
            index (int | None):
            deleted (bool | Unset):  Default: False.
            status (CellAfterOpStatusType0 | None | Unset):
    """

    id: str
    name: str
    kind: str
    index: int | None
    deleted: bool | Unset = False
    status: CellAfterOpStatusType0 | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        id = self.id

        name = self.name

        kind = self.kind

        index: int | None
        index = self.index

        deleted = self.deleted

        status: None | str | Unset
        if isinstance(self.status, Unset):
            status = UNSET
        elif isinstance(self.status, CellAfterOpStatusType0):
            status = self.status.value
        else:
            status = self.status

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "name": name,
                "kind": kind,
                "index": index,
            }
        )
        if deleted is not UNSET:
            field_dict["deleted"] = deleted
        if status is not UNSET:
            field_dict["status"] = status

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        id = d.pop("id")

        name = d.pop("name")

        kind = d.pop("kind")

        def _parse_index(data: object) -> int | None:
            if data is None:
                return data
            return cast(int | None, data)

        index = _parse_index(d.pop("index"))

        deleted = d.pop("deleted", UNSET)

        def _parse_status(data: object) -> CellAfterOpStatusType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                status_type_0 = CellAfterOpStatusType0(data)

                return status_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(CellAfterOpStatusType0 | None | Unset, data)

        status = _parse_status(d.pop("status", UNSET))

        cell_after_op = cls(
            id=id,
            name=name,
            kind=kind,
            index=index,
            deleted=deleted,
            status=status,
        )

        cell_after_op.additional_properties = d
        return cell_after_op

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
