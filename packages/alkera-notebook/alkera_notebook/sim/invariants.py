"""The invariants the simulator checks after every step.

Each invariant is a function over a :class:`~alkera_notebook.sim.targets.SimTarget`
and the driver's own record of what happened (:class:`SimLedger`), returning
the violations it found. Every invariant names the target capabilities it
needs; one the target cannot support is reported as unchecked.

The ledger is what makes these checks more than the engine agreeing with
itself: the oracle document is built by applying every acknowledged operation
to a model written from the operation rules alone; the set of ids is
everything the driver ever saw created; the gate log is the driver's own
record of which calls were asked about.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from itertools import pairwise

from alkera_notebook.format import file_code
from alkera_notebook.sim.analysis import GraphInput, analyze
from alkera_notebook.sim.oracle import DocumentModel
from alkera_notebook.sim.targets import Capability, CellFacts, SimTarget
from alkera_notebook.tools.gates import GateEffect


@dataclass(frozen=True)
class Violation:
    invariant: str
    path: str
    detail: str


class InvariantViolationError(AssertionError):
    def __init__(self, violations: Sequence[Violation]) -> None:
        lines = [f"[{v.invariant}] {v.path}: {v.detail}" for v in violations]
        super().__init__("\n".join(lines))
        self.violations = list(violations)


@dataclass
class CallRecord:
    """One agent tool call as the driver saw it."""

    tool: str
    effect: GateEffect
    asked: bool
    """Whether the gatekeeper was asked about this very call."""
    succeeded: bool
    ran_code: bool
    """Whether the call's result says it started code (a run that was not refused)."""
    result_ok: bool
    """Whether the result validated against the tool's output model and, when
    it ran code, named the reactivity mode, the environment and the plan."""
    error: str | None = None


@dataclass
class SimLedger:
    """What the driver knows independently of the engine."""

    oracles: dict[str, DocumentModel] = field(default_factory=dict)
    ids_seen: dict[str, set[str]] = field(default_factory=dict)
    deleted_ids: dict[str, set[str]] = field(default_factory=dict)
    calls: list[CallRecord] = field(default_factory=list)
    agent_id: str = ""
    gate_mode: str = "default"
    unchecked: set[str] = field(default_factory=set)


Invariant = Callable[[SimTarget, SimLedger], list[Violation]]


@dataclass(frozen=True)
class InvariantDef:
    name: str
    needs: frozenset[Capability]
    check: Invariant
    settled: bool = False
    """Checked only once no run is queued or running."""


def _kernel_text(facts: CellFacts) -> str:
    return facts.submitted if facts.submitted is not None else facts.source


def _kernel_inputs(facts: dict[str, CellFacts], order: Sequence[str]) -> list[GraphInput]:
    """Each cell with the text it last ran with, or its current text if it never ran."""
    return [
        (cid, facts[cid].kind, _kernel_text(facts[cid]), facts[cid].meta)
        for cid in order
        if cid in facts
    ]


def recompute_statuses(target: SimTarget, path: str) -> dict[str, str]:
    """Every cell's status from the kernel facts alone, by the status rules:
    a cell is fresh when it ran its current text and every ancestor in the
    kernel graph holds a value from before it ran and is fresh itself."""
    facts = target.kernel_facts(path)
    order = [cid for cid, _, _, _ in target.document(path)]
    graph = analyze(_kernel_inputs(facts, order))
    out: dict[str, str] = {}

    def status(cid: str, trail: frozenset[str]) -> str:
        if cid in out:
            return out[cid]
        f = facts[cid]
        if f.disabled:
            result = "disabled"
        elif f.result == "none":
            result = "not_run"
        elif f.result in ("error", "interrupted", "skipped", "stopped"):
            result = f.result
        elif f.source != f.submitted:
            result = "edited"
        else:
            result = "fresh"
            for parent in graph.ancestors(cid):
                if parent in trail or parent not in facts:
                    continue
                pf = facts[parent]
                if not pf.has_value or pf.seq > f.seq or status(parent, trail | {cid}) != "fresh":
                    result = "stale"
                    break
        out[cid] = result
        return result

    for cid in order:
        status(cid, frozenset())
    return out


def check_oracle_document(target: SimTarget, ledger: SimLedger) -> list[Violation]:
    out: list[Violation] = []
    for path, oracle in ledger.oracles.items():
        actual = target.document(path)
        expected = oracle.signature()
        if actual != expected:
            out.append(
                Violation(
                    "oracle_document",
                    path,
                    f"store has {len(actual)} cells {[a[0] for a in actual]}, "
                    f"oracle has {len(expected)} {[e[0] for e in expected]}; "
                    f"first difference at {_first_difference(actual, expected)}",
                )
            )
    return out


