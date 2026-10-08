"""Applying a batch of operations to a document, and normalizing it.

Pure: :func:`apply_ops` works on a clone and either returns the new document
or raises :class:`NotebookOpError` for the first op that cannot apply, in
which case nothing changed.
"""

from __future__ import annotations

import keyword
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, TypeVar

from alkera_notebook.document.fmt import FormatApi
from alkera_notebook.document.model import DocCell, Document
from alkera_notebook.document.ops import (
    CellNotice,
    DeleteCell,
    EditCell,
    InsertCell,
    MoveCell,
    NotebookOp,
    NotebookOpError,
    ReplaceCell,
    RestoreCell,
    SetCellConfig,
    SetCellKind,
    SetCellMeta,
    SetCellName,
    SetSetting,
)
from alkera_notebook.format.op_rules import (
    CONFIG_TYPES,
    DEFAULT_META,
    TEMPLATED_KINDS,
    OpRuleError,
    apply_edit,
    check_config_keys,
    check_insertable,
    kind_change,
    templated_code,
)
from alkera_notebook.format.settings import BY_NAME

T = TypeVar("T")

MAX_CELLS = 2000
MAX_SOURCE_BYTES = 1024 * 1024
#: The largest rendered notebook file (below the kernel RPC's 16 MiB frame and
#: the box's text transfer). Every cell's source is in the file, so a batch whose
#: sources alone pass it is refused before anything renders.
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_OPS_PER_BATCH = 500

CONFIG_DEFAULTS: dict[str, Any] = {
    "column": None,
    "disabled": False,
    "hide_code": False,
    "expand_output": False,
}

CONNECTION_RE = re.compile(r"^[\w .-]{1,128}$")
IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

SETTING_SPECS = BY_NAME


@dataclass
class ApplyOutcome:
    document: Document
    created: list[str] = field(default_factory=list)
    touched: list[str] = field(default_factory=list)
    notices: list[CellNotice] = field(default_factory=list)
    settings_changed: bool = False


def is_identifier(name: str) -> bool:
    return bool(IDENT_RE.match(name)) and not keyword.iskeyword(name)


def valid_name(name: str) -> bool:
    return name == "_" or is_identifier(name)


def _source_bytes(text: str) -> int:
    return len(text.encode("utf-8"))


