from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast
from uuid import UUID

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="ResolveResult")


@_attrs_define
class ResolveResult:
    """
    Attributes:
        id (UUID):
        state (str):
        node_id (UUID):
        head_version_id (UUID):
        copy_node_id (None | Unset | UUID):
    """

    id: UUID
    state: str
    node_id: UUID
    head_version_id: UUID
    copy_node_id: None | Unset | UUID = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        id = str(self.id)

        state = self.state

        node_id = str(self.node_id)

        head_version_id = str(self.head_version_id)

        copy_node_id: None | str | Unset
        if isinstance(self.copy_node_id, Unset):
            copy_node_id = UNSET
        elif isinstance(self.copy_node_id, UUID):
            copy_node_id = str(self.copy_node_id)
        else:
            copy_node_id = self.copy_node_id

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "state": state,
                "nodeId": node_id,
                "headVersionId": head_version_id,
            }
        )
        if copy_node_id is not UNSET:
            field_dict["copyNodeId"] = copy_node_id

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        id = UUID(d.pop("id"))

        state = d.pop("state")

        node_id = UUID(d.pop("nodeId"))

        head_version_id = UUID(d.pop("headVersionId"))

        def _parse_copy_node_id(data: object) -> None | Unset | UUID:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                copy_node_id_type_0 = UUID(data)

                return copy_node_id_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | Unset | UUID, data)

        copy_node_id = _parse_copy_node_id(d.pop("copyNodeId", UNSET))

        resolve_result = cls(
            id=id,
            state=state,
            node_id=node_id,
            head_version_id=head_version_id,
            copy_node_id=copy_node_id,
        )

        resolve_result.additional_properties = d
        return resolve_result

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
