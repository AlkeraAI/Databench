"""Operations on a notebook document from its peers, in the sandbox.

The agent and box peers send the engine's document operations; this module
applies them to the Loro notebook document (:mod:`.notebook` holds its schema,
its strategy and its normal form) on a fork at the version their token names
(:func:`apply_ops`), carries a batch or an editor's unsent update made before
a history restart into the current epoch cell by cell (:func:`rebase_ops`,
:func:`rebase_update`), and reads the document as the notebook view, the
dependency graph and normal form need it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, TypeVar

import loro
from loro import EphemeralStore, ExportMode, LoroDoc, LoroMap, LoroText, Side, VersionVector

from backend.services.crdt.sandbox import core
from backend.services.crdt.sandbox.core import SandboxError, guard, rewrite_text
from backend.services.crdt.sandbox.notebook import (
    AGENT_CARET_MS,
    CELL_ID_RE,
    CONFIG_TYPES,
    KINDS,
    MAX_CELLS,
    MAX_FREE_TEXT_BYTES,
    MAX_SOURCE_BYTES,
    META_KEYS,
    NOTEBOOK,
    Cell,
    NotebookStrategy,
    all_cells,
    as_str,
    child,
    config_ok,
    effective_kind,
    ensure_map,
    ensure_text,
    fill_cell,
    insert_map,
    insert_text,
    live_order,
    meta_ok,
    name_ok,
    normalize,
    raw_order,
    read_cell,
    set_value,
    setting_names,
    setting_ok,
    settings_of,
    sync_map,
    sync_text,
    utf8_len,
)

T = TypeVar("T")


class OpError(Exception):
    """An operation the batch is refused for: its index and a code."""

    def __init__(self, index: int, code: str, message: str) -> None:
        super().__init__(message)
        self.index = index
        self.code = code
        self.message = message


@dataclass(slots=True)
class _Batch:
    created: list[str] = field(default_factory=list)
    touched: list[str] = field(default_factory=list)
    edited: list[str] = field(default_factory=list)
    #: The last edit: the cell and the offset at the end of what it wrote.
    caret: tuple[str, int] | None = None

    def touch(self, cell_id: str) -> None:
        if cell_id not in self.touched:
            self.touched.append(cell_id)


def _live_cell(doc: LoroDoc, index: int, cell_id: Any) -> Cell:
    cell = read_cell(doc, cell_id) if isinstance(cell_id, str) else None
    if cell is None:
        raise OpError(index, "cell_not_found", f"no cell {cell_id!r}")
    return cell


def _anchor_index(doc: LoroDoc, index: int, cell_id: Any) -> int:
    """Where ``cell_id`` stands in ``order``; the cell must be live."""
    cell = _live_cell(doc, index, cell_id)
    order = raw_order(doc)
    if cell.deleted or cell.id not in order:
        raise OpError(index, "cell_not_found", f"cell {cell_id!r} is deleted")
    return order.index(cell.id)


def _position(doc: LoroDoc, index: int, after: Any, before: Any) -> int:
    if after is not None:
        return _anchor_index(doc, index, after) + 1
    if before is not None:
        return _anchor_index(doc, index, before)
    return len(raw_order(doc))


def _check_cell_fields(index: int, kind: Any, name: Any, config: Any, meta: Any) -> None:
    if kind not in KINDS:
        raise OpError(index, "unknown_kind", f"no cell kind {kind!r}")
    if not name_ok(name):
        raise OpError(index, "invalid_name", f"{name!r} is not a cell name")
    if not isinstance(config, dict) or not config_ok(config):
        raise OpError(index, "invalid_config", "the cell configuration is not marimo's")
    if not isinstance(meta, dict) or not meta_ok(str(kind), meta, meta.keys()):
        raise OpError(index, "invalid_config", f"the metadata does not fit a {kind} cell")


def _setup_ids(doc: LoroDoc, exclude: str | None = None) -> list[str]:
    cells = all_cells(doc)
    return [c for c in live_order(doc, cells) if cells[c].kind == "setup" and c != exclude]


def _source_text(doc: LoroDoc, cell_id: str) -> LoroText:
    cell_map = child(doc.get_map("cells"), cell_id)
    return ensure_text(cell_map, "source")


def _new_id(doc: LoroDoc, new_cell_id: Callable[[], str]) -> str:
    cells_root = doc.get_map("cells")
    for _ in range(16):
        candidate = str(new_cell_id())
        if CELL_ID_RE.match(candidate) and cells_root.get(candidate) is None:
            return candidate
    raise SandboxError("internal", "could not mint a fresh cell id")


def _rule(index: int, check: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """``check`` under the format's op rules (``alkera_notebook.format.op_rules``,
    the one statement of what an op means for every applier), a refusal
    reported as this op's."""
    try:
        return check(*args, **kwargs)
    except ValueError as refused:
        code = getattr(refused, "code", None)
        if not isinstance(code, str):
            raise
        raise OpError(index, code, str(refused)) from refused


