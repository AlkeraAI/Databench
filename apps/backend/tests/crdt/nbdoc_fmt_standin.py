"""A stand-in for the notebook format API (``alkera_notebook.format``) in tests.

The real module (lane FMT) reads and writes ``.alknb.py``; until it lands the
notebook document type is tested against this one, which honours the same
signatures and the properties the document type relies on, over a deliberately
simple text layout that is NOT the notebook format:

* ``write(read(f)) == f`` for every text it wrote, and ``write(read(x))`` is a
  fixed point;
* ids ride each cell's header line; a cell without one is resolved against
  ``known`` by exact normalized code, then by similarity (0.6), then minted
  deterministically from its code (never positionally);
* a ``format`` whose major is above 1 reads with ``read_only_reason``
  ``newer_format``; reading never raises.

Layout::

    #fmt format=1.0 generated_with=0.25.1
    #set reactivity=lazy
    #unknown foo = 1
    #app width=medium
    <header lines>
    #cell id=<id> kind=<kind> name=<name> config=<json> meta=<json> extra=<json>
    <source lines>
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

# The real op rules: they take this stand-in's ``render_cell`` and ``classify``,
# as the sandbox passes them, so the stand-in states no op rule of its own.
from alkera_notebook.format import op_rules as op_rules

_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"


@dataclass(frozen=True)
class Violation:
    code: str
    line: int
    message: str


@dataclass(frozen=True)
class CellIR:
    id: str
    kind: str
    name: str
    source: str
    code: str
    config: Mapping[str, Any]
    meta: Mapping[str, Any]
    extra: Mapping[str, Any]
    resolution: str


@dataclass(frozen=True)
class NotebookIR:
    format: str
    header_text: str
    settings: Mapping[str, Any]
    unknown_settings: str
    app_config: Mapping[str, Any]
    generated_with: str
    cells: Sequence[CellIR]
    violations: Sequence[Violation] = field(default_factory=tuple)
    read_only_reason: str | None = None


def new_cell_id() -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(10))


def _minted(code: str, taken: set[str]) -> str:
    seed = code.encode("utf-8")
    while True:
        digest = hashlib.sha256(seed).digest()
        number = int.from_bytes(digest[:7], "big") >> 6
        found = "".join(_ALPHABET[(number >> (5 * i)) & 31] for i in range(10))
        if found not in taken:
            return found
        seed = digest


def normalize_code(code: str) -> str:
    return "\n".join(line.rstrip() for line in code.strip().splitlines())


def render_cell(kind: str, source: str, meta: Mapping[str, Any]) -> str:
    if kind == "sql":
        var = meta.get("output_var", "_df")
        return f'{var} = alkera.sql(rf"""{source}""")'
    if kind == "markdown":
        return f'alkera.md(r"""{source}""")'
    return source


_SQL = re.compile(r'\A(?P<var>\w+) = alkera\.sql\(rf"""(?P<source>.*)"""\)\Z', re.DOTALL)
_MD = re.compile(r'\Aalkera\.md\(r"""(?P<source>.*)"""\)\Z', re.DOTALL)


def classify(code: str) -> tuple[str, str, dict[str, Any]]:
    """What :func:`render_cell` wrote reads back as its kind and text, as the
    real format's templates do; any other code is Python."""
    sql = _SQL.match(code)
    if sql is not None and '"""' not in sql["source"]:
        return "sql", sql["source"], {"output_var": sql["var"]}
    md = _MD.match(code)
    if md is not None and '"""' not in md["source"]:
        return "markdown", md["source"], {}
    return "python", code, {}


def _escape(line: str) -> str:
    return "\\" + line if line.startswith(("#cell ", "\\")) else line


def _unescape(line: str) -> str:
    return line[1:] if line.startswith("\\") else line


def write(ir: NotebookIR) -> str:
    lines = [f"#fmt format={ir.format} generated_with={ir.generated_with}"]
    for key, value in ir.settings.items():
        lines.append(f"#set {key}={json.dumps(value)}")
    for raw in ir.unknown_settings.splitlines():
        lines.append(f"#unknown {raw}")
    for key, value in ir.app_config.items():
        lines.append(f"#app {key}={json.dumps(value)}")
    lines.extend(_escape(line) for line in ir.header_text.splitlines())
    for cell in ir.cells:
        lines.append(
            f"#cell id={cell.id} kind={cell.kind} name={cell.name}"
            f" config={json.dumps(dict(cell.config), sort_keys=True, separators=(',', ':'))}"
            f" meta={json.dumps(dict(cell.meta), sort_keys=True, separators=(',', ':'))}"
            f" extra={json.dumps(dict(cell.extra), sort_keys=True, separators=(',', ':'))}"
        )
        lines.extend(_escape(line) for line in cell.source.split("\n"))
    return "\n".join(lines) + "\n"


