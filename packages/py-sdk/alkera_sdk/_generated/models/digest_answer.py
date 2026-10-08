from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.children import Children
    from ..models.digests import Digests


T = TypeVar("T", bound="DigestAnswer")


@_attrs_define
class DigestAnswer:
    """The drive's digest of every folder the request named, keyed by the path
    exactly as it was sent. A folder the drive does not have answers as an
    empty one: no children, a zero XOR.

    ``children`` answers ``names``: for each folder named there, the names of
    its direct files and folders, spelled like the paths (an undecodable byte
    as its surrogate escape). A child the drive is still waiting for the
    holder to take is left out, because a disk that lacks it has not deleted
    it. A folder the drive does not have lists none.

        Attributes:
            digests (Digests):
            children (Children | Unset):
    """

    digests: Digests
    children: Children | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        digests = self.digests.to_dict()

        children: dict[str, Any] | Unset = UNSET
        if not isinstance(self.children, Unset):
            children = self.children.to_dict()

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "digests": digests,
            }
        )
        if children is not UNSET:
            field_dict["children"] = children

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.children import Children
        from ..models.digests import Digests

        d = dict(src_dict)
        digests = Digests.from_dict(d.pop("digests"))

        _children = d.pop("children", UNSET)
        children: Children | Unset
        if isinstance(_children, Unset):
            children = UNSET
        else:
            children = Children.from_dict(_children)

        digest_answer = cls(
            digests=digests,
            children=children,
        )

        digest_answer.additional_properties = d
        return digest_answer

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