def _writable(index: int, fmt: Any, kind: str, source: str, meta: Mapping[str, Any]) -> None:
    """Refuse text a cell of ``kind`` could not be written back to its file as."""
    _rule(
        index,
        fmt.op_rules.templated_code,
        kind,
        source,
        meta,
        render=fmt.render_cell,
        classify=fmt.classify,
    )


def _apply_op(doc: LoroDoc, index: int, op: Mapping[str, Any], batch: _Batch, fmt: Any) -> None:
    kind = op.get("op")
    cells_root = doc.get_map("cells")
    if kind == "insert":
        cell_kind = op.get("kind", "python")
        name = op.get("name", "_")
        config = op.get("config") or {}
        meta = op.get("meta") or {}
        source = op.get("source") or ""
        _rule(index, fmt.op_rules.check_insertable, cell_kind)
        if isinstance(config, dict):
            _rule(index, fmt.op_rules.check_config_keys, config)
        _check_cell_fields(index, cell_kind, name, config, meta)
        if not isinstance(source, str):
            raise OpError(index, "invalid_config", "a cell's source is text")
        _writable(index, fmt, cell_kind, source, meta)
        if len(cells_root.keys()) >= MAX_CELLS or utf8_len(source) > MAX_SOURCE_BYTES:
            raise OpError(index, "cap_exceeded", "the notebook or the cell is at its size cap")
        position = _position(doc, index, op.get("after"), op.get("before"))
        if cell_kind == "setup":
            if _setup_ids(doc) or position != 0:
                raise OpError(index, "setup_must_be_first", "a setup cell is the one first cell")
        cell_id = _new_id(doc, fmt.new_cell_id)
        created: LoroMap = insert_map(cells_root, cell_id)
        fill_cell(created, kind=cell_kind, name=name, source=source)
        sync_map(ensure_map(created, "config"), config, keep=False)
        sync_map(ensure_map(created, "meta"), meta, keep=False)
        ensure_map(created, "extra")
        doc.get_movable_list("order").insert(position, cell_id)
        batch.created.append(cell_id)
        batch.touch(cell_id)
        if source:
            batch.caret = (cell_id, len(source))
        return
    if kind == "set_setting":
        key, value = op.get("key"), op.get("value")
        if key == "header":
            # The text outside the fence (comments, a docstring), diffed into
            # the header so concurrent typing there survives.
            if not isinstance(value, str):
                raise OpError(index, "invalid_config", "the header is text")
            if utf8_len(value) > MAX_FREE_TEXT_BYTES:
                raise OpError(index, "cap_exceeded", "the header would pass its size cap")
            sync_text(ensure_text(doc.get_map("meta"), "header"), value, keep=False)
            return
        if not isinstance(key, str) or key not in setting_names():
            raise OpError(index, "invalid_config", f"no setting {key!r}")
        settings = doc.get_map("settings")
        if value is None:
            if settings.get(key) is not None:
                settings.delete(key)
            return
        if not setting_ok(key, value):
            raise OpError(index, "invalid_config", f"{value!r} is not a value of {key}")
        set_value(settings, key, value)
        return
    cell = _live_cell(doc, index, op.get("cell_id"))
    cell_map = child(cells_root, cell.id)
    batch.touch(cell.id)
    if kind in ("edit", "replace"):
        text = _source_text(doc, cell.id)
        current = str(text.to_string())
        if kind == "replace":
            target = op.get("source")
            if not isinstance(target, str):
                raise OpError(index, "invalid_config", "a replacement is text")
            if utf8_len(target) > MAX_SOURCE_BYTES:
                raise OpError(index, "cap_exceeded", "the cell would pass its size cap")
            _writable(index, fmt, cell.kind, target, cell.meta)
            edits = core.text_edits(current, target)
            rewrite_text(text, current, target)
            end = (edits[-1][0] + len(edits[-1][2])) if edits else None
            if end is not None:
                shift = sum(len(ins) - rem for at, rem, ins in edits[:-1])
                batch.caret = (cell.id, end + shift)
        else:
            for edit in op.get("edits") or []:
                old, new = edit.get("old"), edit.get("new")
                if not isinstance(old, str) or not isinstance(new, str):
                    raise OpError(index, "edit_not_found", "an edit is old and new text")
                occurrence = edit.get("occurrence")
                at, updated = _rule(index, fmt.op_rules.apply_edit, current, old, new, occurrence)
                if utf8_len(updated) > MAX_SOURCE_BYTES:
                    raise OpError(index, "cap_exceeded", "the cell would pass its size cap")
                for offset, remove, insert in reversed(core.text_edits(old, new)):
                    if remove:
                        text.delete(at + offset, remove)
                    if insert:
                        text.insert(at + offset, insert)
                current = updated
                batch.caret = (cell.id, at + len(new))
            _writable(index, fmt, cell.kind, current, cell.meta)
        batch.edited.append(cell.id)
        return
    if kind == "delete":
        if not cell.deleted:
            cell_map.insert("deleted", True)
        order = doc.get_movable_list("order")
        for position in range(len(raw_order(doc)) - 1, -1, -1):
            if raw_order(doc)[position] == cell.id:
                order.delete(position, 1)
        return
    if kind == "restore":
        if cell.deleted:
            cell_map.insert("deleted", False)
        if cell.kind == "setup" and _setup_ids(doc, exclude=cell.id):
            raise OpError(index, "setup_must_be_first", "the notebook has a setup cell already")
        _move(doc, index, cell.id, op.get("after"), None)
        return
    if kind == "move":
        if cell.deleted:
            raise OpError(index, "cell_not_found", f"cell {cell.id!r} is deleted")
        _move(doc, index, cell.id, op.get("after"), op.get("before"))
        return
    if kind == "rename":
        name = op.get("name")
        if not name_ok(name):
            raise OpError(index, "invalid_name", f"{name!r} is not a cell name")
        set_value(cell_map, "name", name)
        return
    if kind == "set_kind":
        new_kind = op.get("kind")
        _rule(index, fmt.op_rules.check_insertable, new_kind)
        if new_kind == cell.kind:
            return
        if new_kind == "setup" and _setup_ids(doc, exclude=cell.id):
            raise OpError(index, "setup_must_be_first", "the notebook has a setup cell already")
        source, meta = _rule(
            index,
            fmt.op_rules.kind_change,
            cell.kind,
            str(new_kind),
            cell.source,
            cell.meta,
            render=fmt.render_cell,
            classify=fmt.classify,
        )
        cell_map.insert("kind", new_kind)
        # A new text: typing that has not reached this peer yet lands in the
        # old one, which the editors offer back.
        text = insert_text(cell_map, "source")
        if source:
            text.insert(0, source)
        admitted = META_KEYS.get(str(new_kind), frozenset())
        meta_map = ensure_map(cell_map, "meta")
        for key in list(meta_map.keys()):
            if str(key) not in admitted:
                meta_map.delete(str(key))
        wanted = {k: v for k, v in meta.items() if k in admitted and v is not None}
        sync_map(meta_map, wanted, keep=True)
        if new_kind == "setup":
            _move(doc, index, cell.id, None, None, first=True)
        return
    if kind == "set_config":
        config = op.get("config")
        if not isinstance(config, dict):
            raise OpError(index, "invalid_config", "a configuration is a mapping")
        _rule(index, fmt.op_rules.check_config_keys, config)
        wanted = {k: v for k, v in config.items() if v is not None}
        if not config_ok(wanted) or not all(k in CONFIG_TYPES for k in config):
            raise OpError(index, "invalid_config", "the cell configuration is not marimo's")
        config_map = ensure_map(cell_map, "config")
        for key, value in config.items():
            if value is None:
                if config_map.get(key) is not None:
                    config_map.delete(key)
            else:
                set_value(config_map, key, value)
        return
    if kind == "set_meta":
        meta = op.get("meta")
        if not isinstance(meta, dict):
            raise OpError(index, "invalid_config", "a cell's metadata is a mapping")
        admitted = META_KEYS.get(cell.kind, frozenset())
        if not admitted:
            raise OpError(index, "invalid_config", f"a {cell.kind} cell has no metadata")
        wanted = {k: v for k, v in meta.items() if v is not None}
        if not all(k in admitted for k in meta) or not meta_ok(cell.kind, wanted, wanted.keys()):
            raise OpError(index, "invalid_config", f"the metadata does not fit a {cell.kind} cell")
        after = {k: v for k, v in {**cell.meta, **meta}.items() if v is not None}
        _writable(index, fmt, cell.kind, cell.source, after)
        meta_map = ensure_map(cell_map, "meta")
        for key, value in meta.items():
            if value is None:
                if meta_map.get(key) is not None:
                    meta_map.delete(key)
            else:
                set_value(meta_map, key, value)
        return
    raise OpError(index, "invalid_config", f"no operation {kind!r}")


