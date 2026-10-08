from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar
from uuid import UUID

from attrs import define as _attrs_define
from attrs import field as _attrs_field

T = TypeVar("T", bound="PrincipalRef")


@_attrs_define
class PrincipalRef:
    """Who a grant is for: a ``user``, a ``team`` or the ``org`` itself.

    The kind is an open registry, so a kind the server has not registered is a
    422 rather than a row nothing can resolve. An ``org`` grant names the
    caller's own org; any other id is a 422 too. A ``user`` or ``team`` id that
    is not a principal of the caller's org (another org's, a stranger, an id
    never issued) is a 404, the same answer for every one of them, so the route
    never confirms who exists outside the org.

        Attributes:
            kind (str):
            id (UUID):
    """

    kind: str
    id: UUID
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        kind = self.kind

        id = str(self.id)

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "kind": kind,
                "id": id,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        kind = d.pop("kind")

        id = UUID(d.pop("id"))

        principal_ref = cls(
            kind=kind,
            id=id,
        )

        principal_ref.additional_properties = d
        return principal_ref

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
