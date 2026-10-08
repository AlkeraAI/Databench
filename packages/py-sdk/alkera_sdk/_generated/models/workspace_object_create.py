from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Literal, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.workspace_object_create_spec import WorkspaceObjectCreateSpec


T = TypeVar("T", bound="WorkspaceObjectCreate")


@_attrs_define
class WorkspaceObjectCreate:
    """
    Attributes:
        type_ (Literal['result']):
        title (str):
        client_id (str):
        spec (WorkspaceObjectCreateSpec | Unset):
        visibility_scope (None | str | Unset):
    """

    type_: Literal["result"]
    title: str
    client_id: str
    spec: WorkspaceObjectCreateSpec | Unset = UNSET
    visibility_scope: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        type_ = self.type_

        title = self.title

        client_id = self.client_id

        spec: dict[str, Any] | Unset = UNSET
        if not isinstance(self.spec, Unset):
            spec = self.spec.to_dict()

        visibility_scope: None | str | Unset
        if isinstance(self.visibility_scope, Unset):
            visibility_scope = UNSET
        else:
            visibility_scope = self.visibility_scope

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "type": type_,
                "title": title,
                "client_id": client_id,
            }
        )
        if spec is not UNSET:
            field_dict["spec"] = spec
        if visibility_scope is not UNSET:
            field_dict["visibility_scope"] = visibility_scope

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.workspace_object_create_spec import WorkspaceObjectCreateSpec

        d = dict(src_dict)
        type_ = cast(Literal["result"], d.pop("type"))
        if type_ != "result":
            raise ValueError(f"type must match const 'result', got '{type_}'")

        title = d.pop("title")

        client_id = d.pop("client_id")

        _spec = d.pop("spec", UNSET)
        spec: WorkspaceObjectCreateSpec | Unset
        if isinstance(_spec, Unset):
            spec = UNSET
        else:
            spec = WorkspaceObjectCreateSpec.from_dict(_spec)

        def _parse_visibility_scope(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        visibility_scope = _parse_visibility_scope(d.pop("visibility_scope", UNSET))

        workspace_object_create = cls(
            type_=type_,
            title=title,
            client_id=client_id,
            spec=spec,
            visibility_scope=visibility_scope,
        )

        workspace_object_create.additional_properties = d
        return workspace_object_create

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