def _move(
    doc: LoroDoc, index: int, cell_id: str, after: Any, before: Any, *, first: bool = False
) -> None:
    order = doc.get_movable_list("order")
    current = raw_order(doc)
    if first:
        dest = 0
    else:
        dest = _position(doc, index, after, before) if (after or before) else len(current)
    if cell_id in current:
        at = current.index(cell_id)
        if at < dest:
            dest -= 1
        if at != dest:
            order.mov(at, dest)
    else:
        order.insert(dest, cell_id)


@dataclass(frozen=True, slots=True)
class OpsApplied:
    """What :func:`apply_ops` did. ``outcome`` is ``ok`` (``delta`` is the
    canonical bytes to commit) or ``dup`` (the batch changed nothing)."""

    outcome: str
    delta: bytes
    vv: bytes
    projection: dict[str, Any]
    result: dict[str, Any]


def _cell_after(doc: LoroDoc, cell_id: str, order: list[str]) -> dict[str, Any]:
    cell = read_cell(doc, cell_id)
    if cell is None:  # pragma: no cover - a touched cell exists
        return {"id": cell_id, "index": None, "kind": "python", "name": "_", "deleted": True}
    return {
        "id": cell_id,
        "index": order.index(cell_id) if cell_id in order else None,
        "kind": cell.kind,
        "name": cell.name,
        "deleted": cell.deleted,
    }