def _first_difference(a: Sequence[object], b: Sequence[object]) -> str:
    for i, (x, y) in enumerate(zip(a, b, strict=False)):
        if x != y:
            return f"index {i}: {x!r} != {y!r}"
    return f"length {len(a)} != {len(b)}"


def check_ids(target: SimTarget, ledger: SimLedger) -> list[Violation]:
    """Surviving cells keep their ids and no id is ever reused: every live id is
    one the driver saw created, and an id seen deleted comes back only by restore
    (which the oracle tracks)."""
    out: list[Violation] = []
    for path in target.paths():
        seen = ledger.ids_seen.get(path, set())
        live = [cid for cid, _, _, _ in target.document(path)]
        unknown = [cid for cid in live if cid not in seen]
        if unknown:
            out.append(Violation("ids_stable", path, f"cells with ids nobody created: {unknown}"))
        if len(set(live)) != len(live):
            out.append(Violation("ids_stable", path, f"duplicate ids in the document: {live}"))
    return out


def check_statuses(target: SimTarget, ledger: SimLedger) -> list[Violation]:
    del ledger
    out: list[Violation] = []
    for path in target.paths():
        expected = recompute_statuses(target, path)
        reported = _reported_statuses(target, path)
        for cid, status in reported.items():
            if expected.get(cid) != status:
                out.append(
                    Violation(
                        "statuses_recomputed",
                        path,
                        f"cell {cid} reported {status}, recomputed {expected.get(cid)}",
                    )
                )
    return out


def _reported_statuses(target: SimTarget, path: str) -> dict[str, str]:
    return target.reported_statuses(path)


def check_graph(target: SimTarget, ledger: SimLedger) -> list[Violation]:
    """The graph the engine plans by equals the analysis of the code each cell
    last ran with (its current text if it never ran)."""
    del ledger
    out: list[Violation] = []
    for path in target.paths():
        facts = target.kernel_facts(path)
        order = [cid for cid, _, _, _ in target.document(path)]
        expected = analyze(_kernel_inputs(facts, order))
        edges, errors = target.reported_graph(path)
        if sorted(edges) != sorted(expected.edges):
            out.append(
                Violation(
                    "graph_analysis", path, f"edges {sorted(edges)} != {sorted(expected.edges)}"
                )
            )
        if {k: sorted(v) for k, v in errors.items()} != {
            k: sorted(v) for k, v in expected.errors.items()
        }:
            out.append(Violation("graph_analysis", path, f"errors {errors} != {expected.errors}"))
    return out


def check_outputs_name_runs(target: SimTarget, ledger: SimLedger) -> list[Violation]:
    """Every executed step belongs to a run whose plan included the cell."""
    del ledger
    out: list[Violation] = []
    for path in target.paths():
        plans = {r.run_id: set(r.planned) for r in target.runs(path)}
        for run_id, cid in target.executions(path):
            if run_id not in plans:
                out.append(
                    Violation("outputs_name_runs", path, f"{cid} ran in unknown run {run_id}")
                )
            elif cid not in plans[run_id]:
                out.append(
                    Violation("outputs_name_runs", path, f"{cid} ran in {run_id}, outside its plan")
                )
    return out


def check_one_run_at_a_time(target: SimTarget, ledger: SimLedger) -> list[Violation]:
    """Runs execute one at a time, in the order they were queued, and every
    run has ended once the engine settles."""
    del ledger
    out: list[Violation] = []
    for path in target.paths():
        runs = target.runs(path)
        order = [r.run_id for r in runs]
        executed_runs: list[str] = []
        for run_id, _ in target.executions(path):
            if not executed_runs or executed_runs[-1] != run_id:
                if run_id in executed_runs:
                    out.append(Violation("one_run_at_a_time", path, f"run {run_id} interleaved"))
                executed_runs.append(run_id)
        positions = [order.index(r) for r in executed_runs if r in order]
        if positions != sorted(positions):
            out.append(
                Violation("one_run_at_a_time", path, f"runs out of queue order: {executed_runs}")
            )
        spans = sorted(
            (r.started_at, r.finished_at, r.run_id)
            for r in runs
            if r.started_at is not None and r.finished_at is not None
        )
        for (_, earlier_end, earlier), (later_start, _, later) in pairwise(spans):
            if later_start < earlier_end:
                out.append(
                    Violation(
                        "one_run_at_a_time",
                        path,
                        f"runs {earlier} and {later} overlap",
                    )
                )
        open_runs = [r.run_id for r in runs if r.status in ("queued", "running")]
        if open_runs:
            out.append(Violation("one_run_at_a_time", path, f"runs that never ended: {open_runs}"))
    return out


