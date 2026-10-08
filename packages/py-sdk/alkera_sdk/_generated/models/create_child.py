from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.create_child_conflictbehavior import CreateChildConflictbehavior
from ..models.create_child_kind import CreateChildKind
from ..models.create_child_symlink_kind_type_0 import CreateChildSymlinkKindType0
from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.create_child_file_type_0 import CreateChildFileType0


T = TypeVar("T", bound="CreateChild")


@_attrs_define
class CreateChild:
    """The `POST …/children` body: one folder, symlink or special node.

    Files are absent on purpose — bytes arrive through a content PUT or an
    upload session, so a `kind: "file"` here would be a node with no version
    that a listing would render as an empty file nobody wrote.

    A body that asks for one anyway is REFUSED rather than quietly rounded to
    the default. ``kind`` used to leave a caller's ``"file"`` out of the
    literal, and a Drive-shaped body naming a ``file`` facet was simply an
    unknown key: either way the caller asked for a file, got a 201 describing a
    folder of that name, and discovered it only when the content PUT that
    followed answered 404 as though the file had vanished.

        Attributes:
            name (str):
            kind (CreateChildKind | Unset):  Default: CreateChildKind.FOLDER.
            file (CreateChildFileType0 | None | Unset):
            subtype (None | str | Unset):
            symlink_target (None | str | Unset):
            symlink_kind (CreateChildSymlinkKindType0 | None | Unset):
            conflict_behavior (CreateChildConflictbehavior | Unset):  Default: CreateChildConflictbehavior.FAIL.
    """

    name: str
    kind: CreateChildKind | Unset = CreateChildKind.FOLDER
    file: CreateChildFileType0 | None | Unset = UNSET
    subtype: None | str | Unset = UNSET
    symlink_target: None | str | Unset = UNSET
    symlink_kind: CreateChildSymlinkKindType0 | None | Unset = UNSET
    conflict_behavior: CreateChildConflictbehavior | Unset = CreateChildConflictbehavior.FAIL
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.create_child_file_type_0 import CreateChildFileType0

        name = self.name

        kind: str | Unset = UNSET
        if not isinstance(self.kind, Unset):
            kind = self.kind.value

        file: dict[str, Any] | None | Unset
        if isinstance(self.file, Unset):
            file = UNSET
        elif isinstance(self.file, CreateChildFileType0):
            file = self.file.to_dict()
        else:
            file = self.file

        subtype: None | str | Unset
        if isinstance(self.subtype, Unset):
            subtype = UNSET
        else:
            subtype = self.subtype

        symlink_target: None | str | Unset
        if isinstance(self.symlink_target, Unset):
            symlink_target = UNSET
        else:
            symlink_target = self.symlink_target

        symlink_kind: None | str | Unset
        if isinstance(self.symlink_kind, Unset):
            symlink_kind = UNSET
        elif isinstance(self.symlink_kind, CreateChildSymlinkKindType0):
            symlink_kind = self.symlink_kind.value
        else:
            symlink_kind = self.symlink_kind

        conflict_behavior: str | Unset = UNSET
        if not isinstance(self.conflict_behavior, Unset):
            conflict_behavior = self.conflict_behavior.value

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "name": name,
            }
        )
        if kind is not UNSET:
            field_dict["kind"] = kind
        if file is not UNSET:
            field_dict["file"] = file
        if subtype is not UNSET:
            field_dict["subtype"] = subtype
        if symlink_target is not UNSET:
            field_dict["symlinkTarget"] = symlink_target
        if symlink_kind is not UNSET:
            field_dict["symlinkKind"] = symlink_kind
        if conflict_behavior is not UNSET:
            field_dict["conflictBehavior"] = conflict_behavior

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.create_child_file_type_0 import CreateChildFileType0

        d = dict(src_dict)
        name = d.pop("name")

        _kind = d.pop("kind", UNSET)
        kind: CreateChildKind | Unset
        if isinstance(_kind, Unset):
            kind = UNSET
        else:
            kind = CreateChildKind(_kind)

        def _parse_file(data: object) -> CreateChildFileType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                file_type_0 = CreateChildFileType0.from_dict(data)

                return file_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(CreateChildFileType0 | None | Unset, data)

        file = _parse_file(d.pop("file", UNSET))

        def _parse_subtype(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        subtype = _parse_subtype(d.pop("subtype", UNSET))

        def _parse_symlink_target(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        symlink_target = _parse_symlink_target(d.pop("symlinkTarget", UNSET))

        def _parse_symlink_kind(data: object) -> CreateChildSymlinkKindType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                symlink_kind_type_0 = CreateChildSymlinkKindType0(data)

                return symlink_kind_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(CreateChildSymlinkKindType0 | None | Unset, data)

        symlink_kind = _parse_symlink_kind(d.pop("symlinkKind", UNSET))

        _conflict_behavior = d.pop("conflictBehavior", UNSET)
        conflict_behavior: CreateChildConflictbehavior | Unset
        if isinstance(_conflict_behavior, Unset):
            conflict_behavior = UNSET
        else:
            conflict_behavior = CreateChildConflictbehavior(_conflict_behavior)

        create_child = cls(
            name=name,
            kind=kind,
            file=file,
            subtype=subtype,
            symlink_target=symlink_target,
            symlink_kind=symlink_kind,
            conflict_behavior=conflict_behavior,
        )

        create_child.additional_properties = d
        return create_child

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