def _caret(doc: LoroDoc, peer: int, cell_id: str, offset: int) -> bytes | None:
    cell_map = child(doc.get_map("cells"), cell_id)
    text = child(cell_map, "source") if isinstance(cell_map, LoroMap) else None
    if not isinstance(text, LoroText):
        return None
    offset = min(offset, int(text.len_unicode))
    cursor = text.get_cursor(offset, Side.Left)
    if cursor is None:
        return None
    encoded = bytes(cursor.encode())
    store = EphemeralStore(AGENT_CARET_MS)
    store.set(str(peer), {"anchor": encoded, "focus": encoded, "cell": cell_id})
    return bytes(store.encode(str(peer)))


def apply_ops(
    cache: core.DocCache,
    *,
    key: str,
    epoch: int,
    log_seq: int,
    peer: int,
    base_vv: bytes | None,
    ops: list[dict[str, Any]],
    strategy: NotebookStrategy = NOTEBOOK,
) -> OpsApplied | None:
    """Apply a batch of operations written by ``peer`` (minted for it alone) on
    a fork at ``base_vv`` (the head when ``None``) and merge it into the head,
    normalized. Atomic: an operation that is refused raises :class:`OpError`
    and nothing is kept. ``None`` on a cache miss."""
    entry = cache.get(key, epoch, log_seq)
    if entry is None:
        return None
    if peer <= core.SERVER_PEER_MAX:
        raise SandboxError("bad_request", "operations are written by a minted peer")
    entry.pending = None
    doc = entry.doc
    before: VersionVector = guard(lambda: doc.oplog_vv, "internal")
    base = before if base_vv is None else core.decode_vv(base_vv)
    if not guard(lambda: before.diff(base).forward.is_empty, "internal"):
        raise SandboxError("bad_base", "the base is not a version of this document")
    own = guard(lambda: before.get_last(peer), "internal")
    if own is not None:
        # A writer that keeps its peer wrote on top of its own earlier
        # operations whatever token it names (it knows what it wrote), and a
        # peer never writes one operation id twice: its base includes them.
        base = core.decode_vv(bytes(base.encode()))
        base.extend_to_include_last_id(loro.ID(peer, own))
    fmt = strategy.load()
    fork: LoroDoc = guard(lambda: doc.fork_at(doc.vv_to_frontiers(base)), "internal")
    fork.peer_id = peer
    start: VersionVector = guard(lambda: fork.oplog_vv, "internal")
    batch = _Batch()
    head_kinds = {c.id: c.kind for c in all_cells(doc).values()}
    base_kinds = {c.id: c.kind for c in all_cells(fork).values()}
    for index, op in enumerate(ops):
        if not isinstance(op, dict):
            raise OpError(index, "invalid_config", "an operation is an object")
        _apply_op(fork, index, op, batch, fmt)
    guard(fork.commit, "internal")
    changed = not guard(lambda: start.diff(fork.oplog_vv).forward.is_empty, "internal")
    merged: LoroDoc = guard(doc.fork, "internal")
    if changed:
        change: bytes = guard(lambda: bytes(fork.export(ExportMode.Updates(start))), "internal")
        status = guard(lambda: merged.import_(change), "internal")
        if core.pending_import(status):  # pragma: no cover - the fork's history is the document's
            raise SandboxError("internal", "the batch depended on history the document lacks")
        merged.peer_id = peer
        if guard(lambda: normalize(merged), "internal"):
            guard(merged.commit, "internal")
        # The notebook's file must stay under its cap: refused as the batch's
        # last operation (the one that completes what passes it).
        if guard(lambda: strategy.grows_past_file_cap(doc, merged), "internal"):
            raise OpError(
                max(len(ops) - 1, 0), "cap_exceeded", "a notebook file holds at most 4 MiB"
            )
        # What the server writes is held to the rules a client's write is.
        spans = core.forward_spans(before, guard(lambda: merged.oplog_vv, "internal"))
        found = core.changes_of(merged, spans, strategy)
        reason = (
            found
            if isinstance(found, str)
            else guard(lambda: strategy.judge(doc, merged, found), "internal")
        )
        if reason:
            raise SandboxError("internal", f"the batch broke the rule {reason!r}")
    order = live_order(merged)
    notices: list[dict[str, Any]] = []
    for cell_id in dict.fromkeys(batch.edited):
        cell = read_cell(merged, cell_id)
        if cell is not None and cell.deleted:
            notices.append(
                {
                    "cell_id": cell_id,
                    "kind": "edited_deleted_cell",
                    "message": "the cell was deleted; the edit is kept in it",
                }
            )
        if head_kinds.get(cell_id) != base_kinds.get(cell_id):
            notices.append(
                {
                    "cell_id": cell_id,
                    "kind": "kind_changed_by_other",
                    "message": f"someone changed the cell to {head_kinds.get(cell_id)}",
                }
            )
    caret = None
    if batch.caret is not None:
        cell_at, offset = batch.caret
        caret = guard(lambda: _caret(merged, peer, cell_at, offset), "internal")
    result = {
        "cells": [_cell_after(merged, c, order) for c in batch.touched],
        "created": batch.created,
        "touched": batch.touched,
        "notices": notices,
        "caret": None if caret is None else caret.hex(),
    }
    vv = core.encode_vv(merged)
    projection = guard(lambda: strategy.project(merged), "internal")
    if not changed:
        return OpsApplied(outcome="dup", delta=b"", vv=vv, projection=projection, result=result)
    delta: bytes = guard(lambda: bytes(merged.export(ExportMode.Updates(before))), "internal")
    entry.pending = (log_seq + 1, merged, delta)
    return OpsApplied(outcome="ok", delta=delta, vv=vv, projection=projection, result=result)


