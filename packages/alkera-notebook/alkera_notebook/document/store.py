"""The document store seam.

The engine reads and edits notebooks only through a :class:`DocumentStore`.
The core implementation is :class:`~alkera_notebook.document.file_store.FileDocumentStore`
(one in-memory document per path, the file written after every batch); the
platform implements the same protocol over its Loro documents.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Literal, Protocol

if TYPE_CHECKING:
    from alkera_notebook.document.model import Document
    from alkera_notebook.document.ops import CellNotice, NotebookOp, NotebookOpsResult
    from alkera_notebook.engine.models import Actor


@dataclass(frozen=True)
class StoredNotebook:
    path: str
    token: str
    document: Document
    notices: Sequence[CellNotice] = ()


@dataclass(frozen=True)
class CodeSnapshot:
    """Each live cell's current code at one token, in document order."""

    token: str
    cells: Sequence[tuple[str, str]]  # (cell_id, code)


@dataclass(frozen=True)
class EditingInfo:
    """Someone who is in a cell now, by the rule in ``document/editing.py``."""

    actor_id: str
    display_name: str
    kind: Literal["person", "agent", "system"]
    at: datetime
    #: Their caret stands in the cell (True), or they publish no caret and
    #: edited it lately (False).
    caret: bool = False


@dataclass(frozen=True)
class DocumentChange:
    path: str
    token: str
    actor_id: str | None
    cell_ids: Sequence[str] = field(default_factory=tuple)
    notices: Sequence[CellNotice] = field(default_factory=tuple)
    # "ops": an apply through the store; "external": the file changed outside;
    # "deleted": the file is gone.
    origin: Literal["ops", "external", "deleted"] = "ops"


class DocumentStore(Protocol):
    async def load(self, path: str) -> StoredNotebook: ...

    async def apply(
        self,
        path: str,
        ops: Sequence[NotebookOp],
        base_token: str | None,
        actor: Actor,
        submit_id: str | None,
    ) -> NotebookOpsResult: ...

    async def snapshot(self, path: str, cell_ids: Sequence[str] | None = None) -> CodeSnapshot: ...

    async def editing(self, path: str) -> dict[str, list[EditingInfo]]:
        """Who is in each cell now: ``editing_now`` (``document/editing.py``)
        over the claims this store holds."""
        ...

    def changes(self, path: str) -> AsyncIterator[DocumentChange]:
        """Changes to ``path`` from the moment of the call (not of the first
        iteration): the engine subscribes, then catches up with one load."""
        ...