def check_gates(target: SimTarget, ledger: SimLedger) -> list[Violation]:
    """No call that edits, runs code or installs succeeded without its gate
    being asked about that very call, and every call that ran code returned a
    result naming the mode, the environment and the plan."""
    del target
    out: list[Violation] = []
    for index, call in enumerate(ledger.calls):
        if (
            call.succeeded
            and call.effect is not GateEffect.READ
            and not call.asked
            and call.ran_code
        ):
            out.append(Violation("gated", call.tool, f"call {index} ran ungated"))
        if call.succeeded and not call.result_ok:
            out.append(Violation("result_schema", call.tool, f"call {index} result invalid"))
    return out


def check_file(target: SimTarget, ledger: SimLedger) -> list[Violation]:
    del ledger
    out: list[Violation] = []
    for path in target.paths():
        checked = target.file_check(path)
        if checked is None:
            continue
        cells, identical = checked
        document = target.document(path)
        expected = [(cid, kind, name, file_code(src)) for cid, kind, name, src in document]
        kinds = {cid: kind for cid, kind, _, _ in document}
        read = [
            (cid, _document_kind(kind, kinds.get(cid, kind)), name, file_code(src))
            for cid, kind, name, src in cells
        ]
        if read != expected:
            out.append(
                Violation(
                    "file_roundtrip",
                    path,
                    f"the file does not read to the document: {_first_difference(read, expected)}",
                )
            )
        if not identical:
            out.append(Violation("file_roundtrip", path, "writing the file back changes its bytes"))
    return out


#: Kinds the format derives from a cell's code. A document cell of the kind on
#: the left reads back from the file as any kind on the right: a Python cell that
#: holds only a function is a ``function`` cell in the file, and one that does not
#: parse is ``unparsable``; a SQL or Markdown text the template cannot hold is
#: written as plain code and reads back as a Python cell with the same text.
_DERIVED_KINDS: dict[str, frozenset[str]] = {
    "python": frozenset({"python", "function", "class", "unparsable"}),
    "setup": frozenset({"setup", "unparsable"}),
    "sql": frozenset({"sql", "python", "unparsable"}),
    "markdown": frozenset({"markdown", "python", "unparsable"}),
}


def _document_kind(file_kind: str, document_kind: str) -> str:
    """The file's kind, as the document kind it is derived from when it is one."""
    if file_kind in _DERIVED_KINDS.get(document_kind, frozenset()):
        return document_kind
    return file_kind


def check_memory(target: SimTarget, ledger: SimLedger) -> list[Violation]:
    del ledger
    budget, samples = target.memory_samples()
    over = 0
    for sample in samples:
        over = over + 1 if budget and sample > budget else 0
        if over > 1:
            return [Violation("memory_budget", "*", f"over budget {budget} for two samples")]
    return []


INVARIANTS: tuple[InvariantDef, ...] = (
    InvariantDef("oracle_document", frozenset({"document"}), check_oracle_document),
    InvariantDef("ids_stable", frozenset({"document"}), check_ids),
    InvariantDef(
        "statuses_recomputed", frozenset({"document", "kernel_facts"}), check_statuses, True
    ),
    InvariantDef("graph_analysis", frozenset({"document", "kernel_facts"}), check_graph),
    InvariantDef("outputs_name_runs", frozenset({"runs"}), check_outputs_name_runs),
    InvariantDef("one_run_at_a_time", frozenset({"runs"}), check_one_run_at_a_time, True),
    InvariantDef("gated", frozenset(), check_gates),
    InvariantDef("file_roundtrip", frozenset({"file", "document"}), check_file),
    InvariantDef("memory_budget", frozenset({"memory"}), check_memory),
)


def check_all(target: SimTarget, ledger: SimLedger, *, settled: bool) -> list[Violation]:
    found: list[Violation] = []
    for inv in INVARIANTS:
        if not inv.needs <= target.capabilities:
            ledger.unchecked.add(inv.name)
            continue
        if inv.settled and not settled:
            continue
        found.extend(inv.check(target, ledger))
    return found


__all__ = [
    "INVARIANTS",
    "CallRecord",
    "InvariantDef",
    "InvariantViolationError",
    "SimLedger",
    "Violation",
    "check_all",
    "recompute_statuses",
]