def rebase_ops(
    cache: core.DocCache,
    *,
    key: str,
    epoch: int,
    log_seq: int,
    peer: int,
    ops: list[dict[str, Any]],
    tail_snapshot: bytes,
    base_vv: bytes,
    next_base_vv: bytes,
    strategy: NotebookStrategy = NOTEBOOK,
) -> OpsApplied | None:
    """Apply a batch whose token names an epoch the history has restarted
    from, and carry it into the current one cell by cell.

    The batch is applied exactly as it would have been then: on the old
    epoch's last state (``tail_snapshot``) at the version its token names, and
    merged there with everything written in the old epoch after it. That
    state, rendered, is then merged into the current epoch from the version
    whose content is the old epoch's last (``next_base_vv``): each cell the
    batch changed is diffed into the same cell (a restart keeps the ids), and
    everything typed since the restart stays. ``peer`` is minted for this
    batch alone. ``None`` on a cache miss."""
    entry = cache.get(key, epoch, log_seq)
    if entry is None:
        return None
    if peer <= core.SERVER_PEER_MAX:
        raise SandboxError("bad_request", "operations are written by a minted peer")
    old = core.new_doc()
    status = guard(lambda: old.import_(tail_snapshot), "bad_request")
    if core.pending_import(status):
        raise SandboxError("bad_request", "the old epoch's state is incomplete")
    ended: VersionVector = guard(lambda: old.oplog_vv, "internal")
    base = core.decode_vv(base_vv)
    if not guard(lambda: ended.diff(base).forward.is_empty, "internal"):
        raise SandboxError("bad_base", "the base is not a version of the old epoch")
    fmt = strategy.load()
    fork: LoroDoc = guard(lambda: old.fork_at(old.vv_to_frontiers(base)), "internal")
    fork.peer_id = peer
    start: VersionVector = guard(lambda: fork.oplog_vv, "internal")
    base_kinds = {c.id: c.kind for c in all_cells(fork).values()}
    head_kinds = {c.id: c.kind for c in all_cells(entry.doc).values()}
    batch = _Batch()
    for index, op in enumerate(ops):
        if not isinstance(op, dict):
            raise OpError(index, "invalid_config", "an operation is an object")
        _apply_op(fork, index, op, batch, fmt)
    guard(fork.commit, "internal")
    if guard(lambda: start.diff(fork.oplog_vv).forward.is_empty, "internal"):
        order = live_order(entry.doc)
        result: dict[str, Any] = {
            "cells": [
                _cell_after(entry.doc, c, order) for c in batch.touched if read_cell(entry.doc, c)
            ],
            "created": [],
            "touched": [],
            "notices": [],
            "caret": None,
            "rebased": True,
        }
        return OpsApplied(
            outcome="dup",
            delta=b"",
            vv=core.encode_vv(entry.doc),
            projection=guard(lambda: strategy.project(entry.doc), "internal"),
            result=result,
        )
    change: bytes = guard(lambda: bytes(fork.export(ExportMode.Updates(start))), "internal")
    later: LoroDoc = guard(old.fork, "internal")
    guard(lambda: later.import_(change), "internal")
    text = guard(lambda: strategy.render(later), "internal")
    merged = core.merge(
        cache,
        key=key,
        epoch=epoch,
        log_seq=log_seq,
        rules=strategy,
        # The old epoch's copy above lives only in this call: the same
        # minted peer is new to the current epoch.
        peer=peer,
        base_vv=next_base_vv,
        text=text,
        keep=False,
    )
    if merged is None:  # pragma: no cover - the entry was found above
        return None
    if merged.outcome == "dup":
        return OpsApplied(
            outcome="dup",
            delta=b"",
            vv=merged.vv,
            projection=guard(lambda: strategy.project(entry.doc), "internal"),
            result={"cells": [], "created": [], "touched": [], "notices": [], "caret": None},
        )
    if merged.outcome != "ok":
        raise SandboxError("reject", merged.reason or "rejected")
    pending = entry.pending
    if pending is None:  # pragma: no cover - a merge that says ok leaves it
        raise SandboxError("internal", "the merge left nothing to commit")
    now = pending[1]
    order = live_order(now)
    notices: list[dict[str, Any]] = []
    for cell_id in dict.fromkeys(batch.edited):
        cell = read_cell(now, cell_id)
        if cell is not None and cell.deleted:
            notices.append(
                {
                    "cell_id": cell_id,
                    "kind": "edited_deleted_cell",
                    "message": "the cell was deleted; the edit is kept in it",
                }
            )
        if head_kinds.get(cell_id) != base_kinds.get(cell_id):
            notices.append(
                {
                    "cell_id": cell_id,
                    "kind": "kind_changed_by_other",
                    "message": f"someone changed the cell to {head_kinds.get(cell_id)}",
                }
            )
    result = {
        "cells": [_cell_after(now, c, order) for c in batch.touched if read_cell(now, c)],
        "created": batch.created,
        "touched": batch.touched,
        "notices": notices,
        "caret": None,
        "rebased": True,
    }
    return OpsApplied(
        outcome="ok", delta=merged.delta, vv=merged.vv, projection=merged.projection, result=result
    )


