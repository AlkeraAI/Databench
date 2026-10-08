"""The rules every notebook op applier follows, owned once.

Three appliers change a notebook's cells: the file store
(``alkera_notebook.document.apply``), the platform's live document (the
backend sandbox's ``notebook_ops``) and the browser's editor. They hold the
document differently (a file, a Loro document, a Loro document in a tab), but
what an op means must not differ: which text an ``edit`` replaces, what a
cell's text becomes when its kind changes, which kinds can be inserted. Those
decisions live here, pure (text and values in, text and values out), and the
shared op vectors (``packages/alkera-notebook/tests/vectors/notebook_ops.json``) hold
every applier to the same cases.

A templated (SQL or Markdown) cell must stay writable as its kind: an op that
would leave one holding text its template cannot write is refused
(``not_representable``), by every applier alike.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from alkera_notebook.format.templates import classify, render_cell

Render = Callable[[str, str, Mapping[str, Any]], str]
Classify = Callable[[str], tuple[str, str, dict[str, Any]]]

#: The kinds an insert or a kind change may name. ``unparsable`` is what a
#: reader makes of code it cannot parse, never something an editor creates.
INSERTABLE_KINDS: frozenset[str] = frozenset(
    {"setup", "python", "function", "class", "sql", "markdown"}
)
#: Kinds whose text is not the code they run as, but rendered through a template.
TEMPLATED_KINDS: frozenset[str] = frozenset({"sql", "markdown"})
#: What no templated kind's text may contain: it would end the template's string.
TRIPLE_QUOTE = '"""'
#: marimo's cell configuration (``set_config``): key -> the type its value has.
CONFIG_TYPES: Mapping[str, type] = {
    "column": int,
    "disabled": bool,
    "hide_code": bool,
    "expand_output": bool,
}
#: The settings each templated kind keeps in its ``meta`` (``set_meta``).
META_KEYS: Mapping[str, frozenset[str]] = {
    "sql": frozenset({"output_var", "connection", "engine", "show_output"}),
    "markdown": frozenset({"quote"}),
}
#: The meta a cell changed to a templated kind starts with.
DEFAULT_META: Mapping[str, Mapping[str, Any]] = {
    "sql": {"output_var": "_df"},
    "markdown": {"quote": "r"},
}


class OpRuleError(ValueError):
    """An op the rules refuse; ``code`` is the op error code every applier reports."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def check_config_keys(config: Mapping[str, Any]) -> None:
    """Refuse a ``set_config`` (or an insert's ``config``) naming a key that is
    not marimo's cell configuration. The refusal names the keys it takes and,
    for a key that is a SQL or Markdown cell setting, says to use ``set_meta``,
    so the caller can correct the op instead of guessing."""
    unknown = [k for k in config if k not in CONFIG_TYPES]
    if not unknown:
        return
    allowed = ", ".join(CONFIG_TYPES)
    for key in unknown:
        kinds = [kind for kind, keys in META_KEYS.items() if key in keys]
        if kinds:
            where = " or ".join("SQL" if kind == "sql" else "Markdown" for kind in kinds)
            raise OpRuleError(
                "invalid_config",
                f"{key} is a {where} cell setting: change it with set_meta, not set_config "
                f"(set_config takes {allowed})",
            )
    raise OpRuleError("invalid_config", f"set_config has no key {unknown[0]}; it takes {allowed}")


def locate_edit(text: str, old: str, occurrence: int | None) -> int:
    """Where an exact-text edit replacing ``old`` applies in ``text``.

    Matches are counted without overlap, left to right. ``occurrence`` (from 1)
    picks one; without it the text must occur once. An empty ``old`` names only
    the whole text of an empty cell: anywhere else it is ambiguous."""
    if old == "":
        if text:
            raise OpRuleError("edit_ambiguous", "an empty match is ambiguous in a cell with text")
        return 0
    found: list[int] = []
    at = text.find(old)
    while at >= 0:
        found.append(at)
        at = text.find(old, at + len(old))
    if occurrence is not None:
        if occurrence < 1 or occurrence > len(found):
            raise OpRuleError("edit_not_found", f"occurrence {occurrence} of the text is not there")
        return found[occurrence - 1]
    if not found:
        raise OpRuleError("edit_not_found", "the text to replace is not in the cell")
    if len(found) > 1:
        raise OpRuleError("edit_ambiguous", f"the text to replace is there {len(found)} times")
    return found[0]


def apply_edit(text: str, old: str, new: str, occurrence: int | None) -> tuple[int, str]:
    """``text`` after one exact-text edit, and where the edit applied."""
    at = locate_edit(text, old, occurrence)
    return at, text[:at] + new + text[at + len(old) :]


def check_insertable(kind: str) -> None:
    if kind not in INSERTABLE_KINDS:
        raise OpRuleError("unknown_kind", f"no cell kind {kind!r} can be created")


def templated_code(
    kind: str,
    source: str,
    meta: Mapping[str, Any],
    *,
    render: Render = render_cell,
    classify: Classify = classify,
) -> str:
    """The code a cell of ``kind`` holding ``source`` is written as.

    A SQL or Markdown cell is written through its template, which must read
    back as the same kind and text: text with three double quotes, or meta the
    template cannot write, cannot be that kind, and the op is refused rather
    than the cell turned into Python that would not run (or lose its
    connection). Every other kind is written as its text."""
    if kind not in TEMPLATED_KINDS:
        return source
    if TRIPLE_QUOTE in source:
        raise OpRuleError(
            "not_representable",
            f"a {kind} cell cannot hold three double quotes in a row; use a Python cell",
        )
    code = render(kind, source, dict(meta))
    got_kind, got_source, _ = classify(code)
    if got_kind != kind or got_source != source:
        raise OpRuleError("not_representable", f"this {kind} cell's settings cannot be written")
    return code


def kind_change(
    from_kind: str,
    to_kind: str,
    source: str,
    meta: Mapping[str, Any],
    *,
    render: Render = render_cell,
    classify: Classify = classify,
) -> tuple[str, dict[str, Any]]:
    """The text and meta a cell holds after its kind changes.

    To a templated kind, the code the cell runs as is read the way the reader
    reads a file: code that is already that kind's template gives its text and
    meta (``_df = alkera.sql("...")`` becomes the query); anything else keeps
    its editor text and starts with the kind's default meta. Out of a templated kind,
    the text becomes the code the cell ran as, so a SQL cell made Python runs
    the same query rather than raw SQL Python cannot parse. Text that cannot be
    the new kind (:func:`templated_code`) is refused. ``render`` and
    ``classify`` are the caller's format (the module's own by default)."""
    check_insertable(to_kind)
    code = render(from_kind, source, dict(meta)) if from_kind in TEMPLATED_KINDS else source
    if to_kind in TEMPLATED_KINDS:
        got_kind, got_source, got_meta = classify(code)
        if got_kind == to_kind:
            return got_source, dict(got_meta)
        new_meta = dict(DEFAULT_META[to_kind])
        templated_code(to_kind, source, new_meta, render=render, classify=classify)
        return source, new_meta
    return code, {}


__all__ = [
    "CONFIG_TYPES",
    "DEFAULT_META",
    "INSERTABLE_KINDS",
    "META_KEYS",
    "TEMPLATED_KINDS",
    "TRIPLE_QUOTE",
    "OpRuleError",
    "apply_edit",
    "check_config_keys",
    "check_insertable",
    "kind_change",
    "locate_edit",
    "templated_code",
]
