"""A notebook as it is stored, for a reader with no kernel and no live document.

The file's cells come from the format's reader and the outputs from the
snapshot saved beside it, matched to the cells the way a kernel reattaches
them when it opens the notebook. An output stored out of line stays a
reference: the client reads its bytes through the notebook's blob route, under
the same decision as any other read of the folder.
"""

from __future__ import annotations

from typing import Any

from alkera_notebook.document.ops import CellNotice
from alkera_notebook.engine.models import CellState, StoredNotebook
from alkera_notebook.format.reader import read
from alkera_notebook.outputs.snapshot import (
    REF_MIME,
    SnapshotRead,
    parse_snapshot,
    reattach,
    valid_ref,
)


def _keep_reference(ref: Any) -> tuple[bool, Any]:
    """A well-formed reference, left as the reference a client resolves."""
    if valid_ref(ref) is None:
        return False, None
    return True, {REF_MIME: ref}


def _notice(text: str) -> CellNotice:
    kind = text.split(":", 1)[0] if ":" in text else "snapshot_notice"
    return CellNotice(kind=kind, message=text)


def stored_notebook(
    text: str, snapshot: str | bytes | None, *, notices: list[str] | None = None
) -> StoredNotebook:
    """``text`` (the ``.alknb.py`` file) read as cells, each carrying the
    outputs ``snapshot`` (the session snapshot beside it, when there is one)
    saved for it. ``notices`` are the caller's own, said alongside."""
    ir = read(text)
    saved = SnapshotRead() if snapshot is None else parse_snapshot(snapshot, _keep_reference)
    found = reattach(saved, [(cell.id, cell.code) for cell in ir.cells])
    cells: list[CellState] = []
    for index, cell in enumerate(ir.cells):
        outputs = found.get(cell.id)
        cells.append(
            CellState(
                id=cell.id,
                name=cell.name,
                kind=cell.kind,
                index=index,
                status="not_run",
                source=cell.source,
                outputs=outputs.items(cell.id) if outputs is not None else [],
                output_origin=outputs.origin if outputs is not None else None,
                last_run=outputs.attribution() if outputs is not None else None,
                config=dict(cell.config),
                meta=dict(cell.meta),
                extra=dict(cell.extra),
            )
        )
    return StoredNotebook(
        cells=cells,
        read_only_reason=ir.read_only_reason,
        notices=[_notice(note) for note in [*(notices or []), *saved.notices]],
    )


__all__ = ["stored_notebook"]
