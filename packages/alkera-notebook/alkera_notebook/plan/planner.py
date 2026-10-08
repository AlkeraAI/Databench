"""The run planner: which cells run, with which code, in which order.

Pure: the same :class:`PlanInput` always yields the same :class:`Plan`.

The kernel graph is built from submitted code (what each cell last ran with
in the current kernel). A run submits the current text of its targets only;
everything else that runs (upstream cells with no value, descendants that
hold values) runs the code the kernel already knows, so nobody's live,
unsubmitted text ever executes as a side effect of someone else's run.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from alkera_notebook.plan.graph import CellGraph

RuntimeStatus = Literal["fresh", "stale", "not_run", "error", "interrupted", "skipped", "stopped"]
Reactivity = Literal["autorun", "lazy"]
TargetCode = Literal["current", "submitted"]
Scope = Literal["cells", "all", "stale", "above", "below"]


@dataclass(frozen=True)
class PlanCell:
    id: str
    name: str
    current_code: str
    # The code this cell last ran with in the current kernel; None if it has
    # not run in this kernel.
    submitted_code: str | None
    status: RuntimeStatus
    holds_value: bool
    disabled: bool = False
    kind: str = "python"
    duration_s: float | None = None
    connection: str | None = None


@dataclass(frozen=True)
class PlanInput:
    cells: Sequence[PlanCell]  # live cells in document order
    scope: Scope
    targets: Sequence[str] = ()
    reactivity: Reactivity = "autorun"
    # Whose editing makes a re-run of which cell wait (cell id -> display
    # name): ``document.editing.blocker`` for the run's requester.
    editing: Mapping[str, str] = field(default_factory=dict)
    # The cells among them only the requester's own caret is in: they wait
    # as descendants like any other, but never refuse the requester's run.
    own_caret: frozenset[str] = frozenset()
    # Names each cell defined when it last ran in this kernel.
    prev_defs: Mapping[str, Sequence[str]] = field(default_factory=dict)
    # Names of cells deleted since they last ran.
    deleted_defs: Sequence[str] = ()
    target_code: TargetCode = "current"
    cost_guard_seconds: float = 60.0
    confirm_expensive: bool = False
    metered_connections: frozenset[str] = frozenset()


@dataclass(frozen=True)
class PlanStep:
    cell_id: str
    name: str
    code: str
    reason: Literal["target", "upstream", "descendant"]


@dataclass(frozen=True)
class Refusal:
    code: str  # "upstream_being_edited", "unknown_cell"
    cell_id: str
    message: str
    by: str | None = None


@dataclass(frozen=True)
class Plan:
    steps: tuple[PlanStep, ...]
    clear: tuple[str, ...]
    # Cells that hold values and become stale because something they depend
    # on runs while they do not (lazy mode, or someone is editing them).
    stale: tuple[str, ...]
    # Descendants skipped because someone is editing them.
    skipped_editing: tuple[str, ...]
    refusal: Refusal | None
    needs_confirmation: bool
    estimate_s: float
    confirmation_reasons: tuple[str, ...]
    graph: CellGraph

    @property
    def cell_ids(self) -> list[str]:
        return [s.cell_id for s in self.steps]


Analyze = Callable[[Sequence[tuple[str, str]]], Mapping[str, Any]]


def scope_targets(inp: PlanInput) -> list[str]:
    """The target cells a request names, in document order."""
    order = [c.id for c in inp.cells]
    by_id = {c.id: c for c in inp.cells}
    if inp.scope == "cells":
        return [c for c in order if c in set(inp.targets)]
    if inp.scope == "all":
        return list(order)
    if inp.scope == "stale":
        return [
            c.id
            for c in inp.cells
            if c.status != "fresh"
            or (c.submitted_code is not None and c.submitted_code != c.current_code)
        ]
    anchor = inp.targets[0] if inp.targets else None
    if anchor not in by_id:
        return []
    i = order.index(anchor)
    return order[:i] if inp.scope == "above" else order[i:]


def make_plan(inp: PlanInput, analyze: Analyze) -> Plan:
    by_id = {c.id: c for c in inp.cells}
    order = [c.id for c in inp.cells]
    position = {c: i for i, c in enumerate(order)}

    missing = [t for t in inp.targets if t not in by_id] if inp.scope == "cells" else []
    targets = scope_targets(inp)

    def code_for(cell: PlanCell, is_target: bool) -> str:
        if is_target and inp.target_code == "current":
            return cell.current_code
        if cell.submitted_code is not None:
            return cell.submitted_code
        return cell.current_code

    target_set = set(targets)
    graph = CellGraph.from_analysis(
        order, analyze([(c.id, code_for(c, c.id in target_set)) for c in inp.cells])
    )

    def empty(refusal: Refusal | None) -> Plan:
        return Plan((), (), (), (), refusal, False, 0.0, (), graph)

    if missing:
        return empty(Refusal("unknown_cell", missing[0], f"cell {missing[0]} does not exist"))

    # A disabled cell, or one below a disabled cell, never runs.
    disabled: set[str] = {c.id for c in inp.cells if c.disabled}
    disabled |= graph.descendants_of(list(disabled))
    targets = [t for t in targets if t not in disabled]
    target_set = set(targets)

    planned: dict[str, PlanStep] = {}
    for t in targets:
        planned[t] = PlanStep(t, by_id[t].name, code_for(by_id[t], True), "target")

    # Upstream: walk parents of everything that will run; a parent runs only
    # when it is not fresh and holds no value.
    stack = list(targets)
    seen: set[str] = set(targets)
    while stack:
        cur = stack.pop()
        for parent_id in sorted(graph.parents.get(cur, frozenset()), key=position.__getitem__):
            if parent_id in seen:
                continue
            seen.add(parent_id)
            parent = by_id[parent_id]
            if parent.status == "fresh" or parent.holds_value or parent_id in disabled:
                continue
            if (
                parent.submitted_code is None
                and parent_id in inp.editing
                and parent_id not in inp.own_caret
            ):
                who = inp.editing[parent_id]
                return empty(
                    Refusal(
                        "upstream_being_edited",
                        parent_id,
                        f"cell `{parent.name}` is being edited by {who}",
                        by=who,
                    )
                )
            planned[parent_id] = PlanStep(
                parent_id, parent.name, code_for(parent, False), "upstream"
            )
            stack.append(parent_id)

    stale: set[str] = set()
    skipped_editing: list[str] = []
    if inp.reactivity == "autorun":
        queue = sorted(planned, key=position.__getitem__)
        while queue:
            cur = queue.pop(0)
            for child_id in sorted(graph.children.get(cur, frozenset()), key=position.__getitem__):
                if child_id in planned or child_id in disabled:
                    continue
                child = by_id[child_id]
                if not child.holds_value or child.status not in ("fresh", "stale"):
                    continue
                if child_id in inp.editing:
                    if child_id not in skipped_editing:
                        skipped_editing.append(child_id)
                    stale.add(child_id)
                    continue
                planned[child_id] = PlanStep(
                    child_id, child.name, code_for(child, False), "descendant"
                )
                queue.append(child_id)
    # Whatever holds a value below a running cell and does not run is stale.
    for d in graph.descendants_of(list(planned)):
        if d not in planned and by_id[d].holds_value:
            stale.add(d)

    steps = tuple(planned[c] for c in graph.topological(planned))

    clear: list[str] = []
    for step in steps:
        for name in inp.prev_defs.get(step.cell_id, ()):
            if name not in clear:
                clear.append(name)
    for name in inp.deleted_defs:
        if name not in clear:
            clear.append(name)

    implicit = [s for s in steps if s.reason != "target"]
    estimate = sum(by_id[s.cell_id].duration_s or 0.0 for s in steps)
    implicit_estimate = sum(by_id[s.cell_id].duration_s or 0.0 for s in implicit)
    reasons: list[str] = []
    if implicit_estimate > inp.cost_guard_seconds:
        reasons.append("duration")
    if any(
        by_id[s.cell_id].kind == "sql"
        and (by_id[s.cell_id].connection or "") in inp.metered_connections
        for s in implicit
    ):
        reasons.append("metered_sql")
    needs = bool(reasons) and not inp.confirm_expensive

    return Plan(
        steps=steps,
        clear=tuple(clear),
        stale=tuple(sorted(stale, key=position.__getitem__)),
        skipped_editing=tuple(skipped_editing),
        refusal=None,
        needs_confirmation=needs,
        estimate_s=estimate,
        confirmation_reasons=tuple(reasons),
        graph=graph,
    )


def widget_targets(graph: CellGraph, names: Sequence[str]) -> list[str]:
    """Cells whose refs include any of the names a widget is bound to."""
    wanted = set(names)
    return [c for c in graph.order if wanted & set(graph.refs.get(c, ()))]
