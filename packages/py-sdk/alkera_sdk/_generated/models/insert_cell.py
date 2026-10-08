from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Literal, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.config import Config
    from ..models.meta import Meta


T = TypeVar("T", bound="InsertCell")


@_attrs_define
class InsertCell:
    """
    Attributes:
        op (Literal['insert']):
        kind (str | Unset):  Default: 'python'.
        source (str | Unset):  Default: ''.
        name (str | Unset):  Default: '_'.
        after (None | str | Unset):
        before (None | str | Unset):
        config (Config | Unset):
        meta (Meta | Unset):
    """

    op: Literal["insert"]
    kind: str | Unset = "python"
    source: str | Unset = ""
    name: str | Unset = "_"
    after: None | str | Unset = UNSET
    before: None | str | Unset = UNSET
    config: Config | Unset = UNSET
    meta: Meta | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        op = self.op

        kind = self.kind

        source = self.source

        name = self.name

        after: None | str | Unset
        if isinstance(self.after, Unset):
            after = UNSET
        else:
            after = self.after

        before: None | str | Unset
        if isinstance(self.before, Unset):
            before = UNSET
        else:
            before = self.before

        config: dict[str, Any] | Unset = UNSET
        if not isinstance(self.config, Unset):
            config = self.config.to_dict()

        meta: dict[str, Any] | Unset = UNSET
        if not isinstance(self.meta, Unset):
            meta = self.meta.to_dict()

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "op": op,
            }
        )
        if kind is not UNSET:
            field_dict["kind"] = kind
        if source is not UNSET:
            field_dict["source"] = source
        if name is not UNSET:
            field_dict["name"] = name
        if after is not UNSET:
            field_dict["after"] = after
        if before is not UNSET:
            field_dict["before"] = before
        if config is not UNSET:
            field_dict["config"] = config
        if meta is not UNSET:
            field_dict["meta"] = meta

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.config import Config
        from ..models.meta import Meta

        d = dict(src_dict)
        op = cast(Literal["insert"], d.pop("op"))
        if op != "insert":
            raise ValueError(f"op must match const 'insert', got '{op}'")

        kind = d.pop("kind", UNSET)

        source = d.pop("source", UNSET)

        name = d.pop("name", UNSET)

        def _parse_after(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        after = _parse_after(d.pop("after", UNSET))

        def _parse_before(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        before = _parse_before(d.pop("before", UNSET))

        _config = d.pop("config", UNSET)
        config: Config | Unset
        if isinstance(_config, Unset):
            config = UNSET
        else:
            config = Config.from_dict(_config)

        _meta = d.pop("meta", UNSET)
        meta: Meta | Unset
        if isinstance(_meta, Unset):
            meta = UNSET
        else:
            meta = Meta.from_dict(_meta)

        insert_cell = cls(
            op=op,
            kind=kind,
            source=source,
            name=name,
            after=after,
            before=before,
            config=config,
            meta=meta,
        )

        return insert_cell