def rebase_update(
    cache: core.DocCache,
    *,
    key: str,
    epoch: int,
    log_seq: int,
    peer: int,
    tail_snapshot: bytes,
    update: bytes,
    next_base_vv: bytes,
    strategy: NotebookStrategy = NOTEBOOK,
) -> OpsApplied | None:
    """Carry an editor's update that never reached the epoch it was written
    in (the history restarted first) into the current epoch, cell by cell.

    The update is judged on the old epoch's last state as any update is on
    the document (the same paths, the same post-state checks), imported
    there, and the result merged into the current epoch from the version
    whose content is that last state (see :func:`rebase_ops`). A container's
    identity changes across epochs, so nothing of the update is replayed as
    Loro operations: what it changed is diffed in per cell, by id. ``reject``
    with the rule it broke; ``resync`` when it depends on history even the old
    epoch's last state lacks. ``None`` on a cache miss."""
    entry = cache.get(key, epoch, log_seq)
    if entry is None:
        return None
    if peer <= core.SERVER_PEER_MAX:
        raise SandboxError("bad_request", "a rebase is written by a minted peer")
    old = core.new_doc()
    status = guard(lambda: old.import_(tail_snapshot), "bad_request")
    if core.pending_import(status):
        raise SandboxError("bad_request", "the old epoch's state is incomplete")
    ended: VersionVector = guard(lambda: old.oplog_vv, "internal")
    later: LoroDoc = guard(old.fork, "internal")
    try:
        imported = guard(lambda: later.import_(update), "reject")
    except SandboxError:
        return _refused("undecodable", entry.doc, strategy)
    if core.pending_import(imported):
        return _refused("resync", entry.doc, strategy, outcome="resync")
    spans = core.forward_spans(ended, guard(lambda: later.oplog_vv, "reject"))
    if not spans:
        return _refused("", entry.doc, strategy, outcome="dup")
    found = core.changes_of(later, spans, strategy)
    reason = (
        found
        if isinstance(found, str)
        else guard(lambda: strategy.judge(old, later, found), "reject")
    )
    if reason:
        return _refused(reason, entry.doc, strategy)
    touched = (
        [] if isinstance(found, str) else strategy.describe(old, later, found).get("touched", [])
    )
    text = guard(lambda: strategy.render(later), "internal")
    merged = core.merge(
        cache,
        key=key,
        epoch=epoch,
        log_seq=log_seq,
        rules=strategy,
        peer=peer,
        base_vv=next_base_vv,
        text=text,
        keep=False,
    )
    if merged is None:  # pragma: no cover - the entry was found above
        return None
    if merged.outcome == "dup":
        return _refused("", entry.doc, strategy, outcome="dup")
    if merged.outcome != "ok":
        return _refused(merged.reason or "rejected", entry.doc, strategy)
    return OpsApplied(
        outcome="ok",
        delta=merged.delta,
        vv=merged.vv,
        projection=merged.projection,
        result={"touched": touched, "reason": ""},
    )


