"""The dependency graph of cells, through the format's own analysis.

The simulator never re-implements defs and refs: a cell's code is rendered
from its kind and editor text with ``format.render_cell`` (a SQL or Markdown
cell becomes the ``alkera.sql`` or ``alkera.md`` call the file would hold) and
the graph is ``format.analyze_code`` over those codes, which is what the
engine builds its kernel graph with. This module only adds the walks the
planner and the invariants need (ancestors, descendants, topological order).
"""

from __future__ import annotations

import functools
import heapq
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from alkera_notebook import format as nbformat

#: A cell as the graph sees it: (id, kind, editor text, meta).
GraphInput = tuple[str, str, str, Mapping[str, Any]]


@dataclass(frozen=True)
class CellAnalysis:
    defs: tuple[str, ...]
    refs: tuple[str, ...]


@dataclass
class GraphAnalysis:
    cells: dict[str, CellAnalysis]
    edges: list[tuple[str, str]]
    errors: dict[str, list[str]] = field(default_factory=dict)
    """Cell id -> error codes (``syntax_error``, ``multiple_definitions``,
    ``cycle``, ``delete_nonlocal``)."""
    names: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    """Cell id -> error code -> the names the error is about."""

    _links: dict[bool, dict[str, set[str]]] = field(default_factory=dict, repr=False, compare=False)
    """Adjacency by direction, built on the first walk (``edges`` never changes
    after analysis), so a walk costs the cells it reaches, not every edge."""

    def _adjacency(self, forward: bool) -> dict[str, set[str]]:
        links = self._links.get(forward)
        if links is None:
            links = {}
            for a, b in self.edges:
                src, dst = (a, b) if forward else (b, a)
                links.setdefault(src, set()).add(dst)
            self._links[forward] = links
        return links

    def _walk(self, start: str, forward: bool) -> set[str]:
        links = self._adjacency(forward)
        seen: set[str] = set()
        stack = list(links.get(start, ()))
        while stack:
            node = stack.pop()
            if node in seen:
                continue
            seen.add(node)
            stack.extend(links.get(node, ()))
        seen.discard(start)
        return seen

    def ancestors(self, cell_id: str) -> set[str]:
        return self._walk(cell_id, forward=False)

    def descendants(self, cell_id: str) -> set[str]:
        return self._walk(cell_id, forward=True)


def cell_code(kind: str, text: str, meta: Mapping[str, Any]) -> str:
    """The Python code a cell of ``kind`` with editor text ``text`` runs as."""
    return nbformat.render_cell(kind, text, meta)


def analyze(cells: Sequence[GraphInput]) -> GraphAnalysis:
    """The graph of ``cells``. The result is shared between calls with the same
    cells (the reference engine asks for the same graph on every read), so a
    caller treats it as read-only."""
    key = tuple(
        (cid, kind, text, json.dumps(dict(meta), sort_keys=True, default=str))
        for cid, kind, text, meta in cells
    )
    return _analyze(key)


@functools.lru_cache(maxsize=8)
def _analyze(cells: tuple[tuple[str, str, str, str], ...]) -> GraphAnalysis:
    graph = nbformat.analyze_code(
        [(cid, cell_code(kind, text, json.loads(meta))) for cid, kind, text, meta in cells]
    )
    analyses = {
        cid: CellAnalysis(tuple(info["defs"]), tuple(info["refs"]))
        for cid, info in graph["cells"].items()
    }
    errors: dict[str, list[str]] = {}
    names: dict[str, dict[str, list[str]]] = {}
    for cid, info in graph["cells"].items():
        for error in info["errors"]:
            code, name = error_code(error), error_name(error)
            if code not in errors.setdefault(cid, []):
                errors[cid].append(code)
            if name:
                names.setdefault(cid, {}).setdefault(code, []).append(name)
    return GraphAnalysis(analyses, [(a, b) for a, b in graph["edges"]], errors, names)


def error_code(error: Any) -> str:
    """A graph error's code, from the format's error object, the engine's
    structured error, or a bare code."""
    if isinstance(error, Mapping):
        return str(error.get("code", "error"))
    code = getattr(error, "code", None)
    if isinstance(code, str):
        return code
    return str(error)


def error_name(error: Any) -> str | None:
    if isinstance(error, Mapping) and error.get("name"):
        return str(error["name"])
    return None


def topological(graph: GraphAnalysis, cells: Sequence[str], doc_order: Sequence[str]) -> list[str]:
    """``cells`` ordered so every cell follows its ancestors among them; ties by
    document order; the members of a cycle in document order."""
    wanted = set(cells)
    position = {cid: i for i, cid in enumerate(doc_order)}
    waiting: dict[str, int] = dict.fromkeys(wanted, 0)
    children: dict[str, list[str]] = {cid: [] for cid in wanted}
    for a, b in set(graph.edges):
        if a in wanted and b in wanted:
            waiting[b] += 1
            children[a].append(b)
    ready = [(position.get(cid, 0), cid) for cid, count in waiting.items() if count == 0]
    heapq.heapify(ready)
    out: list[str] = []
    while ready:
        _, cid = heapq.heappop(ready)
        out.append(cid)
        for child in children[cid]:
            waiting[child] -= 1
            if waiting[child] == 0:
                heapq.heappush(ready, (position.get(child, 0), child))
    if len(out) < len(wanted):
        done = set(out)
        out.extend(sorted((c for c in wanted if c not in done), key=lambda c: position.get(c, 0)))
    return out


__all__ = [
    "CellAnalysis",
    "GraphAnalysis",
    "GraphInput",
    "analyze",
    "cell_code",
    "error_code",
    "error_name",
    "topological",
]
