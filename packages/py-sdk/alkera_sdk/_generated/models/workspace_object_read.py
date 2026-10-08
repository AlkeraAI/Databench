from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast
from uuid import UUID

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.workspace_object_read_type import WorkspaceObjectReadType
from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.workspace_object_spec import WorkspaceObjectSpec


T = TypeVar("T", bound="WorkspaceObjectRead")


@_attrs_define
class WorkspaceObjectRead:
    """
    Attributes:
        id (UUID):
        logical_id (str):
        namespace (str):
        type_ (WorkspaceObjectReadType):
        title (str):
        version (int):
        status (str):
        spec (WorkspaceObjectSpec):
        owner_user_id (UUID):
        visibility_scope (str):
        created_at (datetime.datetime):
        updated_at (datetime.datetime):
        content_updated_at (float):
        web_url (None | str | Unset):
    """

    id: UUID
    logical_id: str
    namespace: str
    type_: WorkspaceObjectReadType
    title: str
    version: int
    status: str
    spec: WorkspaceObjectSpec
    owner_user_id: UUID
    visibility_scope: str
    created_at: datetime.datetime
    updated_at: datetime.datetime
    content_updated_at: float
    web_url: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        id = str(self.id)

        logical_id = self.logical_id

        namespace = self.namespace

        type_ = self.type_.value

        title = self.title

        version = self.version

        status = self.status

        spec = self.spec.to_dict()

        owner_user_id = str(self.owner_user_id)

        visibility_scope = self.visibility_scope

        created_at = self.created_at.isoformat()

        updated_at = self.updated_at.isoformat()

        content_updated_at = self.content_updated_at

        web_url: None | str | Unset
        if isinstance(self.web_url, Unset):
            web_url = UNSET
        else:
            web_url = self.web_url

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "logical_id": logical_id,
                "namespace": namespace,
                "type": type_,
                "title": title,
                "version": version,
                "status": status,
                "spec": spec,
                "owner_user_id": owner_user_id,
                "visibility_scope": visibility_scope,
                "created_at": created_at,
                "updated_at": updated_at,
                "content_updated_at": content_updated_at,
            }
        )
        if web_url is not UNSET:
            field_dict["web_url"] = web_url

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.workspace_object_spec import WorkspaceObjectSpec

        d = dict(src_dict)
        id = UUID(d.pop("id"))

        logical_id = d.pop("logical_id")

        namespace = d.pop("namespace")

        type_ = WorkspaceObjectReadType(d.pop("type"))

        title = d.pop("title")

        version = d.pop("version")

        status = d.pop("status")

        spec = WorkspaceObjectSpec.from_dict(d.pop("spec"))

        owner_user_id = UUID(d.pop("owner_user_id"))

        visibility_scope = d.pop("visibility_scope")

        created_at = datetime.datetime.fromisoformat(d.pop("created_at"))

        updated_at = datetime.datetime.fromisoformat(d.pop("updated_at"))

        content_updated_at = d.pop("content_updated_at")

        def _parse_web_url(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        web_url = _parse_web_url(d.pop("web_url", UNSET))

        workspace_object_read = cls(
            id=id,
            logical_id=logical_id,
            namespace=namespace,
            type_=type_,
            title=title,
            version=version,
            status=status,
            spec=spec,
            owner_user_id=owner_user_id,
            visibility_scope=visibility_scope,
            created_at=created_at,
            updated_at=updated_at,
            content_updated_at=content_updated_at,
            web_url=web_url,
        )

        workspace_object_read.additional_properties = d
        return workspace_object_read

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
