"""A notebook as the drive stores it, and what is stored beside it.

The saved outputs live in the session folder (``__marimo__/session/`` next to
the file). :func:`read_stored` reads the file and its saved snapshot from the
drive's head versions and nothing else: no live document, no kernel, no box.
That is what a preview of the file shows, so it is what a reader who may only
see the file gets."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from typing import Final

from alkera_core.authz import ActingContext
from alkera_core.config import settings
from alkera_core.files.ids import NodeId
from alkera_core.models.files.tree import FileNode
from alkera_notebook.engine.models import StoredNotebook
from alkera_notebook.outputs import InlineImage, inline_image, parse_snapshot, stored_notebook
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.files import FilesContext, NotEditableError, read_head
from backend.services.notebooks.callers import Target

#: The folders, from the notebook's own, that hold its saved session.
SESSION_FOLDERS: Final = (b"__marimo__", b"session")
#: The extensions an output blob may be stored under, by what it holds.
BLOB_EXTENSIONS: Final = frozenset(
    {"png", "jpg", "jpeg", "gif", "webp", "svg", "json", "txt", "html", "arrow", "parquet", "js"}
)
#: The largest saved snapshot read for a preview. Outputs over the engine's
#: blob threshold are stored out of line, so a snapshot past this is unusual.
SNAPSHOT_MAX_BYTES: Final = 16 * 1024 * 1024


async def beside(
    files: FilesContext,
    target: Target,
    folders: Sequence[bytes],
    pick: Callable[[FileNode], bool],
) -> FileNode | None:
    """The first file ``pick`` takes in the folder ``folders`` names from the
    notebook's own folder, or ``None`` when a folder or the file is missing."""
    node = target.node
    if node.parent_id is None:
        return None
    async with files.repo.transaction():
        parent_id = NodeId(uuid.UUID(str(node.parent_id)))
        for name in folders:
            found = next(
                (
                    child
                    for child in await files.repo.siblings(parent_id)
                    if bytes(child.name) == name and child.kind == "folder"
                ),
                None,
            )
            if found is None:
                return None
            parent_id = NodeId(uuid.UUID(str(found.id)))
        for child in await files.repo.siblings(parent_id):
            if child.kind == "file" and pick(child):
                return child
    return None


async def snapshot_node(files: FilesContext, target: Target) -> FileNode | None:
    """The saved snapshot ``__marimo__/session/<notebook>.json`` beside the
    notebook, or ``None``."""
    name = bytes(target.node.name) + b".json"
    return await beside(files, target, SESSION_FOLDERS, lambda child: bytes(child.name) == name)


async def blob_node(
    files: FilesContext, target: Target, sha256: str, *, only_ext: str | None = None
) -> FileNode | None:
    """The stored output ``__marimo__/session/<notebook>.d/<sha256>.<ext>``
    beside the notebook, located by hash alone (the extension from the
    allowlist), or ``None``."""

    def pick(child: FileNode) -> bool:
        stem, dot, ext = bytes(child.name).decode("utf-8", "replace").partition(".")
        return (
            bool(dot)
            and stem == sha256
            and ext in BLOB_EXTENSIONS
            and (only_ext is None or ext == only_ext)
        )

    folders = [*SESSION_FOLDERS, bytes(target.node.name) + b".d"]
    return await beside(files, target, folders, pick)


async def saved_image(
    db: AsyncSession, ctx: ActingContext, snapshot: FileNode, sha256: str
) -> InlineImage | None:
    """The raster image ``sha256`` names among the outputs ``snapshot`` (a
    saved snapshot the caller may read) carries inline, or ``None``. An
    output stored out of line is a file of its own and is not looked for
    here."""
    try:
        saved = await read_head(db, ctx, uuid.UUID(str(snapshot.id)), max_bytes=SNAPSHOT_MAX_BYTES)
    except NotEditableError:
        return None
    read = parse_snapshot(saved.text, lambda _ref: (False, None))
    return inline_image((bundle for cell in read.cells for bundle in cell.outputs.bundles), sha256)


async def read_stored(
    db: AsyncSession, ctx: ActingContext, target: Target, snapshot: FileNode | None
) -> StoredNotebook:
    """The notebook's cells as its head version holds them, with the outputs
    ``snapshot`` (a node the caller may read, or ``None``) saved for them.
    Raises :class:`NotEditableError` when the notebook itself cannot be read
    as text; a snapshot that cannot be read only loses the outputs, and says
    so in a notice."""
    head = await read_head(db, ctx, target.item_id, max_bytes=settings.realtime_doc_max_bytes)
    if snapshot is None:
        return stored_notebook(head.text, None)
    try:
        saved = await read_head(db, ctx, uuid.UUID(str(snapshot.id)), max_bytes=SNAPSHOT_MAX_BYTES)
    except NotEditableError as exc:
        return stored_notebook(head.text, None, notices=[f"outputs_unreadable: {exc.reason}"])
    return stored_notebook(head.text, saved.text)


__all__ = [
    "BLOB_EXTENSIONS",
    "SESSION_FOLDERS",
    "SNAPSHOT_MAX_BYTES",
    "beside",
    "blob_node",
    "read_stored",
    "saved_image",
    "snapshot_node",
]