class _Batch:
    def __init__(self, doc: Document, fmt: FormatApi, new_id: Callable[[], str]) -> None:
        self.doc = doc
        self.fmt = fmt
        self.new_id = new_id
        self.out = ApplyOutcome(document=doc)
        self.index = 0

    # helpers -----------------------------------------------------------

    def fail(self, code: str, message: str) -> NotebookOpError:
        return NotebookOpError(self.index, code, message)

    def touch(self, cell_id: str) -> None:
        if cell_id not in self.out.touched:
            self.out.touched.append(cell_id)

    def notice(self, kind: str, cell_id: str | None, message: str, **data: Any) -> None:
        self.out.notices.append(CellNotice(kind=kind, cell_id=cell_id, message=message, data=data))

    def cell(self, cell_id: str) -> DocCell:
        c = self.doc.cells.get(cell_id)
        if c is None:
            raise self.fail("cell_not_found", f"no cell {cell_id}")
        return c

    def live(self, cell_id: str) -> DocCell:
        c = self.cell(cell_id)
        if c.deleted:
            raise self.fail("cell_not_found", f"cell {cell_id} is deleted")
        return c

    def anchor_after(self, cell_id: str, order: list[str]) -> int:
        """Position just after ``cell_id``; a deleted cell anchors where it last was."""
        seen: set[str] = set()
        cur: str | None = cell_id
        while cur is not None and cur not in seen:
            seen.add(cur)
            if cur in order:
                return order.index(cur) + 1
            c = self.doc.cells.get(cur)
            cur = c.deleted_after if c is not None else None
        return 0

    def anchors(self, *cell_ids: str | None) -> None:
        """Every cell an op names as an anchor exists, whether or not the op
        ends up placing anything against it."""
        for cell_id in cell_ids:
            if cell_id is not None:
                self.cell(cell_id)

    def position(
        self, after: str | None, before: str | None, order: list[str], default: int
    ) -> int:
        self.anchors(after, before)
        if after is not None:
            return self.anchor_after(after, order)
        if before is not None:
            target = self.cell(before)
            if not target.deleted and before in order:
                return order.index(before)
            return self.anchor_after(before, order)
        return default

    def check_position(self, kind: str, pos: int, order: list[str]) -> None:
        if kind == "setup":
            if pos != 0:
                raise self.fail("setup_must_be_first", "the setup cell must be first")
            if order and self.doc.cells[order[0]].kind == "setup":
                raise self.fail("setup_must_be_first", "the notebook already has a setup cell")
        elif pos == 0 and order and self.doc.cells[order[0]].kind == "setup":
            raise self.fail("setup_must_be_first", "nothing can go before the setup cell")

    def check_config(self, config: dict[str, Any]) -> dict[str, Any]:
        try:
            check_config_keys(config)
        except OpRuleError as exc:
            raise self.fail(exc.code, exc.message) from exc
        for k, v in config.items():
            t = CONFIG_TYPES[k]
            if v is None:
                continue
            if t is int and (isinstance(v, bool) or not isinstance(v, int)):
                raise self.fail("invalid_config", f"{k} must be an integer")
            if t is bool and not isinstance(v, bool):
                raise self.fail("invalid_config", f"{k} must be true or false")
        return config

    def check_meta(self, kind: str, meta: dict[str, Any]) -> dict[str, Any]:
        if kind == "sql":
            merged = {**DEFAULT_META["sql"], **meta}
            for k, v in merged.items():
                if k == "output_var":
                    if not isinstance(v, str) or not is_identifier(v):
                        raise self.fail("invalid_config", "output_var must be an identifier")
                elif k == "connection":
                    if v is not None and (not isinstance(v, str) or not CONNECTION_RE.match(v)):
                        raise self.fail("invalid_config", "invalid connection name")
                elif k == "show_output":
                    if not isinstance(v, bool):
                        raise self.fail("invalid_config", "show_output must be true or false")
                elif k == "engine":
                    if v is not None and not isinstance(v, str):
                        raise self.fail("invalid_config", "engine must be a string")
                else:
                    raise self.fail("invalid_config", f"unknown sql meta key {k}")
            return {k: v for k, v in merged.items() if v is not None}
        if kind == "markdown":
            merged = {**DEFAULT_META["markdown"], **meta}
            for k, v in merged.items():
                if k != "quote" or v not in ("r", "rf"):
                    raise self.fail("invalid_config", f"invalid markdown meta {k}")
            return merged
        if meta:
            raise self.fail("invalid_config", f"{kind} cells take no meta")
        return {}

    def code_of(self, kind: str, source: str, meta: dict[str, Any]) -> str:
        """The code (kind, source) is written as; refused when that kind cannot hold it."""
        return self.rule(
            lambda: templated_code(
                kind, source, meta, render=self.fmt.render_cell, classify=self.fmt.classify
            )
        )

    def canonical_meta(self, kind: str, code: str, meta: dict[str, Any]) -> dict[str, Any]:
        """The meta the format reads back from ``code``: a document holds the
        same meta a reload of its own file would give."""
        if kind not in TEMPLATED_KINDS:
            return meta
        return dict(self.fmt.classify(code)[2])

    def check_size(self, c: DocCell) -> None:
        if _source_bytes(c.source) > MAX_SOURCE_BYTES or _source_bytes(c.code) > MAX_SOURCE_BYTES:
            raise self.fail("cap_exceeded", f"cell {c.id} is larger than 1 MiB")

    def check_name(self, cell_id: str, name: str) -> None:
        if not valid_name(name):
            raise self.fail("invalid_name", f"{name!r} is not a valid cell name")
        if name != "_":
            for other in self.doc.live_cells():
                if other.id != cell_id and other.name == name:
                    self.notice("duplicate_name", cell_id, f"another cell is named {name}")
                    break

    def set_text(self, c: DocCell, source: str) -> None:
        code = self.code_of(c.kind, source, c.meta)
        c.source = source
        c.code = code
        c.meta = self.canonical_meta(c.kind, code, c.meta)
        self.check_size(c)

    # ops ---------------------------------------------------------------

    def rule(self, check: Callable[[], T]) -> T:
        """``check`` under the shared op rules, its refusal as this batch's."""
        try:
            return check()
        except OpRuleError as refused:
            raise self.fail(refused.code, refused.message) from refused

    def insert(self, op: InsertCell) -> None:
        self.rule(lambda: check_insertable(op.kind))
        if len(self.doc.cells) >= MAX_CELLS:
            raise self.fail("cap_exceeded", f"a notebook holds at most {MAX_CELLS} cells")
        config = {
            k: v
            for k, v in self.check_config(dict(op.config)).items()
            if v != CONFIG_DEFAULTS.get(k)
        }
        meta = self.check_meta(op.kind, dict(op.meta))
        name = "setup" if op.kind == "setup" and op.name == "_" else op.name
        order = self.doc.order
        pos = self.position(op.after, op.before, order, len(order))
        self.check_position(op.kind, pos, order)
        cid = self.new_id()
        while cid in self.doc.cells:
            cid = self.new_id()
        self.check_name(cid, name)
        kind = op.kind
        code = self.code_of(kind, op.source, meta)
        meta = self.canonical_meta(kind, code, meta)
        c = DocCell(
            id=cid, kind=kind, name=name, source=op.source, code=code, config=config, meta=meta
        )
        self.check_size(c)
        self.doc.cells[cid] = c
        order.insert(pos, cid)
        self.out.created.append(cid)
        self.touch(cid)

    def edit(self, op: EditCell) -> None:
        c = self.cell(op.cell_id)
        text = c.source
        for e in op.edits:
            try:
                _, text = apply_edit(text, e.old, e.new, e.occurrence)
            except OpRuleError as refused:
                raise self.fail(refused.code, refused.message) from refused
        self.set_text(c, text)
        if c.deleted:
            self.notice("edited_deleted_cell", c.id, "this cell was deleted; restore it to keep it")
        self.touch(c.id)

    def replace(self, op: ReplaceCell) -> None:
        c = self.cell(op.cell_id)
        self.set_text(c, op.source)
        if c.deleted:
            self.notice("edited_deleted_cell", c.id, "this cell was deleted; restore it to keep it")
        self.touch(c.id)

    def delete(self, op: DeleteCell) -> None:
        c = self.cell(op.cell_id)
        if c.deleted:
            return
        order = self.doc.order
        i = order.index(c.id)
        c.deleted_after = order[i - 1] if i > 0 else None
        c.deleted = True
        order.remove(c.id)
        self.touch(c.id)

    def restore(self, op: RestoreCell) -> None:
        c = self.cell(op.cell_id)
        self.anchors(op.after)
        if not c.deleted:
            return
        order = self.doc.order
        if op.after is not None:
            pos = self.position(op.after, None, order, len(order))
        elif c.deleted_after is not None:
            pos = self.anchor_after(c.deleted_after, order)
        else:
            pos = 0
        if c.kind != "setup" and pos == 0 and order and self.doc.cells[order[0]].kind == "setup":
            pos = 1
        self.check_position(c.kind, pos, order)
        c.deleted = False
        c.deleted_after = None
        order.insert(pos, c.id)
        self.touch(c.id)

    def move(self, op: MoveCell) -> None:
        c = self.live(op.cell_id)
        self.anchors(op.after, op.before)
        order = self.doc.order
        old = order.index(c.id)
        rest = [i for i in order if i != c.id]
        if op.after == c.id or op.before == c.id:
            return
        pos = self.position(op.after, op.before, rest, len(rest))
        if c.kind == "setup":
            if pos != 0:
                raise self.fail("setup_must_be_first", "the setup cell must be first")
        elif pos == 0 and rest and self.doc.cells[rest[0]].kind == "setup":
            raise self.fail("setup_must_be_first", "nothing can go before the setup cell")
        rest.insert(pos, c.id)
        self.doc.order[:] = rest
        if pos != old:
            self.touch(c.id)

    def rename(self, op: SetCellName) -> None:
        c = self.cell(op.cell_id)
        self.check_name(c.id, op.name)
        c.name = op.name
        self.touch(c.id)

    def set_kind(self, op: SetCellKind) -> None:
        c = self.cell(op.cell_id)
        target = op.kind
        self.rule(lambda: check_insertable(target))
        if target == c.kind:
            return
        if target == "setup" or c.kind == "setup":
            order = self.doc.order
            if target == "setup" and (not order or order[0] != c.id):
                raise self.fail("setup_must_be_first", "only the first cell can be setup")
        source, meta = self.rule(
            lambda: kind_change(
                c.kind,
                target,
                c.source,
                c.meta,
                render=self.fmt.render_cell,
                classify=self.fmt.classify,
            )
        )
        if target in TEMPLATED_KINDS:
            code = self.code_of(target, source, meta)
            meta = self.canonical_meta(target, code, meta)
            c.kind, c.source, c.meta, c.code = target, source, meta, code
        else:
            c.kind, c.source, c.meta = target, source, meta
            if target == "setup":
                c.name = "setup"
        self.check_size(c)
        self.touch(c.id)

    def set_config(self, op: SetCellConfig) -> None:
        c = self.cell(op.cell_id)
        merged = {**c.config, **self.check_config(dict(op.config))}
        c.config = {k: v for k, v in merged.items() if v is not None and v != CONFIG_DEFAULTS[k]}
        self.touch(c.id)

    def set_meta(self, op: SetCellMeta) -> None:
        c = self.cell(op.cell_id)
        if c.kind not in TEMPLATED_KINDS:
            raise self.fail("invalid_config", f"{c.kind} cells take no meta")
        # ``None`` resets a key: the check fills its default back in (or, for
        # a key whose default is no value, leaves it out).
        merged = {k: v for k, v in {**c.meta, **op.meta}.items() if v is not None}
        meta = self.check_meta(c.kind, merged)
        code = self.code_of(c.kind, c.source, meta)
        c.meta = self.canonical_meta(c.kind, code, meta)
        c.code = code
        self.check_size(c)
        self.touch(c.id)

    def set_setting(self, op: SetSetting) -> None:
        key, value = op.key, op.value
        if key == "header":
            if not isinstance(value, str):
                raise self.fail("invalid_config", "header must be text")
            self.doc.meta.header = value
        elif key in SETTING_SPECS:
            # ``None`` takes the setting out of the file: its value is then
            # inherited (the workspace's, detection's or the default).
            if value is None:
                self.doc.settings.pop(key, None)
            elif SETTING_SPECS[key].valid(value):
                self.doc.settings[key] = value
            else:
                expected = SETTING_SPECS[key].expected
                raise self.fail("invalid_config", f"{key} must be {expected}")
        else:
            raise self.fail("invalid_config", f"unknown setting {key}")
        self.out.settings_changed = True

    def run(self, ops: Sequence[NotebookOp]) -> ApplyOutcome:
        if len(ops) > MAX_OPS_PER_BATCH:
            self.index = MAX_OPS_PER_BATCH
            raise self.fail("cap_exceeded", f"a batch holds at most {MAX_OPS_PER_BATCH} ops")
        for i, op in enumerate(ops):
            self.index = i
            if isinstance(op, InsertCell):
                self.insert(op)
            elif isinstance(op, EditCell):
                self.edit(op)
            elif isinstance(op, ReplaceCell):
                self.replace(op)
            elif isinstance(op, DeleteCell):
                self.delete(op)
            elif isinstance(op, RestoreCell):
                self.restore(op)
            elif isinstance(op, MoveCell):
                self.move(op)
            elif isinstance(op, SetCellName):
                self.rename(op)
            elif isinstance(op, SetCellKind):
                self.set_kind(op)
            elif isinstance(op, SetCellConfig):
                self.set_config(op)
            elif isinstance(op, SetCellMeta):
                self.set_meta(op)
            else:
                self.set_setting(op)
        total = sum(_source_bytes(c.source) for c in self.doc.cells.values() if not c.deleted)
        if total > MAX_FILE_BYTES:
            raise self.fail("cap_exceeded", "a notebook file holds at most 4 MiB")
        normalize(self.doc)
        return self.out


