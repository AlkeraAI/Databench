"""A dependency graph over cells, built from the format's analysis output."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from alkera_notebook.document.ops import GraphErrorInfo, error_label


@dataclass(frozen=True)
class CellGraph:
    """Edges parent -> child; ``order`` is document order (ties)."""

    order: tuple[str, ...]
    defs: Mapping[str, tuple[str, ...]]
    refs: Mapping[str, tuple[str, ...]]
    errors: Mapping[str, tuple[GraphErrorInfo, ...]]
    parents: Mapping[str, frozenset[str]]
    children: Mapping[str, frozenset[str]]
    edges: tuple[tuple[str, str], ...] = field(default=())

    @classmethod
    def from_analysis(cls, order: Sequence[str], analysis: Mapping[str, Any]) -> CellGraph:
        cells: Mapping[str, Any] = analysis.get("cells", {}) or {}
        known = set(order)
        parents: dict[str, set[str]] = {c: set() for c in order}
        children: dict[str, set[str]] = {c: set() for c in order}
        edges: list[tuple[str, str]] = []
        for edge in analysis.get("edges", []) or []:
            a, b = str(edge[0]), str(edge[1])
            if a in known and b in known and a != b and (a, b) not in edges:
                parents[b].add(a)
                children[a].add(b)
                edges.append((a, b))

        def _names(cid: str, key: str) -> tuple[str, ...]:
            info = cells.get(cid) or {}
            return tuple(error_label(n) for n in info.get(key, []) or [])

        return cls(
            order=tuple(order),
            defs={c: _names(c, "defs") for c in order},
            refs={c: _names(c, "refs") for c in order},
            errors={
                c: tuple(
                    GraphErrorInfo.parse(e) for e in (cells.get(c) or {}).get("errors", []) or []
                )
                for c in order
            },
            parents={c: frozenset(p) for c, p in parents.items()},
            children={c: frozenset(ch) for c, ch in children.items()},
            edges=tuple(edges),
        )

    def ancestors(self, cell_id: str) -> set[str]:
        return self._closure([cell_id], self.parents)

    def descendants(self, cell_id: str) -> set[str]:
        return self._closure([cell_id], self.children)

    def descendants_of(self, cell_ids: Iterable[str]) -> set[str]:
        return self._closure(list(cell_ids), self.children)

    @staticmethod
    def _closure(start: list[str], step: Mapping[str, frozenset[str]]) -> set[str]:
        seen: set[str] = set()
        stack = list(start)
        while stack:
            for nxt in step.get(stack.pop(), frozenset()):
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        seen.difference_update(start)
        return seen

    def topological(self, cells: Iterable[str]) -> list[str]:
        """``cells`` ordered parents first, ties (and cycles) by document order."""
        wanted = set(cells)
        position = {c: i for i, c in enumerate(self.order)}
        indegree = {c: len(self.parents.get(c, frozenset()) & wanted) for c in wanted}
        ready = sorted((c for c, d in indegree.items() if d == 0), key=position.__getitem__)
        out: list[str] = []
        while ready:
            cur = ready.pop(0)
            out.append(cur)
            changed = False
            for child in self.children.get(cur, frozenset()):
                if child in indegree and child not in out:
                    indegree[child] -= 1
                    if indegree[child] == 0:
                        ready.append(child)
                        changed = True
            if changed:
                ready.sort(key=position.__getitem__)
        if len(out) < len(wanted):
            out += sorted(wanted - set(out), key=position.__getitem__)
        return out
