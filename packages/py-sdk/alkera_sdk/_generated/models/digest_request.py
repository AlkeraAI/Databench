from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

T = TypeVar("T", bound="DigestRequest")


@_attrs_define
class DigestRequest:
    """The folders a holder's walk wants the drive's digests of.

    Relative to the leased folder, ``/``-separated, ``""`` for the folder
    itself; the same byte rules as a tree report's paths.

    ``names`` asks, for some of those folders, the names of their children as
    the drive files them: what a walk that found a folder differing needs to
    learn which of the drive's rows its disk no longer has. Each must also be
    one of ``paths``.

        Attributes:
            paths (list[str]):
            names (list[str] | Unset):
    """

    paths: list[str]
    names: list[str] | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        paths = self.paths

        names: list[str] | Unset = UNSET
        if not isinstance(self.names, Unset):
            names = self.names

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "paths": paths,
            }
        )
        if names is not UNSET:
            field_dict["names"] = names

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        paths = cast(list[str], d.pop("paths"))

        names = cast(list[str], d.pop("names", UNSET))

        digest_request = cls(
            paths=paths,
            names=names,
        )

        return digest_request
