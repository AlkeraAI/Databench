from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.content_grant_request_disposition import ContentGrantRequestDisposition
from ..models.content_grant_request_kind import ContentGrantRequestKind
from ..types import UNSET, Unset

T = TypeVar("T", bound="ContentGrantRequest")


@_attrs_define
class ContentGrantRequest:
    """What kind of read the caller is asking to be granted.

    ``file`` is the single-use download URL every client already mints through
    the ``302``; ``page`` is the multi-use grant over the entry's own folder
    that lets a rendered document fetch the images beside it. They are one
    route because they are one decision — ``EXPORT`` on the node the caller
    named — and a client that asks for the wrong one for a node's type is told
    so rather than quietly handed the other.

        Attributes:
            kind (ContentGrantRequestKind):
            disposition (ContentGrantRequestDisposition | Unset):  Default: ContentGrantRequestDisposition.INLINE.
    """

    kind: ContentGrantRequestKind
    disposition: ContentGrantRequestDisposition | Unset = ContentGrantRequestDisposition.INLINE
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        kind = self.kind.value

        disposition: str | Unset = UNSET
        if not isinstance(self.disposition, Unset):
            disposition = self.disposition.value

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "kind": kind,
            }
        )
        if disposition is not UNSET:
            field_dict["disposition"] = disposition

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        kind = ContentGrantRequestKind(d.pop("kind"))

        _disposition = d.pop("disposition", UNSET)
        disposition: ContentGrantRequestDisposition | Unset
        if isinstance(_disposition, Unset):
            disposition = UNSET
        else:
            disposition = ContentGrantRequestDisposition(_disposition)

        content_grant_request = cls(
            kind=kind,
            disposition=disposition,
        )

        content_grant_request.additional_properties = d
        return content_grant_request

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
