"""A pure model of a notebook document and its operations.

The simulator applies every operation the store acknowledged to this model as
well, and checks after each step that the store's document equals it: same
live cells in the same order, same ids, kinds, names and sources. The model is
written from the operation rules alone (atomic batches, soft delete and
restore, setup first, the error codes), independently of any engine.
"""

from __future__ import annotations

import keyword
import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from alkera_notebook.format.settings import BY_NAME
from alkera_notebook.tools.models import (
    DeleteCellOp,
    EditCellOp,
    InsertCellOp,
    MoveCellOp,
    NotebookOp,
    RenameCellOp,
    ReplaceCellOp,
    RestoreCellOp,
    SetCellConfigOp,
    SetCellKindOp,
    SetCellMetaOp,
    SetSettingOp,
)

#: The kinds an insert or a kind change may name.
EDITABLE_KINDS: frozenset[str] = frozenset({"setup", "python", "sql", "markdown"})
MAX_CELLS = 2_000
SETUP_NAME = "setup"
MAX_SOURCE_BYTES = 1024 * 1024
_ID_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"
_CONFIG_KEYS: dict[str, type] = {
    "disabled": bool,
    "hide_code": bool,
    "column": int,
    "expand_output": bool,
}


class OpError(Exception):
    """A refused batch: the index of the failing op and its error code."""

    def __init__(self, index: int, code: str, message: str = "") -> None:
        super().__init__(f"op {index}: {code}" + (f" ({message})" if message else ""))
        self.index = index
        self.code = code


@dataclass(frozen=True)
class Cell:
    id: str
    kind: str
    name: str
    source: str
    config: dict[str, Any] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)
    deleted: bool = False


def random_cell_id(rng: random.Random) -> str:
    return "".join(rng.choice(_ID_ALPHABET) for _ in range(10))


def valid_name(name: str) -> bool:
    return name == "_" or (name.isidentifier() and not keyword.iskeyword(name))


