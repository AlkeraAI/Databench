from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.notebook_connection import NotebookConnection


T = TypeVar("T", bound="NotebookConnections")


@_attrs_define
class NotebookConnections:
    """``GET .../connections``: what the notebook's SQL cells may name. Empty
    for a notebook in no workspace, whose kernel resolves no connection.

        Attributes:
            connections (list[NotebookConnection] | Unset):
    """

    connections: list[NotebookConnection] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        connections: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.connections, Unset):
            connections = []
            for connections_item_data in self.connections:
                connections_item = connections_item_data.to_dict()
                connections.append(connections_item)

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if connections is not UNSET:
            field_dict["connections"] = connections

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.notebook_connection import NotebookConnection

        d = dict(src_dict)
        _connections = d.pop("connections", UNSET)
        connections: list[NotebookConnection] | Unset = UNSET
        if _connections is not UNSET:
            connections = []
            for connections_item_data in _connections:
                connections_item = NotebookConnection.from_dict(connections_item_data)

                connections.append(connections_item)

        notebook_connections = cls(
            connections=connections,
        )

        notebook_connections.additional_properties = d
        return notebook_connections

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
