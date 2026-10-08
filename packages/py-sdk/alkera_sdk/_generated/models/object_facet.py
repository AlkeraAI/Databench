from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.object_facet_metadata import ObjectFacetMetadata


T = TypeVar("T", bound="ObjectFacet")


@_attrs_define
class ObjectFacet:
    """A row-backed object (a chat, a query, a board) rendered as a node.

    ``title`` is the object's own CURRENT title, which is not the node's name.
    The name was minted from the title the day the node was created and is a
    filesystem name ever since — an untitled chat is a uuid, and renaming the
    chat afterwards does not rename the folder. A surface that renders the node
    name therefore shows ``3952c9e2-….alkerachat`` for a conversation the person
    has been calling something else all morning. Empty when the object has no
    title (or, on a record written before 1.1.0, when nobody asked).

        Attributes:
            schema_version (str | Unset):
            metadata (ObjectFacetMetadata | Unset):
            type_ (str | Unset):  Default: ''.
            id (str | Unset):  Default: ''.
            title (str | Unset):  Default: ''.
            web_url (None | str | Unset):
            app_url (None | str | Unset):
    """

    schema_version: str | Unset = UNSET
    metadata: ObjectFacetMetadata | Unset = UNSET
    type_: str | Unset = ""
    id: str | Unset = ""
    title: str | Unset = ""
    web_url: None | str | Unset = UNSET
    app_url: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        schema_version = self.schema_version

        metadata: dict[str, Any] | Unset = UNSET
        if not isinstance(self.metadata, Unset):
            metadata = self.metadata.to_dict()

        type_ = self.type_

        id = self.id

        title = self.title

        web_url: None | str | Unset
        if isinstance(self.web_url, Unset):
            web_url = UNSET
        else:
            web_url = self.web_url

        app_url: None | str | Unset
        if isinstance(self.app_url, Unset):
            app_url = UNSET
        else:
            app_url = self.app_url

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if schema_version is not UNSET:
            field_dict["schema_version"] = schema_version
        if metadata is not UNSET:
            field_dict["metadata"] = metadata
        if type_ is not UNSET:
            field_dict["type"] = type_
        if id is not UNSET:
            field_dict["id"] = id
        if title is not UNSET:
            field_dict["title"] = title
        if web_url is not UNSET:
            field_dict["web_url"] = web_url
        if app_url is not UNSET:
            field_dict["app_url"] = app_url

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.object_facet_metadata import ObjectFacetMetadata

        d = dict(src_dict)
        schema_version = d.pop("schema_version", UNSET)

        _metadata = d.pop("metadata", UNSET)
        metadata: ObjectFacetMetadata | Unset
        if isinstance(_metadata, Unset):
            metadata = UNSET
        else:
            metadata = ObjectFacetMetadata.from_dict(_metadata)

        type_ = d.pop("type", UNSET)

        id = d.pop("id", UNSET)

        title = d.pop("title", UNSET)

        def _parse_web_url(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        web_url = _parse_web_url(d.pop("web_url", UNSET))

        def _parse_app_url(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        app_url = _parse_app_url(d.pop("app_url", UNSET))

        object_facet = cls(
            schema_version=schema_version,
            metadata=metadata,
            type_=type_,
            id=id,
            title=title,
            web_url=web_url,
            app_url=app_url,
        )

        object_facet.additional_properties = d
        return object_facet

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
