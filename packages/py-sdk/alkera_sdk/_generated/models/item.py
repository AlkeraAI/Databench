from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.item_kind import ItemKind
from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.attrs_facet import AttrsFacet
    from ..models.capabilities import Capabilities
    from ..models.file_facet import FileFacet
    from ..models.home_facet import HomeFacet
    from ..models.lease_facet import LeaseFacet
    from ..models.live_facet import LiveFacet
    from ..models.name_flags_wire import NameFlagsWire
    from ..models.object_facet import ObjectFacet
    from ..models.special_facet import SpecialFacet
    from ..models.symlink_facet import SymlinkFacet


T = TypeVar("T", bound="Item")


@_attrs_define
class Item:
    """The one item payload every Files surface returns.

    Attributes:
        id (str):
        ino (int | Unset):  Default: 0.
        drive_id (str | Unset):  Default: ''.
        kind (ItemKind | Unset):  Default: ItemKind.FILE.
        subtype (None | str | Unset):
        name (str | Unset):  Default: ''.
        name_display (str | Unset):  Default: ''.
        name_encoding (str | Unset):  Default: 'utf-8'.
        name_flags (NameFlagsWire | Unset): What the server knows about how this name behaves on a client's filesystem.
        path_bytes (str | Unset):  Default: ''.
        parent_id (None | str | Unset):
        path (None | str | Unset):
        etag (str | Unset):  Default: ''.
        ctag (str | Unset):  Default: ''.
        attrs (AttrsFacet | None | Unset):
        file (FileFacet | None | Unset):
        symlink (None | SymlinkFacet | Unset):
        object_ (None | ObjectFacet | Unset):
        special (None | SpecialFacet | Unset):
        home (HomeFacet | None | Unset):
        path_home (HomeFacet | None | Unset):
        lease (LeaseFacet | None | Unset):
        live (LiveFacet | None | Unset):
        stale (bool | Unset):  Default: False.
        trust (None | str | Unset):
        locked (bool | Unset):  Default: False.
        held (bool | Unset):  Default: False.
        capabilities (Capabilities | Unset): What this caller may do with this node, and why not when they may not.

            `refusals` is surface-specific: the same node can be undownloadable on a
            link and downloadable in the portal, and the UI shows the reason rather
            than a dead button.
        owner_name (None | str | Unset):
        parent_name (None | str | Unset):
        shared (bool | Unset):  Default: False.
        starred (bool | Unset):  Default: False.
        trashed (bool | Unset):  Default: False.
        conflict_of (None | str | Unset):
    """

    id: str
    ino: int | Unset = 0
    drive_id: str | Unset = ""
    kind: ItemKind | Unset = ItemKind.FILE
    subtype: None | str | Unset = UNSET
    name: str | Unset = ""
    name_display: str | Unset = ""
    name_encoding: str | Unset = "utf-8"
    name_flags: NameFlagsWire | Unset = UNSET
    path_bytes: str | Unset = ""
    parent_id: None | str | Unset = UNSET
    path: None | str | Unset = UNSET
    etag: str | Unset = ""
    ctag: str | Unset = ""
    attrs: AttrsFacet | None | Unset = UNSET
    file: FileFacet | None | Unset = UNSET
    symlink: None | SymlinkFacet | Unset = UNSET
    object_: None | ObjectFacet | Unset = UNSET
    special: None | SpecialFacet | Unset = UNSET
    home: HomeFacet | None | Unset = UNSET
    path_home: HomeFacet | None | Unset = UNSET
    lease: LeaseFacet | None | Unset = UNSET
    live: LiveFacet | None | Unset = UNSET
    stale: bool | Unset = False
    trust: None | str | Unset = UNSET
    locked: bool | Unset = False
    held: bool | Unset = False
    capabilities: Capabilities | Unset = UNSET
    owner_name: None | str | Unset = UNSET
    parent_name: None | str | Unset = UNSET
    shared: bool | Unset = False
    starred: bool | Unset = False
    trashed: bool | Unset = False
    conflict_of: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.attrs_facet import AttrsFacet
        from ..models.file_facet import FileFacet
        from ..models.home_facet import HomeFacet
        from ..models.lease_facet import LeaseFacet
        from ..models.live_facet import LiveFacet
        from ..models.object_facet import ObjectFacet
        from ..models.special_facet import SpecialFacet
        from ..models.symlink_facet import SymlinkFacet

        id = self.id

        ino = self.ino

        drive_id = self.drive_id

        kind: str | Unset = UNSET
        if not isinstance(self.kind, Unset):
            kind = self.kind.value

        subtype: None | str | Unset
        if isinstance(self.subtype, Unset):
            subtype = UNSET
        else:
            subtype = self.subtype

        name = self.name

        name_display = self.name_display

        name_encoding = self.name_encoding

        name_flags: dict[str, Any] | Unset = UNSET
        if not isinstance(self.name_flags, Unset):
            name_flags = self.name_flags.to_dict()

        path_bytes = self.path_bytes

        parent_id: None | str | Unset
        if isinstance(self.parent_id, Unset):
            parent_id = UNSET
        else:
            parent_id = self.parent_id

        path: None | str | Unset
        if isinstance(self.path, Unset):
            path = UNSET
        else:
            path = self.path

        etag = self.etag

        ctag = self.ctag

        attrs: dict[str, Any] | None | Unset
        if isinstance(self.attrs, Unset):
            attrs = UNSET
        elif isinstance(self.attrs, AttrsFacet):
            attrs = self.attrs.to_dict()
        else:
            attrs = self.attrs

        file: dict[str, Any] | None | Unset
        if isinstance(self.file, Unset):
            file = UNSET
        elif isinstance(self.file, FileFacet):
            file = self.file.to_dict()
        else:
            file = self.file

        symlink: dict[str, Any] | None | Unset
        if isinstance(self.symlink, Unset):
            symlink = UNSET
        elif isinstance(self.symlink, SymlinkFacet):
            symlink = self.symlink.to_dict()
        else:
            symlink = self.symlink

        object_: dict[str, Any] | None | Unset
        if isinstance(self.object_, Unset):
            object_ = UNSET
        elif isinstance(self.object_, ObjectFacet):
            object_ = self.object_.to_dict()
        else:
            object_ = self.object_

        special: dict[str, Any] | None | Unset
        if isinstance(self.special, Unset):
            special = UNSET
        elif isinstance(self.special, SpecialFacet):
            special = self.special.to_dict()
        else:
            special = self.special

        home: dict[str, Any] | None | Unset
        if isinstance(self.home, Unset):
            home = UNSET
        elif isinstance(self.home, HomeFacet):
            home = self.home.to_dict()
        else:
            home = self.home

        path_home: dict[str, Any] | None | Unset
        if isinstance(self.path_home, Unset):
            path_home = UNSET
        elif isinstance(self.path_home, HomeFacet):
            path_home = self.path_home.to_dict()
        else:
            path_home = self.path_home

        lease: dict[str, Any] | None | Unset
        if isinstance(self.lease, Unset):
            lease = UNSET
        elif isinstance(self.lease, LeaseFacet):
            lease = self.lease.to_dict()
        else:
            lease = self.lease

        live: dict[str, Any] | None | Unset
        if isinstance(self.live, Unset):
            live = UNSET
        elif isinstance(self.live, LiveFacet):
            live = self.live.to_dict()
        else:
            live = self.live

        stale = self.stale

        trust: None | str | Unset
        if isinstance(self.trust, Unset):
            trust = UNSET
        else:
            trust = self.trust

        locked = self.locked

        held = self.held

        capabilities: dict[str, Any] | Unset = UNSET
        if not isinstance(self.capabilities, Unset):
            capabilities = self.capabilities.to_dict()

        owner_name: None | str | Unset
        if isinstance(self.owner_name, Unset):
            owner_name = UNSET
        else:
            owner_name = self.owner_name

        parent_name: None | str | Unset
        if isinstance(self.parent_name, Unset):
            parent_name = UNSET
        else:
            parent_name = self.parent_name

        shared = self.shared

        starred = self.starred

        trashed = self.trashed

        conflict_of: None | str | Unset
        if isinstance(self.conflict_of, Unset):
            conflict_of = UNSET
        else:
            conflict_of = self.conflict_of

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
            }
        )
        if ino is not UNSET:
            field_dict["ino"] = ino
        if drive_id is not UNSET:
            field_dict["driveId"] = drive_id
        if kind is not UNSET:
            field_dict["kind"] = kind
        if subtype is not UNSET:
            field_dict["subtype"] = subtype
        if name is not UNSET:
            field_dict["name"] = name
        if name_display is not UNSET:
            field_dict["nameDisplay"] = name_display
        if name_encoding is not UNSET:
            field_dict["nameEncoding"] = name_encoding
        if name_flags is not UNSET:
            field_dict["nameFlags"] = name_flags
        if path_bytes is not UNSET:
            field_dict["pathBytes"] = path_bytes
        if parent_id is not UNSET:
            field_dict["parentId"] = parent_id
        if path is not UNSET:
            field_dict["path"] = path
        if etag is not UNSET:
            field_dict["etag"] = etag
        if ctag is not UNSET:
            field_dict["ctag"] = ctag
        if attrs is not UNSET:
            field_dict["attrs"] = attrs
        if file is not UNSET:
            field_dict["file"] = file
        if symlink is not UNSET:
            field_dict["symlink"] = symlink
        if object_ is not UNSET:
            field_dict["object"] = object_
        if special is not UNSET:
            field_dict["special"] = special
        if home is not UNSET:
            field_dict["home"] = home
        if path_home is not UNSET:
            field_dict["pathHome"] = path_home
        if lease is not UNSET:
            field_dict["lease"] = lease
        if live is not UNSET:
            field_dict["live"] = live
        if stale is not UNSET:
            field_dict["stale"] = stale
        if trust is not UNSET:
            field_dict["trust"] = trust
        if locked is not UNSET:
            field_dict["locked"] = locked
        if held is not UNSET:
            field_dict["held"] = held
        if capabilities is not UNSET:
            field_dict["capabilities"] = capabilities
        if owner_name is not UNSET:
            field_dict["ownerName"] = owner_name
        if parent_name is not UNSET:
            field_dict["parentName"] = parent_name
        if shared is not UNSET:
            field_dict["shared"] = shared
        if starred is not UNSET:
            field_dict["starred"] = starred
        if trashed is not UNSET:
            field_dict["trashed"] = trashed
        if conflict_of is not UNSET:
            field_dict["conflictOf"] = conflict_of

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.attrs_facet import AttrsFacet
        from ..models.capabilities import Capabilities
        from ..models.file_facet import FileFacet
        from ..models.home_facet import HomeFacet
        from ..models.lease_facet import LeaseFacet
        from ..models.live_facet import LiveFacet
        from ..models.name_flags_wire import NameFlagsWire
        from ..models.object_facet import ObjectFacet
        from ..models.special_facet import SpecialFacet
        from ..models.symlink_facet import SymlinkFacet

        d = dict(src_dict)
        id = d.pop("id")

        ino = d.pop("ino", UNSET)

        drive_id = d.pop("driveId", UNSET)

        _kind = d.pop("kind", UNSET)
        kind: ItemKind | Unset
        if isinstance(_kind, Unset):
            kind = UNSET
        else:
            kind = ItemKind(_kind)

        def _parse_subtype(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        subtype = _parse_subtype(d.pop("subtype", UNSET))

        name = d.pop("name", UNSET)

        name_display = d.pop("nameDisplay", UNSET)

        name_encoding = d.pop("nameEncoding", UNSET)

        _name_flags = d.pop("nameFlags", UNSET)
        name_flags: NameFlagsWire | Unset
        if isinstance(_name_flags, Unset):
            name_flags = UNSET
        else:
            name_flags = NameFlagsWire.from_dict(_name_flags)

        path_bytes = d.pop("pathBytes", UNSET)

        def _parse_parent_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        parent_id = _parse_parent_id(d.pop("parentId", UNSET))

        def _parse_path(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        path = _parse_path(d.pop("path", UNSET))

        etag = d.pop("etag", UNSET)

        ctag = d.pop("ctag", UNSET)

        def _parse_attrs(data: object) -> AttrsFacet | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                attrs_type_0 = AttrsFacet.from_dict(data)

                return attrs_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(AttrsFacet | None | Unset, data)

        attrs = _parse_attrs(d.pop("attrs", UNSET))

        def _parse_file(data: object) -> FileFacet | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                file_type_0 = FileFacet.from_dict(data)

                return file_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(FileFacet | None | Unset, data)

        file = _parse_file(d.pop("file", UNSET))

        def _parse_symlink(data: object) -> None | SymlinkFacet | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                symlink_type_0 = SymlinkFacet.from_dict(data)

                return symlink_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | SymlinkFacet | Unset, data)

        symlink = _parse_symlink(d.pop("symlink", UNSET))

        def _parse_object_(data: object) -> None | ObjectFacet | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                object_type_0 = ObjectFacet.from_dict(data)

                return object_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | ObjectFacet | Unset, data)

        object_ = _parse_object_(d.pop("object", UNSET))

        def _parse_special(data: object) -> None | SpecialFacet | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                special_type_0 = SpecialFacet.from_dict(data)

                return special_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | SpecialFacet | Unset, data)

        special = _parse_special(d.pop("special", UNSET))

        def _parse_home(data: object) -> HomeFacet | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                home_type_0 = HomeFacet.from_dict(data)

                return home_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(HomeFacet | None | Unset, data)

        home = _parse_home(d.pop("home", UNSET))

        def _parse_path_home(data: object) -> HomeFacet | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                path_home_type_0 = HomeFacet.from_dict(data)

                return path_home_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(HomeFacet | None | Unset, data)

        path_home = _parse_path_home(d.pop("pathHome", UNSET))

        def _parse_lease(data: object) -> LeaseFacet | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                lease_type_0 = LeaseFacet.from_dict(data)

                return lease_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(LeaseFacet | None | Unset, data)

        lease = _parse_lease(d.pop("lease", UNSET))

        def _parse_live(data: object) -> LiveFacet | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                live_type_0 = LiveFacet.from_dict(data)

                return live_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(LiveFacet | None | Unset, data)

        live = _parse_live(d.pop("live", UNSET))

        stale = d.pop("stale", UNSET)

        def _parse_trust(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        trust = _parse_trust(d.pop("trust", UNSET))

        locked = d.pop("locked", UNSET)

        held = d.pop("held", UNSET)

        _capabilities = d.pop("capabilities", UNSET)
        capabilities: Capabilities | Unset
        if isinstance(_capabilities, Unset):
            capabilities = UNSET
        else:
            capabilities = Capabilities.from_dict(_capabilities)

        def _parse_owner_name(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        owner_name = _parse_owner_name(d.pop("ownerName", UNSET))

        def _parse_parent_name(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        parent_name = _parse_parent_name(d.pop("parentName", UNSET))

        shared = d.pop("shared", UNSET)

        starred = d.pop("starred", UNSET)

        trashed = d.pop("trashed", UNSET)

        def _parse_conflict_of(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        conflict_of = _parse_conflict_of(d.pop("conflictOf", UNSET))

        item = cls(
            id=id,
            ino=ino,
            drive_id=drive_id,
            kind=kind,
            subtype=subtype,
            name=name,
            name_display=name_display,
            name_encoding=name_encoding,
            name_flags=name_flags,
            path_bytes=path_bytes,
            parent_id=parent_id,
            path=path,
            etag=etag,
            ctag=ctag,
            attrs=attrs,
            file=file,
            symlink=symlink,
            object_=object_,
            special=special,
            home=home,
            path_home=path_home,
            lease=lease,
            live=live,
            stale=stale,
            trust=trust,
            locked=locked,
            held=held,
            capabilities=capabilities,
            owner_name=owner_name,
            parent_name=parent_name,
            shared=shared,
            starred=starred,
            trashed=trashed,
            conflict_of=conflict_of,
        )

        item.additional_properties = d
        return item

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