def _refused(
    reason: str, doc: LoroDoc, strategy: NotebookStrategy, *, outcome: str = "reject"
) -> OpsApplied:
    return OpsApplied(
        outcome=outcome,
        delta=b"",
        vv=core.encode_vv(doc),
        projection={},
        result={"touched": [], "reason": reason},
    )


def view(
    cache: core.DocCache,
    *,
    key: str,
    epoch: int,
    log_seq: int,
    frontier: bytes | None,
    strategy: NotebookStrategy = NOTEBOOK,
) -> tuple[dict[str, Any], bytes] | None:
    """The live document as the notebook view reads it (the normal form,
    every live cell with its source), its version vector, and whether it
    covers ``frontier``. Reads the worker's position (``log_seq`` or past it).
    ``None`` on a cache miss."""
    entry = cache.at_least(key, epoch, log_seq)
    if entry is None:
        return None
    doc = entry.doc
    cells = all_cells(doc)
    order = live_order(doc, cells)
    first_setup = order[0] if order and cells[order[0]].kind == "setup" else None
    covered = True
    if frontier is not None:
        try:
            wanted = core.decode_vv(frontier)
        except SandboxError:
            # Not a vector this store can read: no frontier to wait for.
            wanted = None
        if wanted is not None:
            covered = guard(lambda: doc.oplog_vv.diff(wanted).forward.is_empty, "internal")
    meta = doc.get_map("meta")
    found = {
        "format": as_str(child(meta, "format"), "1.0"),
        "settings": settings_of(doc),
        "covered": covered,
        "cells": [
            {
                "id": cell_id,
                "index": index,
                "kind": effective_kind(cells[cell_id], first_setup),
                "name": cells[cell_id].name,
                "source": cells[cell_id].source,
                "config": cells[cell_id].config,
                "meta": cells[cell_id].meta,
                "extra": cells[cell_id].extra,
            }
            for index, cell_id in enumerate(order)
        ],
    }
    return found, core.encode_vv(doc)


