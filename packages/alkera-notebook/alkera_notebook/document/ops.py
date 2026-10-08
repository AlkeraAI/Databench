"""Operations on a notebook document and their results.

A batch of operations is atomic: every op applies or none does. A refused
batch raises :class:`NotebookOpError` naming the index of the op that failed
and one of the codes in :data:`OP_ERROR_CODES`.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

CellKind = Literal["setup", "python", "function", "class", "sql", "markdown", "unparsable"]

#: A cell's run status relative to the current kernel.
CellStatus = Literal[
    "fresh",
    "edited",
    "stale",
    "not_run",
    "queued",
    "running",
    "error",
    "interrupted",
    "skipped",
    "stopped",
    "disabled",
]
CELL_KINDS: tuple[str, ...] = (
    "setup",
    "python",
    "function",
    "class",
    "sql",
    "markdown",
    "unparsable",
)

SUBMIT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,48}$")

OpErrorCode = Literal[
    "cell_not_found",
    "edit_not_found",
    "edit_ambiguous",
    "invalid_name",
    "unknown_kind",
    "invalid_config",
    "setup_must_be_first",
    "cap_exceeded",
    "not_representable",
]
OP_ERROR_CODES: tuple[str, ...] = (
    "cell_not_found",
    "edit_not_found",
    "edit_ambiguous",
    "invalid_name",
    "unknown_kind",
    "invalid_config",
    "setup_must_be_first",
    "cap_exceeded",
    "not_representable",
)


class _Op(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InsertCell(_Op):
    op: Literal["insert"] = "insert"
    kind: str = "python"
    source: str = ""
    name: str = "_"
    after: str | None = None
    before: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    meta: dict[str, Any] = Field(default_factory=dict)


class TextEdit(_Op):
    old: str
    new: str
    occurrence: int | None = None


class EditCell(_Op):
    op: Literal["edit"] = "edit"
    cell_id: str
    edits: list[TextEdit] = Field(min_length=1, max_length=100)


class ReplaceCell(_Op):
    op: Literal["replace"] = "replace"
    cell_id: str
    source: str


class DeleteCell(_Op):
    op: Literal["delete"] = "delete"
    cell_id: str


class RestoreCell(_Op):
    op: Literal["restore"] = "restore"
    cell_id: str
    after: str | None = None


class MoveCell(_Op):
    op: Literal["move"] = "move"
    cell_id: str
    after: str | None = None
    before: str | None = None


class SetCellName(_Op):
    op: Literal["rename"] = "rename"
    cell_id: str
    name: str


class SetCellKind(_Op):
    op: Literal["set_kind"] = "set_kind"
    cell_id: str
    kind: str


class SetCellConfig(_Op):
    op: Literal["set_config"] = "set_config"
    cell_id: str
    config: dict[str, Any]


class SetCellMeta(_Op):
    """Change a SQL or Markdown cell's settings (a SQL cell's ``connection``,
    ``output_var``, ``show_output``; a Markdown cell's ``quote``). Keys not
    named keep their value; ``None`` resets a key to its default (a SQL cell
    with no ``connection`` runs in the notebook's DuckDB)."""

    op: Literal["set_meta"] = "set_meta"
    cell_id: str
    meta: dict[str, Any]


class SetSetting(_Op):
    op: Literal["set_setting"] = "set_setting"
    key: str
    value: Any


NotebookOp = Annotated[
    InsertCell
    | EditCell
    | ReplaceCell
    | DeleteCell
    | RestoreCell
    | MoveCell
    | SetCellName
    | SetCellKind
    | SetCellConfig
    | SetCellMeta
    | SetSetting,
    Field(discriminator="op"),
]


class CellNotice(BaseModel):
    """Something a reader of the document should know about a cell.

    ``kind`` is an open string. Core kinds: ``cell_running``,
    ``edited_deleted_cell``, ``external_conflict``, ``kind_changed_by_other``,
    ``upstream_being_edited``. The engine also emits ``stale_base``,
    ``duplicate_name``, ``invalid_file``,
    ``file_deleted``, ``corrupt_snapshot``, ``memory_warning``.
    """

    kind: str
    cell_id: str | None = None
    message: str = ""
    by: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class CellAfterOp(BaseModel):
    """A cell a batch touched, as it is after the batch.

    ``index`` is its place among the live cells, ``None`` once it is deleted.
    ``status`` is its run status when the writer knows it: the engine's client
    fills it; a document store, which holds no run state, leaves it ``None``.
    """

    id: str
    name: str
    kind: str
    index: int | None
    deleted: bool = False
    status: CellStatus | None = None


def error_label(item: Any) -> str:
    """A name, or a graph error as ``code`` or ``code: name`` (the format
    reports errors as ``{code, name?, cells?}`` objects)."""
    if isinstance(item, GraphErrorInfo):
        return item.label()
    if isinstance(item, dict):
        code = str(item.get("code", "error"))
        name = item.get("name")
        return f"{code}: {name}" if name else code
    return str(item)


class GraphErrorInfo(BaseModel):
    """A problem in the dependency graph: its code (``multiple_definitions``,
    ``cycle``, ``syntax``, ``delete_nonlocal``, ...), the name it is about,
    and the cells it involves. The one graph error model: ``GraphSummary``,
    ``GraphView``, the cells of a view and the ``graph`` event all carry it."""

    code: str
    name: str | None = None
    cells: list[str] = Field(default_factory=list)

    @classmethod
    def parse(cls, item: Any) -> GraphErrorInfo:
        """The format's ``{code, name?, cells?}`` object, or a bare code."""
        if isinstance(item, GraphErrorInfo):
            return item
        if isinstance(item, Mapping):
            name = item.get("name")
            return cls(
                code=str(item.get("code") or "error"),
                name=str(name) if name else None,
                cells=[str(c) for c in item.get("cells") or ()],
            )
        return cls(code=str(item))

    def label(self) -> str:
        """One line: ``code``, or ``code: name``."""
        return f"{self.code}: {self.name}" if self.name else self.code


class GraphCellSummary(BaseModel):
    defs: list[str] = Field(default_factory=list)
    refs: list[str] = Field(default_factory=list)
    errors: list[GraphErrorInfo] = Field(default_factory=list)


class GraphSummary(BaseModel):
    """The document graph (current text): for diagnostics only.

    ``computed`` is false when the writer had no analysis of exactly the state
    it answers for (the platform analyses off the hot path, and the ``graph``
    event on the notebook channel carries the analysis once it is ready);
    ``cells`` and ``edges`` are empty then."""

    computed: bool = True
    cells: dict[str, GraphCellSummary] = Field(default_factory=dict)
    edges: list[tuple[str, str]] = Field(default_factory=list)

    @classmethod
    def pending(cls) -> GraphSummary:
        return cls(computed=False)

    @classmethod
    def of_analysis(cls, graph: Mapping[str, Any]) -> GraphSummary:
        """The summary of the format API's ``analyze`` / ``analyze_code``
        result (``{"cells": {id: {defs, refs, errors}}, "edges": [[a, b]]}``)."""
        raw_cells = graph.get("cells")
        cells: dict[str, GraphCellSummary] = {}
        if isinstance(raw_cells, Mapping):
            for cell_id, info in raw_cells.items():
                if not isinstance(info, Mapping):
                    continue
                cells[str(cell_id)] = GraphCellSummary(
                    defs=[str(d) for d in info.get("defs") or []],
                    refs=[str(r) for r in info.get("refs") or []],
                    errors=[GraphErrorInfo.parse(e) for e in info.get("errors") or []],
                )
        edges = [
            (str(edge[0]), str(edge[1]))
            for edge in graph.get("edges") or []
            if isinstance(edge, list | tuple) and len(edge) == 2
        ]
        return cls(computed=True, cells=cells, edges=edges)


class NotebookOpsResult(BaseModel):
    token: str
    repeat: bool
    cells: list[CellAfterOp]
    created: list[str]
    notices: list[CellNotice]
    graph: GraphSummary


class NotebookOpError(Exception):
    """A batch was refused. Nothing in it was applied."""

    def __init__(self, index: int, code: str, message: str) -> None:
        super().__init__(f"op {index}: {code}: {message}")
        self.index = index
        self.code = code
        self.message = message

    def to_dict(self) -> dict[str, Any]:
        return {"index": self.index, "code": self.code, "message": self.message}
