from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.attrs_patch_xattrs_type_0 import AttrsPatchXattrsType0


T = TypeVar("T", bound="AttrsPatch")


@_attrs_define
class AttrsPatch:
    """The in-flight ``PATCH`` body. ``extra="forbid"`` is the refusal of ``ctime``.

    Every field is spelled as the ``attrs`` facet spells it on the way out
    (camelCase over the wire, snake_case for Python callers), so what a client
    reads is what it may send back. ``mtime``/``atime``/``birthtime`` are the
    datetimes the facet rendered; the ``…Ns`` spellings stay accepted for the
    callers that hold raw nanoseconds.

        Attributes:
            mode (int | None | Unset):
            uid (int | None | Unset):
            gid (int | None | Unset):
            owner (None | str | Unset):
            atime (int | None | Unset):
            mtime (int | None | Unset):
            birthtime (int | None | Unset):
            nlink (int | None | Unset):
            rdev (int | None | Unset):
            xattrs (AttrsPatchXattrsType0 | None | Unset):
    """

    mode: int | None | Unset = UNSET
    uid: int | None | Unset = UNSET
    gid: int | None | Unset = UNSET
    owner: None | str | Unset = UNSET
    atime: int | None | Unset = UNSET
    mtime: int | None | Unset = UNSET
    birthtime: int | None | Unset = UNSET
    nlink: int | None | Unset = UNSET
    rdev: int | None | Unset = UNSET
    xattrs: AttrsPatchXattrsType0 | None | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        from ..models.attrs_patch_xattrs_type_0 import AttrsPatchXattrsType0

        mode: int | None | Unset
        if isinstance(self.mode, Unset):
            mode = UNSET
        else:
            mode = self.mode

        uid: int | None | Unset
        if isinstance(self.uid, Unset):
            uid = UNSET
        else:
            uid = self.uid

        gid: int | None | Unset
        if isinstance(self.gid, Unset):
            gid = UNSET
        else:
            gid = self.gid

        owner: None | str | Unset
        if isinstance(self.owner, Unset):
            owner = UNSET
        else:
            owner = self.owner

        atime: int | None | Unset
        if isinstance(self.atime, Unset):
            atime = UNSET
        else:
            atime = self.atime

        mtime: int | None | Unset
        if isinstance(self.mtime, Unset):
            mtime = UNSET
        else:
            mtime = self.mtime

        birthtime: int | None | Unset
        if isinstance(self.birthtime, Unset):
            birthtime = UNSET
        else:
            birthtime = self.birthtime

        nlink: int | None | Unset
        if isinstance(self.nlink, Unset):
            nlink = UNSET
        else:
            nlink = self.nlink

        rdev: int | None | Unset
        if isinstance(self.rdev, Unset):
            rdev = UNSET
        else:
            rdev = self.rdev

        xattrs: dict[str, Any] | None | Unset
        if isinstance(self.xattrs, Unset):
            xattrs = UNSET
        elif isinstance(self.xattrs, AttrsPatchXattrsType0):
            xattrs = self.xattrs.to_dict()
        else:
            xattrs = self.xattrs

        field_dict: dict[str, Any] = {}

        field_dict.update({})
        if mode is not UNSET:
            field_dict["mode"] = mode
        if uid is not UNSET:
            field_dict["uid"] = uid
        if gid is not UNSET:
            field_dict["gid"] = gid
        if owner is not UNSET:
            field_dict["owner"] = owner
        if atime is not UNSET:
            field_dict["atime"] = atime
        if mtime is not UNSET:
            field_dict["mtime"] = mtime
        if birthtime is not UNSET:
            field_dict["birthtime"] = birthtime
        if nlink is not UNSET:
            field_dict["nlink"] = nlink
        if rdev is not UNSET:
            field_dict["rdev"] = rdev
        if xattrs is not UNSET:
            field_dict["xattrs"] = xattrs

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.attrs_patch_xattrs_type_0 import AttrsPatchXattrsType0

        d = dict(src_dict)

        def _parse_mode(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        mode = _parse_mode(d.pop("mode", UNSET))

        def _parse_uid(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        uid = _parse_uid(d.pop("uid", UNSET))

        def _parse_gid(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        gid = _parse_gid(d.pop("gid", UNSET))

        def _parse_owner(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        owner = _parse_owner(d.pop("owner", UNSET))

        def _parse_atime(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        atime = _parse_atime(d.pop("atime", UNSET))

        def _parse_mtime(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        mtime = _parse_mtime(d.pop("mtime", UNSET))

        def _parse_birthtime(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        birthtime = _parse_birthtime(d.pop("birthtime", UNSET))

        def _parse_nlink(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        nlink = _parse_nlink(d.pop("nlink", UNSET))

        def _parse_rdev(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        rdev = _parse_rdev(d.pop("rdev", UNSET))

        def _parse_xattrs(data: object) -> AttrsPatchXattrsType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                xattrs_type_0 = AttrsPatchXattrsType0.from_dict(data)

                return xattrs_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(AttrsPatchXattrsType0 | None | Unset, data)

        xattrs = _parse_xattrs(d.pop("xattrs", UNSET))

        attrs_patch = cls(
            mode=mode,
            uid=uid,
            gid=gid,
            owner=owner,
            atime=atime,
            mtime=mtime,
            birthtime=birthtime,
            nlink=nlink,
            rdev=rdev,
            xattrs=xattrs,
        )

        return attrs_patch