def _fields(head: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for part in head.split(" ")[1:]:
        key, _, value = part.partition("=")
        found[key] = value
    return found


def _json(raw: str, default: Any) -> Any:
    try:
        return json.loads(raw)
    except ValueError:
        return default


def read(text: str, *, known: Mapping[str, str] | None = None) -> NotebookIR:
    known = dict(known or {})
    lines = text[:-1].split("\n") if text.endswith("\n") else text.split("\n")
    fmt, generated = "1.0", ""
    settings: dict[str, Any] = {}
    unknown: list[str] = []
    app: dict[str, Any] = {}
    header: list[str] = []
    raw_cells: list[tuple[dict[str, str], list[str]]] = []
    for line in lines:
        if raw_cells:
            if line.startswith("#cell "):
                raw_cells.append((_fields(line), []))
            else:
                raw_cells[-1][1].append(_unescape(line))
            continue
        if line.startswith("#cell "):
            raw_cells.append((_fields(line), []))
        elif line.startswith("#fmt "):
            found = _fields(line)
            fmt, generated = found.get("format", "1.0"), found.get("generated_with", "")
        elif line.startswith("#set "):
            key, _, value = line[5:].partition("=")
            settings[key] = _json(value, value)
        elif line.startswith("#unknown "):
            unknown.append(line[9:])
        elif line.startswith("#app "):
            key, _, value = line[5:].partition("=")
            app[key] = _json(value, value)
        elif line:
            header.append(_unescape(line))
    major = fmt.partition(".")[0]
    if major.isdigit() and int(major) > 1:
        return NotebookIR(
            format=fmt,
            header_text="",
            settings={},
            unknown_settings="",
            app_config={},
            generated_with=generated,
            cells=(),
            read_only_reason="newer_format",
        )
    taken: set[str] = set()
    pending: list[tuple[int, str]] = []
    cells: list[CellIR | None] = []
    for index, (head, body) in enumerate(raw_cells):
        kind = head.get("kind", "python")
        source = "\n".join(body)
        code = render_cell(kind, source, _json(head.get("meta", "{}"), {}))
        cell_id = head.get("id", "")
        if cell_id and cell_id not in taken:
            taken.add(cell_id)
            cells.append(_cell(head, cell_id, source, code, "keyword"))
        else:
            cells.append(None)
            pending.append((index, code))
    free = {k: v for k, v in known.items() if k not in taken}
    for index, code in pending:
        head = raw_cells[index][0]
        source = "\n".join(raw_cells[index][1])
        normalized = normalize_code(code)
        exact = [k for k, v in sorted(free.items()) if v == normalized]
        if exact:
            cell_id, how = exact[0], "exact"
        else:
            scored = sorted(
                (
                    (-difflib.SequenceMatcher(None, v, normalized).ratio(), k)
                    for k, v in free.items()
                ),
            )
            if scored and -scored[0][0] >= 0.6:
                cell_id, how = scored[0][1], "similar"
            else:
                cell_id, how = _minted(normalized, taken | set(free)), "minted"
        free.pop(cell_id, None)
        taken.add(cell_id)
        cells[index] = _cell(head, cell_id, source, code, how)
    return NotebookIR(
        format=fmt,
        header_text="\n".join(header),
        settings=settings,
        unknown_settings="\n".join(unknown),
        app_config=app,
        generated_with=generated,
        cells=tuple(c for c in cells if c is not None),
    )


def _cell(head: Mapping[str, str], cell_id: str, source: str, code: str, how: str) -> CellIR:
    return CellIR(
        id=cell_id,
        kind=head.get("kind", "python"),
        name=head.get("name", "_"),
        source=source,
        code=code,
        config=_json(head.get("config", "{}"), {}),
        meta=_json(head.get("meta", "{}"), {}),
        extra=_json(head.get("extra", "{}"), {}),
        resolution=how,
    )


def analyze(ir: NotebookIR) -> dict[str, Any]:
    """Defs are ``name = ...`` at the start of a line; refs any defined name."""
    defs: dict[str, list[str]] = {}
    for cell in ir.cells:
        defs[cell.id] = [
            line.split("=", 1)[0].strip()
            for line in cell.code.splitlines()
            if "=" in line and line.split("=", 1)[0].strip().isidentifier()
        ]
    out: dict[str, Any] = {"cells": {}, "edges": []}
    for cell in ir.cells:
        refs = sorted(
            {
                n
                for other, names in defs.items()
                if other != cell.id
                for n in names
                if n in cell.code
            }
        )
        out["cells"][cell.id] = {
            "defs": defs[cell.id],
            "refs": refs,
            "sql_tables": [],
            "errors": [],
        }
        for other, names in defs.items():
            if other != cell.id and any(n in refs for n in names):
                out["edges"].append([other, cell.id])
    return out
