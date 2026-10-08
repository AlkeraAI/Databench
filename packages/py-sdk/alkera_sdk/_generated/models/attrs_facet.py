from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.attrs_facet_metadata import AttrsFacetMetadata
    from ..models.xattrs import Xattrs


T = TypeVar("T", bound="AttrsFacet")


@_attrs_define
class AttrsFacet:
    """POSIX-shaped attributes; `ctime` is server-owned and read-only.

    ``mtime_ns`` rides beside the RFC 3339 ``mtime`` because the two are not
    interchangeable: a float second cannot hold a nanosecond stamp — an IEEE
    double has run out of mantissa by roughly 256 ns at today's epoch — so a
    client that rebuilt nanoseconds from the rendered ``mtime`` wrote back a
    *different* time than the one it read, and every pulled file then looked
    modified to a tool that compares stat blocks. The integer is the exact
    value the tree stores; ``mtime`` stays for the readers that want a date.

        Attributes:
            schema_version (str | Unset):
            metadata (AttrsFacetMetadata | Unset):
            mode (int | Unset):  Default: 0.
            uid (int | Unset):  Default: 0.
            gid (int | Unset):  Default: 0.
            owner (None | str | Unset):
            mtime (datetime.datetime | None | Unset):
            mtime_ns (int | None | Unset):
            atime (datetime.datetime | None | Unset):
            birthtime (datetime.datetime | None | Unset):
            ctime (datetime.datetime | None | Unset):
            xattrs (Xattrs | Unset):
    """

    schema_version: str | Unset = UNSET
    metadata: AttrsFacetMetadata | Unset = UNSET
    mode: int | Unset = 0
    uid: int | Unset = 0
    gid: int | Unset = 0
    owner: None | str | Unset = UNSET
    mtime: datetime.datetime | None | Unset = UNSET
    mtime_ns: int | None | Unset = UNSET
    atime: datetime.datetime | None | Unset = UNSET
    birthtime: datetime.datetime | None | Unset = UNSET
    ctime: datetime.datetime | None | Unset = UNSET
    xattrs: Xattrs | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        schema_version = self.schema_version

        metadata: dict[str, Any] | Unset = UNSET
        if not isinstance(self.metadata, Unset):
            metadata = self.metadata.to_dict()

        mode = self.mode

        uid = self.uid

        gid = self.gid

        owner: None | str | Unset
        if isinstance(self.owner, Unset):
            owner = UNSET
        else:
            owner = self.owner

        mtime: None | str | Unset
        if isinstance(self.mtime, Unset):
            mtime = UNSET
        elif isinstance(self.mtime, datetime.datetime):
            mtime = self.mtime.isoformat()
        else:
            mtime = self.mtime

        mtime_ns: int | None | Unset
        if isinstance(self.mtime_ns, Unset):
            mtime_ns = UNSET
        else:
            mtime_ns = self.mtime_ns

        atime: None | str | Unset
        if isinstance(self.atime, Unset):
            atime = UNSET
        elif isinstance(self.atime, datetime.datetime):
            atime = self.atime.isoformat()
        else:
            atime = self.atime

        birthtime: None | str | Unset
        if isinstance(self.birthtime, Unset):
            birthtime = UNSET
        elif isinstance(self.birthtime, datetime.datetime):
            birthtime = self.birthtime.isoformat()
        else:
            birthtime = self.birthtime

        ctime: None | str | Unset
        if isinstance(self.ctime, Unset):
            ctime = UNSET
        elif isinstance(self.ctime, datetime.datetime):
            ctime = self.ctime.isoformat()
        else:
            ctime = self.ctime

        xattrs: dict[str, Any] | Unset = UNSET
        if not isinstance(self.xattrs, Unset):
            xattrs = self.xattrs.to_dict()

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if schema_version is not UNSET:
            field_dict["schema_version"] = schema_version
        if metadata is not UNSET:
            field_dict["metadata"] = metadata
        if mode is not UNSET:
            field_dict["mode"] = mode
        if uid is not UNSET:
            field_dict["uid"] = uid
        if gid is not UNSET:
            field_dict["gid"] = gid
        if owner is not UNSET:
            field_dict["owner"] = owner
        if mtime is not UNSET:
            field_dict["mtime"] = mtime
        if mtime_ns is not UNSET:
            field_dict["mtimeNs"] = mtime_ns
        if atime is not UNSET:
            field_dict["atime"] = atime
        if birthtime is not UNSET:
            field_dict["birthtime"] = birthtime
        if ctime is not UNSET:
            field_dict["ctime"] = ctime
        if xattrs is not UNSET:
            field_dict["xattrs"] = xattrs

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.attrs_facet_metadata import AttrsFacetMetadata
        from ..models.xattrs import Xattrs

        d = dict(src_dict)
        schema_version = d.pop("schema_version", UNSET)

        _metadata = d.pop("metadata", UNSET)
        metadata: AttrsFacetMetadata | Unset
        if isinstance(_metadata, Unset):
            metadata = UNSET
        else:
            metadata = AttrsFacetMetadata.from_dict(_metadata)

        mode = d.pop("mode", UNSET)

        uid = d.pop("uid", UNSET)

        gid = d.pop("gid", UNSET)

        def _parse_owner(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        owner = _parse_owner(d.pop("owner", UNSET))

        def _parse_mtime(data: object) -> datetime.datetime | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                mtime_type_0 = datetime.datetime.fromisoformat(data)

                return mtime_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None | Unset, data)

        mtime = _parse_mtime(d.pop("mtime", UNSET))

        def _parse_mtime_ns(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        mtime_ns = _parse_mtime_ns(d.pop("mtimeNs", UNSET))

        def _parse_atime(data: object) -> datetime.datetime | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                atime_type_0 = datetime.datetime.fromisoformat(data)

                return atime_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None | Unset, data)

        atime = _parse_atime(d.pop("atime", UNSET))

        def _parse_birthtime(data: object) -> datetime.datetime | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                birthtime_type_0 = datetime.datetime.fromisoformat(data)

                return birthtime_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None | Unset, data)

        birthtime = _parse_birthtime(d.pop("birthtime", UNSET))

        def _parse_ctime(data: object) -> datetime.datetime | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                ctime_type_0 = datetime.datetime.fromisoformat(data)

                return ctime_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None | Unset, data)

        ctime = _parse_ctime(d.pop("ctime", UNSET))

        _xattrs = d.pop("xattrs", UNSET)
        xattrs: Xattrs | Unset
        if isinstance(_xattrs, Unset):
            xattrs = UNSET
        else:
            xattrs = Xattrs.from_dict(_xattrs)

        attrs_facet = cls(
            schema_version=schema_version,
            metadata=metadata,
            mode=mode,
            uid=uid,
            gid=gid,
            owner=owner,
            mtime=mtime,
            mtime_ns=mtime_ns,
            atime=atime,
            birthtime=birthtime,
            ctime=ctime,
            xattrs=xattrs,
        )

        attrs_facet.additional_properties = d
        return attrs_facet

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
