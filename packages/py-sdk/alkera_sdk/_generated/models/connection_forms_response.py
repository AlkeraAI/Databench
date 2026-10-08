from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.connector_form_descriptor import ConnectorFormDescriptor


T = TypeVar("T", bound="ConnectionFormsResponse")


@_attrs_define
class ConnectionFormsResponse:
    """
    Attributes:
        connectors (list[ConnectorFormDescriptor] | Unset):
        checked_before_save (bool | Unset):  Default: False.
    """

    connectors: list[ConnectorFormDescriptor] | Unset = UNSET
    checked_before_save: bool | Unset = False
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        connectors: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.connectors, Unset):
            connectors = []
            for connectors_item_data in self.connectors:
                connectors_item = connectors_item_data.to_dict()
                connectors.append(connectors_item)

        checked_before_save = self.checked_before_save

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if connectors is not UNSET:
            field_dict["connectors"] = connectors
        if checked_before_save is not UNSET:
            field_dict["checked_before_save"] = checked_before_save

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.connector_form_descriptor import ConnectorFormDescriptor

        d = dict(src_dict)
        _connectors = d.pop("connectors", UNSET)
        connectors: list[ConnectorFormDescriptor] | Unset = UNSET
        if _connectors is not UNSET:
            connectors = []
            for connectors_item_data in _connectors:
                connectors_item = ConnectorFormDescriptor.from_dict(connectors_item_data)

                connectors.append(connectors_item)

        checked_before_save = d.pop("checked_before_save", UNSET)

        connection_forms_response = cls(
            connectors=connectors,
            checked_before_save=checked_before_save,
        )

        connection_forms_response.additional_properties = d
        return connection_forms_response

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