@dataclass
class DocumentModel:
    """The document: an order of cell ids, the cells (deleted ones kept for
    restore), and the settings."""

    order: list[str] = field(default_factory=list)
    cells: dict[str, Cell] = field(default_factory=dict)
    settings: dict[str, Any] = field(
        default_factory=lambda: {"reactivity": "autorun", "dataframe": "auto"}
    )
    version: int = 0
    #: Where a deleted cell stood: the live cell before it when it was deleted.
    deleted_after: dict[str, str | None] = field(default_factory=dict)

    def copy(self) -> DocumentModel:
        return DocumentModel(
            order=list(self.order),
            cells=dict(self.cells),
            settings=dict(self.settings),
            version=self.version,
            deleted_after=dict(self.deleted_after),
        )

    # -- reading ---------------------------------------------------------------

    def live(self) -> list[Cell]:
        return [self.cells[cid] for cid in self.order if not self.cells[cid].deleted]

    def index_of(self, cell_id: str) -> int:
        return [c.id for c in self.live()].index(cell_id)

    def find(self, ref: str) -> Cell | None:
        """A live cell by id, or by name when the name is unique."""
        cell = self.cells.get(ref)
        if cell is not None and not cell.deleted:
            return cell
        named = [c for c in self.live() if c.name == ref and ref != "_"]
        return named[0] if len(named) == 1 else None

    def signature(self) -> list[tuple[str, str, str, str]]:
        """What two documents must agree on: (id, kind, name, source) per live cell."""
        return [(c.id, c.kind, c.name, c.source) for c in self.live()]

    # -- applying --------------------------------------------------------------

    def apply(
        self, ops: Sequence[NotebookOp], new_id: Callable[[], str]
    ) -> tuple[DocumentModel, list[str], list[str]]:
        """The document after ``ops`` (atomic), the ids created, and the ids
        changed. Raises :class:`OpError` and leaves ``self`` untouched."""
        ops = self._by_id(ops)
        doc = self.copy()
        created: list[str] = []
        changed: list[str] = []
        for index, op in enumerate(ops):
            touched = doc._apply_one(index, op, new_id)
            if isinstance(op, InsertCellOp) and touched:
                created.append(touched)
            if touched:
                changed.append(touched)
        # As the engine: only an insert is refused past the cap, so a notebook
        # loaded bigger than the cap can still be edited.
        if created and len(doc.live()) > MAX_CELLS:
            raise OpError(len(ops) - 1, "cap_exceeded", "too many cells")
        doc.version += 1
        return doc, created, list(dict.fromkeys(changed))

    def _by_id(self, ops: Sequence[NotebookOp]) -> list[NotebookOp]:
        """``ops`` with each cell they name by a live cell's unique name, as the
        document stood before the batch, rewritten to that cell's id. A name two
        live cells share is refused; a restore names a deleted cell by id only."""
        live_ids = {cell.id for cell in self.live()}
        by_name: dict[str, list[str]] = {}
        for cell in self.live():
            if cell.name != "_":
                by_name.setdefault(cell.name, []).append(cell.id)
        resolved: list[NotebookOp] = []
        for index, op in enumerate(ops):
            changes: dict[str, str] = {}
            for ref_field in ("cell_id", "after", "before"):
                ref = getattr(op, ref_field, None)
                if not isinstance(ref, str) or ref in live_ids:
                    continue
                if ref_field == "cell_id" and isinstance(op, RestoreCellOp):
                    continue
                named = by_name.get(ref, [])
                if len(named) > 1:
                    raise OpError(index, "ambiguous_cell", ref)
                if named:
                    changes[ref_field] = named[0]
            resolved.append(op.model_copy(update=changes) if changes else op)
        return resolved

    def _live_cell(self, index: int, cell_id: str) -> Cell:
        cell = self.cells.get(cell_id)
        if cell is None or cell.deleted:
            raise OpError(index, "cell_not_found", cell_id)
        return cell

    def _anchor(self, index: int, ref: str | None) -> str | None:
        if ref is None:
            return None
        return self._live_cell(index, ref).id

    def _position(self, index: int, after: str | None, before: str | None) -> int:
        """Where in ``order`` a cell placed after ``after`` or before ``before`` goes."""
        after_id = self._anchor(index, after)
        before_id = self._anchor(index, before)
        if after_id is not None:
            return self.order.index(after_id) + 1
        if before_id is not None:
            return self.order.index(before_id)
        return len(self.order)

    def _check_setup(self, index: int) -> None:
        live = self.live()
        setups = [i for i, c in enumerate(live) if c.kind == "setup"]
        if setups and setups != [0]:
            raise OpError(index, "setup_must_be_first")

    def _check_source(self, index: int, source: str) -> None:
        if len(source.encode("utf-8")) > MAX_SOURCE_BYTES:
            raise OpError(index, "cap_exceeded", "source too large")

    def _check_config(self, index: int, config: dict[str, Any]) -> None:
        for key, value in config.items():
            expected = _CONFIG_KEYS.get(key)
            if expected is None or type(value) is not expected:
                raise OpError(index, "invalid_config", key)

    def _apply_one(self, index: int, op: NotebookOp, new_id: Callable[[], str]) -> str | None:
        if isinstance(op, InsertCellOp):
            if op.kind not in EDITABLE_KINDS:
                raise OpError(index, "unknown_kind", op.kind)
            if not valid_name(op.name):
                raise OpError(index, "invalid_name", op.name)
            self._check_source(index, op.source)
            self._check_config(index, op.config)
            cid = new_id()
            while cid in self.cells:
                cid = new_id()
            pos = self._position(index, op.after, op.before)
            # The setup cell is named `setup` (marimo's name for it) unless named otherwise.
            name = SETUP_NAME if op.kind == "setup" and op.name == "_" else op.name
            self.cells[cid] = Cell(cid, op.kind, name, op.source, dict(op.config), dict(op.meta))
            self.order.insert(pos, cid)
            self._check_setup(index)
            return cid
        if isinstance(op, EditCellOp):
            cell = self._live_cell(index, op.cell_id)
            text = cell.source
            for edit in op.edits:
                text = _apply_text_edit(index, text, edit.old, edit.new, edit.occurrence)
            self._check_source(index, text)
            self.cells[cell.id] = replace(cell, source=text)
            return cell.id
        if isinstance(op, ReplaceCellOp):
            cell = self._live_cell(index, op.cell_id)
            self._check_source(index, op.source)
            self.cells[cell.id] = replace(cell, source=op.source)
            return cell.id
        if isinstance(op, DeleteCellOp):
            cell = self._live_cell(index, op.cell_id)
            live_ids = [c.id for c in self.live()]
            pos = live_ids.index(cell.id)
            self.deleted_after[cell.id] = live_ids[pos - 1] if pos > 0 else None
            self.cells[cell.id] = replace(cell, deleted=True)
            return cell.id
        if isinstance(op, RestoreCellOp):
            gone = self.cells.get(op.cell_id)
            if gone is None or not gone.deleted:
                raise OpError(index, "cell_not_found", op.cell_id)
            cell = gone
            self.order.remove(cell.id)
            anchor = op.after if op.after is not None else self.deleted_after.get(cell.id)
            if anchor is not None and (anchor not in self.cells or self.cells[anchor].deleted):
                if op.after is not None:
                    raise OpError(index, "cell_not_found", op.after)
                anchor = None
            pos = self.order.index(anchor) + 1 if anchor is not None else 0
            self.order.insert(pos, cell.id)
            self.cells[cell.id] = replace(cell, deleted=False)
            self.deleted_after.pop(cell.id, None)
            self._check_setup(index)
            return cell.id
        if isinstance(op, MoveCellOp):
            cell = self._live_cell(index, op.cell_id)
            if op.after == cell.id or op.before == cell.id:
                return cell.id
            self._anchor(index, op.after)
            self._anchor(index, op.before)
            self.order.remove(cell.id)
            pos = self._position(index, op.after, op.before)
            self.order.insert(pos, cell.id)
            self._check_setup(index)
            return cell.id
        if isinstance(op, RenameCellOp):
            cell = self._live_cell(index, op.cell_id)
            if not valid_name(op.name):
                raise OpError(index, "invalid_name", op.name)
            self.cells[cell.id] = replace(cell, name=op.name)
            return cell.id
        if isinstance(op, SetCellKindOp):
            cell = self._live_cell(index, op.cell_id)
            if op.kind not in EDITABLE_KINDS:
                raise OpError(index, "unknown_kind", op.kind)
            self.cells[cell.id] = replace(cell, kind=op.kind)
            self._check_setup(index)
            return cell.id
        if isinstance(op, SetCellConfigOp):
            cell = self._live_cell(index, op.cell_id)
            self._check_config(index, op.config)
            self.cells[cell.id] = replace(cell, config={**cell.config, **op.config})
            return cell.id
        if isinstance(op, SetCellMetaOp):
            cell = self._live_cell(index, op.cell_id)
            if cell.kind not in ("sql", "markdown"):
                raise OpError(index, "invalid_config", f"{cell.kind} cells take no meta")
            meta = {k: v for k, v in {**cell.meta, **op.meta}.items() if v is not None}
            self.cells[cell.id] = replace(cell, meta=meta)
            return cell.id
        if isinstance(op, SetSettingOp):
            spec = BY_NAME.get(op.key)
            if spec is None or (op.value is not None and not spec.valid(op.value)):
                raise OpError(index, "invalid_config", op.key)
            if op.value is None:
                self.settings.pop(op.key, None)
            else:
                self.settings[op.key] = op.value
            return None
        raise OpError(index, "invalid_config", "unknown op")


def _apply_text_edit(index: int, text: str, old: str, new: str, occurrence: int | None) -> str:
    if old == "":
        if text == "" and occurrence in (None, 1):
            return new
        raise OpError(index, "edit_not_found", "empty old text")
    count = text.count(old)
    if count == 0:
        raise OpError(index, "edit_not_found")
    if occurrence is None:
        if count > 1:
            raise OpError(index, "edit_ambiguous")
        return text.replace(old, new, 1)
    if occurrence > count:
        raise OpError(index, "edit_not_found", f"occurrence {occurrence} of {count}")
    start = -1
    for _ in range(occurrence):
        start = text.index(old, start + 1)
    return text[:start] + new + text[start + len(old) :]


__all__ = [
    "EDITABLE_KINDS",
    "MAX_CELLS",
    "MAX_SOURCE_BYTES",
    "Cell",
    "DocumentModel",
    "OpError",
    "random_cell_id",
    "valid_name",
]
