"""The in-memory form of an ``.alknb.py`` notebook.

These types are the format's public data model: :func:`read` returns them,
:func:`write` consumes them, and every other part of the platform (the live
document, the engine, the agent tools) exchanges notebooks through them.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal, TypedDict

FORMAT_VERSION = "1.0"
"""The format version this library reads and writes."""

Kind = Literal["setup", "python", "function", "class", "sql", "markdown", "unparsable"]
KINDS: tuple[Kind, ...] = ("setup", "python", "function", "class", "sql", "markdown", "unparsable")

Resolution = Literal["keyword", "exact", "similar", "minted", "new"]
"""How a cell got its id: its own ``alkera_id`` keyword; an exact or similar
match against the known prior state; deterministic minting; or ``new`` for a
cell an editor created (never returned by :func:`read`)."""

#: Why a file opens read only. ``newer_format``: a later major format than this
#: reader knows. ``not_a_notebook``: no notebook could be read from the text (a
#: plain script, an empty file, ``marimo.App(`` left open), so it holds no cells
#: and saving the document would replace the file's text with an empty notebook.
#: ``unreadable``: the reader itself failed.
ReadOnlyReason = Literal["newer_format", "not_a_notebook", "unreadable"]

_EMPTY: Mapping[str, Any] = MappingProxyType({})


def _frozen(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType(dict(value))


@dataclass(frozen=True)
class Violation:
    """Something in the file that does not follow the format.

    Reading never fails; it reports what it had to tolerate. ``code`` is a
    stable machine name (see :data:`VIOLATION_CODES`), ``line`` is 1-based in
    the text given to :func:`read` (0 when the problem has no single line).
    """

    code: str
    line: int
    message: str


@dataclass(frozen=True)
class CellIR:
    """One cell.

    ``code`` is the cell's Python code exactly as the file holds it (marimo's
    parser's view: the function body or the setup block, dedented).
    ``source`` is the text an editor shows: the SQL or Markdown text for those
    kinds, the code otherwise. ``config`` holds marimo's cell configuration
    keys that differ from their defaults; ``meta`` the kind's metadata (SQL:
    ``output_var``, ``connection``, ``engine``, ``show_output``; Markdown:
    ``quote``; unparsable: ``verbatim``, the cell's text as read, and
    ``stray: True`` for a statement marimo does not treat as a cell); ``extra``
    every other ``alkera_*`` keyword, in file order.
    """

    id: str
    kind: Kind
    name: str
    source: str
    code: str
    config: Mapping[str, Any] = field(default_factory=lambda: _EMPTY)
    meta: Mapping[str, Any] = field(default_factory=lambda: _EMPTY)
    extra: Mapping[str, Any] = field(default_factory=lambda: _EMPTY)
    resolution: Resolution = "new"

    def __post_init__(self) -> None:
        for name in ("config", "meta", "extra"):
            value = getattr(self, name)
            if not isinstance(value, MappingProxyType):
                object.__setattr__(self, name, _frozen(value))


@dataclass(frozen=True)
class NotebookIR:
    """A whole notebook.

    ``header_text`` is everything before ``import marimo`` except the settings
    fence, verbatim, with trailing blank lines removed (empty when there is
    none). ``settings`` holds the known settings keys with their effective
    values (defaults filled in; ``env`` only when present; ``format`` is its own
    field). ``unknown_settings`` is the TOML text of keys this reader does not
    know, kept for the writer. ``set_settings`` names the known keys the file's
    own fence sets (to a valid value); :meth:`file_settings` is those values,
    which is what a document of the file holds. ``app_config`` holds ``marimo.App`` keywords as
    written. ``read_only_reason`` is ``"newer_format"`` when the file declares a
    newer major format than this reader's; such a notebook must not be saved.
    """

    format: str = FORMAT_VERSION
    header_text: str = ""
    settings: Mapping[str, Any] = field(default_factory=lambda: _EMPTY)
    unknown_settings: str = ""
    set_settings: frozenset[str] = frozenset()
    app_config: Mapping[str, Any] = field(default_factory=lambda: _EMPTY)
    generated_with: str = ""
    cells: Sequence[CellIR] = ()
    violations: Sequence[Violation] = ()
    read_only_reason: ReadOnlyReason | None = None

    def __post_init__(self) -> None:
        for name in ("settings", "app_config"):
            value = getattr(self, name)
            if not isinstance(value, MappingProxyType):
                object.__setattr__(self, name, _frozen(value))
        if not isinstance(self.cells, tuple):
            object.__setattr__(self, "cells", tuple(self.cells))
        if not isinstance(self.violations, tuple):
            object.__setattr__(self, "violations", tuple(self.violations))


def file_settings(ir: Any) -> dict[str, Any]:
    """The settings ``ir``'s file itself sets, which is what a document of the
    file holds: an inherited or default value is never one. An IR from a
    reader that predates ``set_settings`` keeps every setting it names."""
    named = getattr(ir, "set_settings", None)
    values = dict(ir.settings)
    if named is None:
        return values
    return {key: value for key, value in values.items() if key in named}


class GraphError(TypedDict, total=False):
    """A graph problem, naming what it is about.

    ``code`` is one of ``multiple_definitions`` (``name`` is defined by every
    cell in ``cells``, this one included), ``cycle`` (``cells`` are the members
    of the cycle), ``delete_nonlocal`` (this cell deletes ``name``, which
    ``cells`` define) and ``syntax_error``. Lists are sorted.
    """

    code: str
    name: str
    cells: list[str]


class CellGraph(TypedDict):
    defs: list[str]
    refs: list[str]
    sql_tables: list[str]
    errors: list[GraphError]


class GraphJSON(TypedDict):
    """``{"cells": {id: {defs, refs, sql_tables, errors}}, "edges": [[a, b]]}``.

    Canonical: every list sorted, so two analyses of the same notebook are
    byte-identical as JSON. An edge ``[a, b]`` means ``b`` refers to a name
    ``a`` defines.
    """

    cells: dict[str, CellGraph]
    edges: list[list[str]]


VIOLATION_CODES: Mapping[str, str] = MappingProxyType(
    {
        # The file as a whole
        "not_a_notebook": "the file has no `import marimo` and `marimo.App` definition",
        "byte_order_mark": "a byte order mark was removed",
        "line_endings": "CR or CRLF line endings were normalised to LF",
        "header_statement": "an executable statement before `import marimo` (kept as written)",
        "body_statement": "a statement between cells that is not a cell (dropped on write)",
        "marimo_import_alias": "`marimo` imported under another name",
        "generated_with": "`__generated_with` is missing or malformed",
        "run_guard": 'the `if __name__ == "__main__"` guard is missing',
        "app_keyword": "a `marimo.App` keyword whose value is not a constant",
        # The settings fence
        "settings_missing": "no `# >>> alkera` settings block",
        "settings_unclosed": "a `# >>> alkera` line without its `# <<< alkera`",
        "settings_line": "a line inside the settings block that is not a comment",
        "settings_duplicate": "a second settings block (kept as comments)",
        "settings_toml": "the settings block is not valid TOML",
        "settings_value": "a known setting with a value of the wrong type (default used)",
        "format_version": "the `format` setting is not `MAJOR.MINOR`",
        "newer_format": "the file declares a newer major format; read only",
        # Cells
        "cell_keyword": "a cell keyword whose value is not a literal constant",
        "id_missing": "a cell without an `alkera_id` keyword",
        "id_malformed": "an `alkera_id` that is not ten lower-case Crockford base32 characters",
        "id_duplicate": "an `alkera_id` used by more than one cell",
        "setup_not_first": "a setup cell after other cells",
        "syntax_error": "a cell whose code this reader cannot parse",
        "decorator_between": "something between a cell's decorator and its definition",
    }
)
