"""The notebook document as plain data.

Mirrors the Loro notebook document: meta (format, generated_with, header
text, app config), settings (known keys plus raw TOML lines of unknown keys),
an order of live cell ids, and cells keyed by id (soft deleted cells stay
until purged). No run state, outputs or carets live here.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

from alkera_notebook.format.settings import NOTEBOOK_SETTINGS

KNOWN_SETTINGS: tuple[str, ...] = ("format", *(s.name for s in NOTEBOOK_SETTINGS))

SETTING_DEFAULTS: dict[str, Any] = {
    "format": "1.0",
    **{s.name: s.default for s in NOTEBOOK_SETTINGS},
}


@dataclass
class DocCell:
    id: str
    kind: str
    name: str
    source: str
    code: str
    config: dict[str, Any] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)
    deleted: bool = False
    # Where a soft-deleted cell was: the live cell before it when it was
    # deleted (None when it was first). Restore and anchors use it.
    deleted_after: str | None = None

    @property
    def disabled(self) -> bool:
        return bool(self.config.get("disabled", False))


@dataclass
class DocMeta:
    format: str = "1.0"
    generated_with: str = ""
    header: str = ""
    app: dict[str, Any] = field(default_factory=dict)


@dataclass
class Document:
    meta: DocMeta = field(default_factory=DocMeta)
    settings: dict[str, Any] = field(default_factory=dict)
    unknown_settings: str = ""
    order: list[str] = field(default_factory=list)
    cells: dict[str, DocCell] = field(default_factory=dict)
    read_only_reason: str | None = None

    def __post_init__(self) -> None:
        # A document holds what its file sets. A settings read (``sources``)
        # or any other key is never one of them: unknown keys live in
        # ``unknown_settings`` as the file's own text.
        stray = sorted(set(self.settings) - set(KNOWN_SETTINGS))
        if stray:
            raise ValueError(f"not notebook settings: {', '.join(stray)}")

    def live_cells(self) -> list[DocCell]:
        return [self.cells[i] for i in self.order if i in self.cells and not self.cells[i].deleted]

    def index_of(self, cell_id: str) -> int:
        return self.order.index(cell_id)

    def setting(self, key: str) -> Any:
        return self.settings.get(key, SETTING_DEFAULTS.get(key))

    def clone(self) -> Document:
        return copy.deepcopy(self)