def check_file_size(text: str, index: int) -> None:
    """Refuse a batch (as its op ``index``) whose rendered file passes
    :data:`MAX_FILE_BYTES`."""
    if _source_bytes(text) > MAX_FILE_BYTES:
        raise NotebookOpError(index, "cap_exceeded", "a notebook file holds at most 4 MiB")


def apply_ops(
    doc: Document,
    ops: Sequence[NotebookOp],
    *,
    fmt: FormatApi,
    new_id: Callable[[], str],
) -> ApplyOutcome:
    """Apply ``ops`` to a clone of ``doc``. Atomic: raises before returning anything."""
    return _Batch(doc.clone(), fmt, new_id).run(ops)


def normalize(doc: Document) -> Document:
    """Repair the order and setup invariants in place (idempotent); returns ``doc``."""
    seen: set[str] = set()
    order: list[str] = []
    for cid in doc.order:
        c = doc.cells.get(cid)
        if c is None or c.deleted or cid in seen:
            continue
        seen.add(cid)
        order.append(cid)
    # A live cell nobody placed goes at the end, in id order: the same rule
    # the platform's document applies, so both stores agree on the result.
    order.extend(sorted(cid for cid, c in doc.cells.items() if not c.deleted and cid not in seen))
    setups = [cid for cid in order if doc.cells[cid].kind == "setup"]
    if setups:
        first = setups[0]
        for extra in setups[1:]:
            doc.cells[extra].kind = "python"
        if order[0] != first:
            order.remove(first)
            order.insert(0, first)
    doc.order[:] = order
    return doc
