"""Random simulation: Hypothesis state machines over every tool and action.

:class:`AgentMachine` is an agent alone: every notebook tool and every action
of each, with arguments drawn from the live notebook (real cell ids, names,
widgets) and deliberately wrong ones (missing cells, edits that do not match,
invalid names and kinds, paths outside the workspace). :class:`CollaborationMachine`
adds a person working in the same notebooks: editing, inserting and deleting
cells, running, moving widgets, and typing in the very cell the agent edited
last.

After every step the driver's invariants run; every few steps the engine is
settled and the settled invariants (statuses, run order) run too. A failure
is shrunk by Hypothesis, and :func:`run_machine` saves the shrunk step list
in the package's ``sim_regressions`` test directory for :func:`replay` to run
forever after.

The target is chosen by :data:`TARGET_FACTORY`, the reference engine unless a
test points it at the real engine.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any, ClassVar

from hypothesis import Phase, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, initialize, precondition, rule

from alkera_notebook.sim.driver import SimDriver, StanceGatekeeper, Step
from alkera_notebook.sim.grammar import NAMES, SETUP_SOURCE, CellSpec, cell_specs
from alkera_notebook.sim.reference import ReferenceWorkspace
from alkera_notebook.sim.targets import ReferenceTarget, SimTarget
from alkera_notebook.tools.models import (
    DeleteCellOp,
    EditCellOp,
    InsertCellOp,
    NotebookTextEdit,
    ReplaceCellOp,
    RunCells,
)

PATHS: tuple[str, ...] = ("analysis.alknb.py", "scratch/other.alknb.py")
BAD_PATHS: tuple[str, ...] = ("../outside.alknb.py", "notes.py", "/etc/passwd.alknb.py", "")
BAD_IDS: tuple[str, ...] = ("0000000000", "not-an-id", "")

#: Builds the target each example runs against.
TARGET_FACTORY: Callable[[], SimTarget] = lambda: ReferenceTarget(  # noqa: E731
    ReferenceWorkspace(seed=0, cost_guard_seconds=3600)
)


def _run(loop: asyncio.AbstractEventLoop, coro: Any) -> Any:
    return loop.run_until_complete(coro)


class AgentMachine(RuleBasedStateMachine):
    """One agent calling every notebook tool."""

    last_steps: ClassVar[list[Step]] = []

    def __init__(self) -> None:
        super().__init__()
        self.loop = asyncio.new_event_loop()
        self.driver = SimDriver(TARGET_FACTORY(), gatekeeper=StanceGatekeeper("default"))
        self.steps_since_settle = 0

    # -- helpers -----------------------------------------------------------------

    def call(self, tool: str, **args: Any) -> Any:
        result = _run(self.loop, self.driver.agent(tool, args))
        _run(self.loop, self.driver.check())
        self.steps_since_settle += 1
        return result

    def paths(self) -> list[str]:
        return self.driver.target.paths()

    def pick_path(self, index: int) -> str:
        paths = self.paths()
        return paths[index % len(paths)]

    def cells(self, path: str) -> list[tuple[str, str, str, str]]:
        return self.driver.target.document(path)

    def pick_cell(self, path: str, index: int) -> tuple[str, str, str, str] | None:
        cells = self.cells(path)
        return cells[index % len(cells)] if cells else None

    def widget_ids(self, path: str) -> list[str]:
        async def ids() -> list[str]:
            port = await self.driver.person_port(path)
            return [w.model_id for w in await port.widgets()]

        return list(_run(self.loop, ids()))

    def deleted(self, path: str) -> list[str]:
        oracle = self.driver.ledger.oracles.get(path)
        if oracle is None:
            return []
        return [cid for cid, cell in oracle.cells.items() if cell.deleted]

    # -- lifecycle -------------------------------------------------------------------

    @initialize(specs=st.lists(cell_specs(), max_size=4))
    def create_first(self, specs: list[CellSpec]) -> None:
        cells = [{"kind": "setup", "source": SETUP_SOURCE}]
        cells += [{"kind": s.kind, "source": s.source, "name": s.name} for s in specs]
        self.call("notebook.create", path=PATHS[0], cells=cells)

    def teardown(self) -> None:
        try:
            _run(self.loop, self.driver.check(settle=True))
            _run(self.loop, self.driver.close())
        finally:
            type(self).last_steps = list(self.driver.steps)
            self.loop.close()

    # -- document tools ------------------------------------------------------------

    @precondition(lambda self: len(self.paths()) < len(PATHS))
    @rule(specs=st.lists(cell_specs(), max_size=3), with_settings=st.booleans())
    def create_second(self, specs: list[CellSpec], with_settings: bool) -> None:
        cells = [{"kind": s.kind, "source": s.source, "name": s.name} for s in specs]
        settings_ = {"reactivity": "lazy"} if with_settings else {}
        self.call("notebook.create", path=PATHS[1], cells=cells, settings=settings_)

    @rule(
        p=st.integers(0, 9),
        spec=cell_specs(),
        at=st.integers(0, 50),
        where=st.sampled_from(["after", "before", "end"]),
    )
    def insert(self, p: int, spec: CellSpec, at: int, where: str) -> None:
        path = self.pick_path(p)
        anchor = self.pick_cell(path, at)
        op: dict[str, Any] = {
            "op": "insert",
            "kind": spec.kind,
            "source": spec.source,
            "name": spec.name,
            "meta": dict(spec.meta),
        }
        if anchor is not None and where != "end":
            op[where] = anchor[0]
        self.call("notebook.edit", path=path, ops=[op])

    @rule(
        p=st.integers(0, 9),
        kind=st.sampled_from(["widget", "notebook", "setup"]),
        name=st.sampled_from(["1x", "class", "ok_name"]),
    )
    def insert_invalid(self, p: int, kind: str, name: str) -> None:
        self.call(
            "notebook.edit",
            path=self.pick_path(p),
            ops=[{"op": "insert", "kind": kind, "name": name}],
        )

    @rule(
        p=st.integers(0, 9),
        c=st.integers(0, 50),
        cut=st.integers(0, 40),
        new=st.sampled_from(["1", "2", "x + 1", "pd"]),
        match=st.booleans(),
        occurrence=st.sampled_from([None, 1, 2]),
    )
    def edit(self, p: int, c: int, cut: int, new: str, match: bool, occurrence: int | None) -> None:
        path = self.pick_path(p)
        cell = self.pick_cell(path, c)
        if cell is None:
            return
        source = cell[3]
        old = source[cut % max(len(source), 1) :][:3] if match and source else "no such text"
        edit: dict[str, Any] = {"old": old, "new": new}
        if occurrence is not None:
            edit["occurrence"] = occurrence
        self.call(
            "notebook.edit", path=path, ops=[{"op": "edit", "cell_id": cell[0], "edits": [edit]}]
        )

    @rule(p=st.integers(0, 9), c=st.integers(0, 50), spec=cell_specs())
    def replace(self, p: int, c: int, spec: CellSpec) -> None:
        path = self.pick_path(p)
        cell = self.pick_cell(path, c)
        if cell is not None:
            self.call(
                "notebook.edit",
                path=path,
                ops=[{"op": "replace", "cell_id": cell[0], "source": spec.source}],
            )

    @rule(p=st.integers(0, 9), c=st.integers(0, 50))
    def delete(self, p: int, c: int) -> None:
        path = self.pick_path(p)
        cell = self.pick_cell(path, c)
        if cell is not None:
            self.call("notebook.edit", path=path, ops=[{"op": "delete", "cell_id": cell[0]}])

    @rule(p=st.integers(0, 9), d=st.integers(0, 50))
    def restore(self, p: int, d: int) -> None:
        path = self.pick_path(p)
        gone = self.deleted(path)
        target = gone[d % len(gone)] if gone else BAD_IDS[0]
        self.call("notebook.edit", path=path, ops=[{"op": "restore", "cell_id": target}])

    @rule(p=st.integers(0, 9), c=st.integers(0, 50), to=st.integers(0, 50), before=st.booleans())
    def move(self, p: int, c: int, to: int, before: bool) -> None:
        path = self.pick_path(p)
        cell, anchor = self.pick_cell(path, c), self.pick_cell(path, to)
        if cell is not None and anchor is not None:
            key = "before" if before else "after"
            self.call(
                "notebook.edit", path=path, ops=[{"op": "move", "cell_id": cell[0], key: anchor[0]}]
            )

    @rule(
        p=st.integers(0, 9),
        c=st.integers(0, 50),
        name=st.sampled_from([*NAMES, "_", "2bad", "lambda", "load"]),
    )
    def rename(self, p: int, c: int, name: str) -> None:
        path = self.pick_path(p)
        cell = self.pick_cell(path, c)
        if cell is not None:
            self.call(
                "notebook.edit", path=path, ops=[{"op": "rename", "cell_id": cell[0], "name": name}]
            )

    @rule(
        p=st.integers(0, 9),
        c=st.integers(0, 50),
        kind=st.sampled_from(["python", "sql", "markdown", "setup", "chart"]),
    )
    def set_kind(self, p: int, c: int, kind: str) -> None:
        path = self.pick_path(p)
        cell = self.pick_cell(path, c)
        if cell is not None:
            self.call(
                "notebook.edit",
                path=path,
                ops=[{"op": "set_kind", "cell_id": cell[0], "kind": kind}],
            )

    @rule(p=st.integers(0, 9), c=st.integers(0, 50), disabled=st.booleans(), bad=st.booleans())
    def set_config(self, p: int, c: int, disabled: bool, bad: bool) -> None:
        path = self.pick_path(p)
        cell = self.pick_cell(path, c)
        if cell is not None:
            config: dict[str, Any] = {"disabled": "yes"} if bad else {"disabled": disabled}
            self.call(
                "notebook.edit",
                path=path,
                ops=[{"op": "set_config", "cell_id": cell[0], "config": config}],
            )

    @rule(p=st.integers(0, 9), c=st.integers(0, 50), bad=st.sampled_from(BAD_IDS))
    def batch_with_failure(self, p: int, c: int, bad: str) -> None:
        """A batch whose last op fails must change nothing."""
        path = self.pick_path(p)
        cell = self.pick_cell(path, c)
        ops: list[dict[str, Any]] = [{"op": "insert", "source": "zz = 1"}]
        if cell is not None:
            ops.append({"op": "replace", "cell_id": cell[0], "source": "zz = 2"})
        ops.append({"op": "delete", "cell_id": bad})
        self.call("notebook.edit", path=path, ops=ops)

    # -- running -----------------------------------------------------------------------

    @rule(
        p=st.integers(0, 9),
        picks=st.lists(st.integers(0, 50), min_size=1, max_size=3),
        wait=st.booleans(),
    )
    def run_cells(self, p: int, picks: list[int], wait: bool) -> None:
        path = self.pick_path(p)
        cells = [self.pick_cell(path, i) for i in picks]
        ids = [c[0] for c in cells if c is not None] or [BAD_IDS[0]]
        self.call(
            "notebook.run", path=path, target={"kind": "cells", "ids": ids}, wait=wait, timeout_s=30
        )

    @rule(
        p=st.integers(0, 9),
        kind=st.sampled_from(["all", "stale", "above", "below"]),
        c=st.integers(0, 50),
    )
    def run_scope(self, p: int, kind: str, c: int) -> None:
        path = self.pick_path(p)
        target: dict[str, Any] = {"kind": kind}
        if kind in ("above", "below"):
            cell = self.pick_cell(path, c)
            target["id"] = cell[0] if cell is not None else BAD_IDS[1]
        self.call("notebook.run", path=path, target=target, timeout_s=30)

    @rule(
        p=st.integers(0, 9), action=st.sampled_from(["status", "interrupt", "restart", "shutdown"])
    )
    def kernel(self, p: int, action: str) -> None:
        self.call("notebook.kernel", path=self.pick_path(p), action=action)

    @rule()
    def settle(self) -> None:
        _run(self.loop, self.driver.check(settle=True))
        self.steps_since_settle = 0

    # -- reading --------------------------------------------------------------------------

    @rule(
        p=st.integers(0, 9),
        some=st.booleans(),
        c=st.integers(0, 50),
        src=st.booleans(),
        outs=st.booleans(),
    )
    def read(self, p: int, some: bool, c: int, src: bool, outs: bool) -> None:
        path = self.pick_path(p)
        cell = self.pick_cell(path, c)
        cells = [cell[0]] if some and cell is not None else None
        self.call("notebook.read", path=path, cells=cells, include_source=src, include_outputs=outs)

    @rule(
        p=st.integers(0, 9),
        c=st.integers(0, 50),
        part=st.sampled_from(["all", "text", "error", "image", "chart", "table", "widget"]),
    )
    def output(self, p: int, c: int, part: str) -> None:
        path = self.pick_path(p)
        cell = self.pick_cell(path, c)
        self.call("notebook.output", path=path, cell=cell[0] if cell else BAD_IDS[0], part=part)

    @rule(
        p=st.integers(0, 9),
        what=st.sampled_from(["variables", "frame", "value"]),
        name=st.sampled_from([*NAMES, None]),
        sort=st.sampled_from([None, "v desc", "k", "nope"]),
        filt=st.sampled_from(
            [None, "SELECT * FROM frame WHERE v > 1", "DROP TABLE frame", "SELECT 1; SELECT 2"]
        ),
    )
    def inspect(
        self, p: int, what: str, name: str | None, sort: str | None, filt: str | None
    ) -> None:
        self.call(
            "notebook.inspect",
            path=self.pick_path(p),
            what=what,
            name=name,
            sort=sort,
            filter_sql=filt,
        )

    @rule(
        p=st.integers(0, 9),
        c=st.integers(0, 50),
        whole=st.booleans(),
        direction=st.sampled_from(["both", "up", "down"]),
    )
    def graph(self, p: int, c: int, whole: bool, direction: str) -> None:
        path = self.pick_path(p)
        cell = None if whole else self.pick_cell(path, c)
        self.call("notebook.graph", path=path, cell=cell[0] if cell else None, direction=direction)

    @rule(
        p=st.integers(0, 9),
        action=st.sampled_from(["list", "get", "set"]),
        value=st.integers(0, 10),
        real=st.booleans(),
    )
    def widget(self, p: int, action: str, value: int, real: bool) -> None:
        path = self.pick_path(p)
        widgets = self.widget_ids(path)
        model = widgets[0] if real and widgets else "w-missing"
        args: dict[str, Any] = {"path": path, "action": action}
        if action != "list":
            args["model_id"] = model
        if action == "set":
            args["state"] = {"value": value}
        self.call("notebook.widget", **args)

    @rule(
        p=st.integers(0, 9),
        action=st.sampled_from(["info", "list", "packages", "install", "materialize", "switch"]),
        pkg=st.sampled_from(["simtable", "requests-nope"]),
        env=st.sampled_from([None, "default"]),
    )
    def env(self, p: int, action: str, pkg: str, env: str | None) -> None:
        packages = [pkg] if action == "install" else []
        self.call("notebook.env", path=self.pick_path(p), action=action, packages=packages, env=env)

    @rule(
        p=st.integers(0, 9),
        reactivity=st.sampled_from([None, "autorun", "lazy"]),
        env=st.sampled_from([None, "default"]),
    )
    def settings(self, p: int, reactivity: str | None, env: str | None) -> None:
        self.call("notebook.settings", path=self.pick_path(p), reactivity=reactivity, env=env)

    @rule(
        path=st.sampled_from(BAD_PATHS),
        tool=st.sampled_from(["notebook.read", "notebook.run", "notebook.create"]),
    )
    def outside(self, path: str, tool: str) -> None:
        args: dict[str, Any] = {"path": path}
        if tool == "notebook.run":
            args["target"] = {"kind": "all"}
        self.call(tool, **args)

    @rule()
    def digest(self) -> None:
        _run(self.loop, self.driver.digest())


class CollaborationMachine(AgentMachine):
    """The agent and a person in the same notebooks."""

    def person(self, coro: Any) -> Any:
        result = _run(self.loop, coro)
        _run(self.loop, self.driver.check())
        return result

    def agent_last_cell(self, path: str) -> str | None:
        for step in reversed(self.driver.steps):
            if (
                step.actor == "agent"
                and step.action == "notebook.edit"
                and step.args.get("path") == path
            ):
                for op in step.args.get("ops", []):
                    if isinstance(op, dict) and op.get("cell_id"):
                        return str(op["cell_id"])
        return None

    @rule(
        p=st.integers(0, 9),
        c=st.integers(0, 50),
        text=st.sampled_from(["\n# note", "\nx_p = 1", " "]),
    )
    def person_types_in_agents_cell(self, p: int, c: int, text: str) -> None:
        path = self.pick_path(p)
        live = {cid for cid, _, _, _ in self.cells(path)}
        target = self.agent_last_cell(path)
        if target not in live:
            cell = self.pick_cell(path, c)
            target = cell[0] if cell else None
        if target is None:
            return
        source = next(s for cid, _, _, s in self.cells(path) if cid == target)
        ops = [ReplaceCellOp(cell_id=target, source=source + text)]
        self.person(self.driver.person_edit(path, ops))

    @rule(p=st.integers(0, 9), c=st.integers(0, 50), spec=cell_specs())
    def person_inserts(self, p: int, c: int, spec: CellSpec) -> None:
        path = self.pick_path(p)
        anchor = self.pick_cell(path, c)
        op = InsertCellOp(
            kind=spec.kind,
            source=spec.source,
            name=spec.name,
            meta=dict(spec.meta),
            after=anchor[0] if anchor else None,
        )
        self.person(self.driver.person_edit(path, [op]))

    @rule(p=st.integers(0, 9), c=st.integers(0, 50))
    def person_deletes(self, p: int, c: int) -> None:
        path = self.pick_path(p)
        cell = self.pick_cell(path, c)
        if cell is not None:
            self.person(self.driver.person_edit(path, [DeleteCellOp(cell_id=cell[0])]))

    @rule(p=st.integers(0, 9), c=st.integers(0, 50), cut=st.integers(0, 20))
    def person_edits(self, p: int, c: int, cut: int) -> None:
        path = self.pick_path(p)
        cell = self.pick_cell(path, c)
        if cell is not None and cell[3]:
            old = cell[3][cut % len(cell[3]) :][:2]
            op = EditCellOp(cell_id=cell[0], edits=[NotebookTextEdit(old=old, new=old + " ")])
            self.person(self.driver.person_edit(path, [op]))

    @rule(p=st.integers(0, 9), c=st.integers(0, 50))
    def person_runs(self, p: int, c: int) -> None:
        path = self.pick_path(p)
        cell = self.pick_cell(path, c)
        if cell is not None:
            self.person(self.driver.person_run(path, RunCells(ids=[cell[0]])))

    @rule(p=st.integers(0, 9), value=st.integers(0, 10))
    def person_moves_widget(self, p: int, value: int) -> None:
        path = self.pick_path(p)
        widgets = self.widget_ids(path)
        if widgets:
            self.person(self.driver.person_widget(path, widgets[0], value))


REGRESSIONS = Path(__file__).resolve().parents[2] / "tests" / "sim_regressions"


def run_machine(
    machine: type[AgentMachine],
    *,
    max_examples: int,
    stateful_step_count: int,
    regressions: Path = REGRESSIONS,
    shrink: bool = True,
) -> None:
    """Run ``machine``; on a failure, save the (shrunk) steps and re-raise."""
    phases = list(Phase)
    from hypothesis.stateful import run_state_machine_as_test

    try:
        # hypothesis ships run_state_machine_as_test without annotations.
        run_state_machine_as_test(  # type: ignore[no-untyped-call]
            machine,
            settings=settings(
                max_examples=max_examples,
                stateful_step_count=stateful_step_count,
                deadline=None,
                database=None,
                phases=phases if shrink else [p for p in phases if p is not Phase.shrink],
            ),
        )
    except BaseException:
        save_regression(machine.__name__, machine.last_steps, regressions)
        raise


def save_regression(name: str, steps: list[Step], directory: Path = REGRESSIONS) -> Path:
    payload = {"machine": name, "steps": [asdict(s) for s in steps]}
    text = json.dumps(payload, indent=1, sort_keys=True, default=str)
    digest = hashlib.sha256(text.encode()).hexdigest()[:12]
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}-{digest}.json"
    path.write_text(text + "\n", encoding="utf-8")
    return path


async def replay(steps: list[dict[str, Any]], driver: SimDriver) -> None:
    """Run saved steps against ``driver``, checking invariants after each."""
    from alkera_notebook.tools.models import RunCells as _RunCells

    for raw in steps:
        actor, action, args = raw["actor"], raw["action"], raw.get("args", {})
        if actor == "agent":
            await driver.agent(action, args)
        elif actor == "digest":
            await driver.digest()
        elif actor == "person" and action == "edit":
            from pydantic import TypeAdapter

            from alkera_notebook.tools.models import NotebookOp

            ops = TypeAdapter(list[NotebookOp]).validate_python(args["ops"])
            await driver.person_edit(args["path"], ops)
        elif actor == "person" and action == "run":
            await driver.person_run(args["path"], _RunCells.model_validate(args["target"]))
        elif actor == "person" and action == "create":
            cells = [(str(k), str(src), str(n)) for k, src, n in args["cells"]]
            await driver.person_create(args["path"], cells)
        await driver.check()
    await driver.check(settle=True)


__all__ = [
    "BAD_IDS",
    "BAD_PATHS",
    "PATHS",
    "REGRESSIONS",
    "TARGET_FACTORY",
    "AgentMachine",
    "CollaborationMachine",
    "replay",
    "run_machine",
    "save_regression",
]
