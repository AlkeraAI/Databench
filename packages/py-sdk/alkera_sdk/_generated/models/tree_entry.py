from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define

from ..models.tree_entry_kind_type_0 import TreeEntryKindType0
from ..models.tree_entry_op import TreeEntryOp
from ..types import UNSET, Unset

T = TypeVar("T", bound="TreeEntry")


@_attrs_define
class TreeEntry:
    """One change under the leased folder, as the holder saw it on disk.

    Attributes:
        op (TreeEntryOp):
        path (str):
        kind (None | TreeEntryKindType0 | Unset):
        from_ (None | str | Unset):
        size (int | None | Unset):
        mtime_ns (int | None | Unset):
        mode (int | None | Unset):
        hash_ (None | str | Unset):
    """

    op: TreeEntryOp
    path: str
    kind: None | TreeEntryKindType0 | Unset = UNSET
    from_: None | str | Unset = UNSET
    size: int | None | Unset = UNSET
    mtime_ns: int | None | Unset = UNSET
    mode: int | None | Unset = UNSET
    hash_: None | str | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        op = self.op.value

        path = self.path

        kind: None | str | Unset
        if isinstance(self.kind, Unset):
            kind = UNSET
        elif isinstance(self.kind, TreeEntryKindType0):
            kind = self.kind.value
        else:
            kind = self.kind

        from_: None | str | Unset
        if isinstance(self.from_, Unset):
            from_ = UNSET
        else:
            from_ = self.from_

        size: int | None | Unset
        if isinstance(self.size, Unset):
            size = UNSET
        else:
            size = self.size

        mtime_ns: int | None | Unset
        if isinstance(self.mtime_ns, Unset):
            mtime_ns = UNSET
        else:
            mtime_ns = self.mtime_ns

        mode: int | None | Unset
        if isinstance(self.mode, Unset):
            mode = UNSET
        else:
            mode = self.mode

        hash_: None | str | Unset
        if isinstance(self.hash_, Unset):
            hash_ = UNSET
        else:
            hash_ = self.hash_

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "op": op,
                "path": path,
            }
        )
        if kind is not UNSET:
            field_dict["kind"] = kind
        if from_ is not UNSET:
            field_dict["from"] = from_
        if size is not UNSET:
            field_dict["size"] = size
        if mtime_ns is not UNSET:
            field_dict["mtime_ns"] = mtime_ns
        if mode is not UNSET:
            field_dict["mode"] = mode
        if hash_ is not UNSET:
            field_dict["hash"] = hash_

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        op = TreeEntryOp(d.pop("op"))

        path = d.pop("path")

        def _parse_kind(data: object) -> None | TreeEntryKindType0 | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                kind_type_0 = TreeEntryKindType0(data)

                return kind_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | TreeEntryKindType0 | Unset, data)

        kind = _parse_kind(d.pop("kind", UNSET))

        def _parse_from_(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        from_ = _parse_from_(d.pop("from", UNSET))

        def _parse_size(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        size = _parse_size(d.pop("size", UNSET))

        def _parse_mtime_ns(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        mtime_ns = _parse_mtime_ns(d.pop("mtime_ns", UNSET))

        def _parse_mode(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        mode = _parse_mode(d.pop("mode", UNSET))

        def _parse_hash_(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        hash_ = _parse_hash_(d.pop("hash", UNSET))

        tree_entry = cls(
            op=op,
            path=path,
            kind=kind,
            from_=from_,
            size=size,
            mtime_ns=mtime_ns,
            mode=mode,
            hash_=hash_,
        )

        return tree_entry