def normalize_cached(
    cache: core.DocCache,
    *,
    key: str,
    epoch: int,
    log_seq: int,
    peer: int,
    strategy: NotebookStrategy = NOTEBOOK,
) -> OpsApplied | None:
    """Normalize the cached document as the server peer ``peer``: ``dup``
    when it is in normal form already. ``None`` on a cache miss."""
    entry = cache.get(key, epoch, log_seq)
    if entry is None:
        return None
    if peer <= core.SERVER_PEER_MAX:
        raise SandboxError("bad_request", "normalization is written by a minted peer")
    entry.pending = None
    doc = entry.doc
    before: VersionVector = guard(lambda: doc.oplog_vv, "internal")
    merged: LoroDoc = guard(doc.fork, "internal")
    merged.peer_id = peer
    wrote = guard(lambda: normalize(merged), "internal")
    vv = core.encode_vv(doc)
    if not wrote:
        return OpsApplied(outcome="dup", delta=b"", vv=vv, projection={}, result={})
    guard(merged.commit, "internal")
    delta: bytes = guard(lambda: bytes(merged.export(ExportMode.Updates(before))), "internal")
    entry.pending = (log_seq + 1, merged, delta)
    return OpsApplied(
        outcome="ok",
        delta=delta,
        vv=core.encode_vv(merged),
        projection=guard(lambda: strategy.project(merged), "internal"),
        result={},
    )


def graph(
    cache: core.DocCache,
    *,
    key: str,
    epoch: int,
    log_seq: int,
    strategy: NotebookStrategy = NOTEBOOK,
) -> tuple[dict[str, Any], bytes] | None:
    """The dependency graph's diagnostics for the document (the format API's
    ``analyze``) at the worker's position, and the version vector there.
    ``None`` on a cache miss."""
    entry = cache.at_least(key, epoch, log_seq)
    if entry is None:
        return None
    fmt = strategy.load()
    analyzed = fmt.analyze(strategy.ir(entry.doc))
    return (analyzed if isinstance(analyzed, dict) else {}), core.encode_vv(entry.doc)


__all__ = [
    "OpError",
    "OpsApplied",
    "apply_ops",
    "graph",
    "normalize_cached",
    "rebase_ops",
    "rebase_update",
    "view",
]
