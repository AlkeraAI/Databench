"""The rendering a retired ``.alkeraquery`` / ``.alkerareport`` still gets.

A saved query or report rendered as a *replication context* folder carrying
everything a fresh chat needs to run the same analysis again. Chat templates
do that job, so nothing creates these folders. The rendering stays because the
conversion that turns a query or report into a chat template writes the README
this module produces into the new template's brief, so retiring the type does
not lose what its author wrote down.

The rendering is pure (plain data in, bytes out), and nothing here needs a
session.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from alkera_core.files.providers.derived_members import CONTEXT_FOLDER_TYPES
from alkera_core.files.providers.rows import canonical_json

__all__ = [
    "CONTEXT_FOLDER_TYPES",
    "LAST_OUTPUT_DIR",
    "README_FILE",
    "SPEC_FILE",
    "context_folder_files",
    "render_readme",
]

SPEC_FILE: Final = "spec.json"
README_FILE: Final = "README.md"
#: Named with the trailing slash the file map uses for "this is a directory".
LAST_OUTPUT_DIR: Final = "last_output/"

#: What a parameter's question falls back to when nobody wrote one. Spelled
#: here rather than left blank so a README never has a silent gap where a
#: question should be — the reader is told to ask *something* about the slot.
_FALLBACK_PROMPT: Final = "Which value for {name}?"


def context_folder_files(document: Mapping[str, Any]) -> dict[str, bytes]:
    """The folder's members, keyed by their path inside it.

    ``document`` is the stamped envelope the rows renderer produces —
    ``{"type", "object", "spec"}`` — so the folder and the single-file
    rendering can never describe different specs. A key ending in ``/`` is a
    directory to create, with no bytes.
    """
    return {
        SPEC_FILE: canonical_json(dict(document)),
        README_FILE: render_readme(document).encode("utf-8"),
        LAST_OUTPUT_DIR: b"",
    }


def render_readme(document: Mapping[str, Any]) -> str:
    """The human-readable form of a replication context.

    Written in the order the work happens: what this is, what to ASK, what it
    connects to, what it does, and how it is written out.
    """
    spec = _mapping(document.get("spec"))
    header = _mapping(document.get("object"))
    object_type = str(document.get("type") or "")
    title = str(spec.get("title") or header.get("title") or "Untitled")

    lines: list[str] = [f"# {title}", ""]
    lines.extend(_intro(object_type))
    lines.extend(_questions_section(spec))
    lines.extend(_connections_section(object_type, spec))
    lines.extend(_steps_section(object_type, spec))
    lines.extend(_rendering_section(spec))
    return "\n".join(lines).rstrip() + "\n"


def _intro(object_type: str) -> list[str]:
    what = "saved query" if object_type == "query" else "report"
    return [
        f"This folder is an Alkera {what} — a replication context, not a saved answer.",
        "It carries everything needed to run the same analysis again with different",
        "parameters. Read `spec.json` for the exact shape.",
        "",
        "**Ask the questions below before you run anything.** Do not reuse the recorded",
        "defaults without asking: they are what it was last run with, not what is wanted",
        "now.",
        "",
    ]


def _questions_section(spec: Mapping[str, Any]) -> list[str]:
    # A report declares them as `questions`; a saved query declares the same
    # things as the `params` its slots bind to. One section either way — the
    # reader should not have to know which kind of object they were handed.
    declared = _sequence(spec.get("questions")) or _sequence(spec.get("params"))
    lines = ["## Questions to ask first", ""]
    if not declared:
        lines += ["_This context declares no parameters: it runs the same way every time._", ""]
        return lines
    defaults = _mapping(spec.get("defaults"))
    for raw in declared:
        param = _mapping(raw)
        name = str(param.get("name") or "")
        prompt = str(param.get("prompt") or "").strip()
        if not prompt:
            prompt = str(param.get("label") or "").strip() or _FALLBACK_PROMPT.format(name=name)
        bits = [f"`{name}`", f"({param.get('type') or 'string'})"]
        if not param.get("required", True):
            bits.append("optional")
        choices = [str(value) for value in _sequence(param.get("enum_values"))]
        if choices:
            bits.append("one of: " + ", ".join(choices))
        if name in defaults:
            bits.append(f"last run with: {defaults[name]!r}")
        lines.append(f"- **{prompt}** — {' · '.join(bits)}")
    lines.append("")
    return lines


def _connections_section(object_type: str, spec: Mapping[str, Any]) -> list[str]:
    lines = ["## Connections", ""]
    declared = _sequence(spec.get("connections"))
    if declared:
        for raw in declared:
            connection = _mapping(raw)
            plugin = str(connection.get("plugin") or "?")
            handle = str(connection.get("handle") or "?")
            lines.append(f"- `{plugin}` / `{handle}`")
        lines.append("")
        return lines
    if object_type == "query":
        engine = str(spec.get("engine") or "")
        connection_id = str(spec.get("connection_id") or "")
        lines.append(
            f"- engine `{engine or 'unknown'}`"
            + (f", connection `{connection_id}`" if connection_id else "")
        )
        lines.append("")
        return lines
    lines += ["_None declared._", ""]
    return lines


def _steps_section(object_type: str, spec: Mapping[str, Any]) -> list[str]:
    lines = ["## Steps", ""]
    declared = _sequence(spec.get("steps"))
    if declared:
        for index, raw in enumerate(declared, start=1):
            step = _mapping(raw)
            kind = str(step.get("kind") or "query")
            against = str(step.get("connection") or "")
            heading = f"{index}. **{kind}**" + (f" against `{against}`" if against else "")
            lines += [heading, "", "```", str(step.get("text") or ""), "```", ""]
        return lines
    if object_type == "query":
        lines += ["```sql", str(spec.get("sql_template") or ""), "```", ""]
        return lines
    lines += ["_None declared._", ""]
    return lines


def _rendering_section(spec: Mapping[str, Any]) -> list[str]:
    narrative = str(spec.get("narrative") or "").strip()
    rendering = _mapping(spec.get("rendering"))
    if not narrative and not rendering:
        return []
    lines = ["## Rendering", ""]
    if rendering:
        lines.append(f"- format: `{rendering.get('format') or 'html'}`")
        template = str(rendering.get("template") or "").strip()
        if template:
            lines += ["- structure:", "", template, ""]
        else:
            lines.append("")
    if narrative:
        lines += ["### What the report says", "", narrative, ""]
    return lines


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []
