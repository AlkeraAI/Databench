"""What a batch of ops changed, for the activity feed.

Pure: two documents (before and after a batch) and the batch's ops in, the
changes out. Only cells whose state the batch actually changed are named: an
op that leaves its cell as it was (a delete of a deleted cell, a move to where
the cell already is, a rename to the same name) changes nothing and names
nothing. Cells the ops do not name are never listed, so a change someone else
made at the same moment is not attributed to this batch.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from alkera_notebook.document.model import DocCell, Document
from alkera_notebook.document.ops import (
    DeleteCell,
    EditCell,
    InsertCell,
    MoveCell,
    NotebookOp,
    ReplaceCell,
    RestoreCell,
    SetCellConfig,
    SetCellKind,
    SetCellMeta,
    SetCellName,
    SetSetting,
)
from alkera_notebook.engine.models import CellChange

_OP_CHANGE: dict[type, CellChange] = {
    EditCell: "edit",
    ReplaceCell: "edit",
    DeleteCell: "delete",
    RestoreCell: "restore",
    MoveCell: "move",
    SetCellName: "rename",
    SetCellKind: "kind",
    SetCellConfig: "config",
    SetCellMeta: "config",
}


@dataclass
class BatchChanges:
    cells: list[tuple[CellChange, list[str]]] = field(default_factory=list)
    """Each kind of cell change with the cells it changed, in batch order."""
    settings: list[str] = field(default_factory=list)
    """Setting keys (``header`` for the header text) whose value changed, env excluded."""
    env_changed: bool = False
    """The batch changed which environment the notebook records."""

    @property
    def cell_ids(self) -> list[str]:
        return list(dict.fromkeys(cid for _, ids in self.cells for cid in ids))


def _predecessor(order: Sequence[str], cell_id: str) -> str | None:
    i = order.index(cell_id)
    return order[i - 1] if i > 0 else None


def _changed(change: CellChange, cell_id: str, before: Document, after: Document) -> bool:
    old: DocCell | None = before.cells.get(cell_id)
    new: DocCell | None = after.cells.get(cell_id)
    if old is None or new is None:
        return False
    if change == "edit":
        return (old.source, old.code) != (new.source, new.code)
    if change == "delete":
        return not old.deleted and new.deleted
    if change == "restore":
        return old.deleted and not new.deleted
    if change == "rename":
        return old.name != new.name
    if change == "kind":
        return old.kind != new.kind
    if change == "config":
        return old.config != new.config
    # move: where the cell sits among the cells that were live before.
    if old.deleted or new.deleted:
        return False
    was = [c.id for c in before.live_cells()]
    kept = set(was)
    now = [c.id for c in after.live_cells() if c.id in kept]
    return _predecessor(was, cell_id) != _predecessor(now, cell_id)


def batch_changes(
    before: Document, after: Document, ops: Sequence[NotebookOp], created: Sequence[str]
) -> BatchChanges:
    out = BatchChanges()
    groups: dict[CellChange, list[str]] = {}
    new_ids = [cid for cid in created if cid in after.cells]
    keys: list[str] = []
    for op in ops:
        if isinstance(op, InsertCell):
            if new_ids and "insert" not in groups:
                groups["insert"] = list(new_ids)
            continue
        if isinstance(op, SetSetting):
            keys.append(op.key)
            continue
        change = _OP_CHANGE[type(op)]
        cell_id = op.cell_id
        if cell_id in created or not _changed(change, cell_id, before, after):
            continue
        ids = groups.setdefault(change, [])
        if cell_id not in ids:
            ids.append(cell_id)
    out.cells = [(change, ids) for change, ids in groups.items() if ids]
    for key in dict.fromkeys(keys):
        if key == "header":
            if before.meta.header != after.meta.header:
                out.settings.append(key)
        elif key == "env":
            if before.setting("env") != after.setting("env"):
                out.env_changed = True
        elif before.setting(key) != after.setting(key):
            out.settings.append(key)
    return out


__all__ = ["BatchChanges", "batch_changes"]
