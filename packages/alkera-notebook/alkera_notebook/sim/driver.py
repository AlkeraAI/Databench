"""The simulator's driver: one agent and one person acting on a target, with
the ledger the invariants compare against.

Every agent action goes through :func:`alkera_notebook.tools.call_tool`, exactly
as a harness would call it, behind a :class:`StanceGatekeeper` that answers the
way a permission mode does and records each subject it was asked about. Every
person action goes straight to the person's port, as the editor would. Each
acknowledged document change is applied to the ledger's oracle as well; a
refusal the store gives must be the refusal the oracle gives.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

from alkera_notebook.digest import Digest, turn_digest
from alkera_notebook.sim.invariants import (
    CallRecord,
    InvariantViolationError,
    SimLedger,
    Violation,
    check_all,
)
from alkera_notebook.sim.oracle import DocumentModel, OpError
from alkera_notebook.sim.targets import SimTarget
from alkera_notebook.tools import TOOLS, call_tool, gate_effect
from alkera_notebook.tools.functions import new_notebook_cells
from alkera_notebook.tools.gates import GateEffect, GateSubject, GateVerdict
from alkera_notebook.tools.models import (
    EngineRunTarget,
    InsertCellOp,
    NotebookCreateInput,
    NotebookCreateOutput,
    NotebookEditInput,
    NotebookEditOutput,
    NotebookOp,
    NotebookRunOutput,
    NotebookSettingsInput,
    NotebookWidgetOutput,
    SetSettingOp,
)
from alkera_notebook.tools.port import ActorRef, NotebookPort, NotebookToolError, OpsRecord

Mode = Literal["default", "auto", "plan", "read_only", "bypass"]

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
#: Refusals the rules do not order among themselves: an insert that is both
#: misnamed and misplaced may be refused for either.
_ORDER_FREE = frozenset({"invalid_name", "unknown_kind", "setup_must_be_first", "invalid_config"})

AGENT = ActorRef(kind="agent", id="agent:sim", display_name="Sim agent")
PERSON = ActorRef(kind="person", id="person:bob", display_name="Bob")


_SQL_READ = ("select", "with", "show", "describe", "explain")
_SQL_DESTROY = ("drop", "truncate", "delete")


def plan_severity(subject: GateSubject) -> Literal["read", "write", "destroy"]:
    """The simulator's model of how a harness classifies a run's plan: a SQL
    statement that reads is a read, one that drops or deletes is a destroy,
    everything else (every Python cell, any statement not known before the run)
    is a write; the most severe step wins."""
    plan = subject.plan
    worst: Literal["read", "write", "destroy"] = "read"
    for step in plan.steps if plan is not None else []:
        if step.kind != "sql" or step.sql is None or step.interpolated:
            level: Literal["read", "write", "destroy"] = "write"
        else:
            head = step.sql.lstrip().split(None, 1)[0].lower() if step.sql.strip() else ""
            level = "read" if head in _SQL_READ else "destroy" if head in _SQL_DESTROY else "write"
        if level == "destroy" or (level == "write" and worst == "read"):
            worst = level
    return worst


@dataclass
class StanceGatekeeper:
    """Answers a gate the way a permission mode does, with a person who says
    ``person_says`` whenever the mode asks."""

    mode: Mode = "default"
    person_says: bool = True
    asked: list[GateSubject] = field(default_factory=list)

    def decision(self, subject: GateSubject) -> Literal["allow", "ask", "reject"]:
        effect = subject.effect
        severity = plan_severity(subject) if effect is GateEffect.RUN_CELLS else "write"
        if subject.affects_others:
            severity = "destroy"
        if severity == "read":
            return "allow"
        if self.mode == "bypass":
            return "allow"
        if self.mode == "read_only":
            return "reject"
        if self.mode == "plan":
            return "allow" if effect is GateEffect.FILE_EDIT else "reject"
        if self.mode == "auto":
            return "ask" if severity == "destroy" else "allow"
        return "allow" if effect is GateEffect.FILE_EDIT else "ask"

    async def __call__(self, subject: GateSubject) -> GateVerdict:
        self.asked.append(subject)
        decision = self.decision(subject)
        if decision == "allow" or (decision == "ask" and self.person_says):
            return GateVerdict(True)
        return GateVerdict(False, f"{subject.title}: not allowed in {self.mode} mode")


@dataclass
class MemoryCursors:
    """Digest cursors held in memory, for the driver's agent."""

    at: dict[str, datetime] = field(default_factory=dict)

    async def get(self, path: str) -> datetime | None:
        return self.at.get(path)

    async def set(self, path: str, at: datetime) -> None:
        self.at[path] = at

    async def paths(self) -> list[str]:
        return sorted(self.at)


@dataclass
class Step:
    """One recorded step, replayable against any target."""

    actor: Literal["agent", "person", "check", "digest"]
    action: str
    args: dict[str, Any] = field(default_factory=dict)


class SimDriver:
    def __init__(self, target: SimTarget, *, gatekeeper: StanceGatekeeper | None = None) -> None:
        self.target = target
        self.gatekeeper = gatekeeper or StanceGatekeeper()
        self.ledger = SimLedger(agent_id=AGENT.id, gate_mode=self.gatekeeper.mode)
        self.agent_host = target.host(AGENT)
        self.person_host = target.host(PERSON)
        self.cursors = MemoryCursors()
        self.steps: list[Step] = []
        self.person_actions: list[tuple[str, str | None]] = []
        """(path, cell id) of person document actions since the agent's last digest."""

    # -- the oracle -------------------------------------------------------------

    def _apply_oracle(
        self,
        path: str,
        ops: Sequence[NotebookOp],
        created: Sequence[str],
    ) -> list[Violation]:
        oracle = self.ledger.oracles.setdefault(path, DocumentModel())
        ids = iter(created)
        try:
            doc, oracle_created, _ = oracle.apply(ops, lambda: next(ids))
        except (OpError, StopIteration) as exc:
            return [
                Violation(
                    "oracle_document", path, f"store accepted a batch the rules refuse: {exc}"
                )
            ]
        self.ledger.oracles[path] = doc
        self.ledger.ids_seen.setdefault(path, set()).update(oracle_created)
        return []

    def _expect_refusal(
        self, path: str, ops: Sequence[NotebookOp], exc: NotebookToolError
    ) -> list[Violation]:
        oracle = self.ledger.oracles.get(path)
        if oracle is None:
            return []
        if exc.op_index is None:
            # A batch refused for one of its ops must say which op and why.
            try:
                oracle.apply(ops, lambda: "0000000000")
            except OpError as expected:
                return [
                    Violation(
                        "op_error_shape",
                        path,
                        f"store refused with {exc.code!r} and no op index; the rules refuse op "
                        f"{expected.index} with {expected.code}",
                    )
                ]
            return []
        counter = iter(range(10**6))
        try:
            oracle.apply(ops, lambda: f"oracle{next(counter):05d}")
        except OpError as expected:
            if expected.index == exc.op_index and {expected.code, exc.code} <= _ORDER_FREE:
                # One op that breaks two rules may be refused for either.
                return []
            if (expected.code, expected.index) != (exc.code, exc.op_index):
                return [
                    Violation(
                        "oracle_document",
                        path,
                        f"store refused op {exc.op_index} with {exc.code}, "
                        f"rules refuse op {expected.index} with {expected.code}",
                    )
                ]
            return []
        return [Violation("oracle_document", path, f"store refused a valid batch: {exc.code}")]

    # -- the agent ----------------------------------------------------------------

    async def agent(self, tool: str, args: dict[str, Any]) -> BaseModel | Exception:
        """One agent tool call; a refusal is returned, not raised."""
        self.steps.append(Step("agent", tool, json.loads(json.dumps(args, default=str))))
        definition = TOOLS[tool]
        try:
            parsed = definition.input.model_validate(args)
        except ValidationError as exc:
            return exc
        effect = gate_effect(tool, parsed)
        asked_before = len(self.gatekeeper.asked)
        violations: list[Violation] = []
        try:
            result = await call_tool(tool, parsed, host=self.agent_host, gatekeeper=self.gatekeeper)
        except NotebookToolError as exc:
            asked = len(self.gatekeeper.asked) > asked_before
            self.ledger.calls.append(CallRecord(tool, effect, asked, False, False, True, exc.code))
            if isinstance(parsed, NotebookEditInput):
                path = self.agent_host.resolve(parsed.path)
                violations.extend(self._expect_refusal(path, parsed.ops, exc))
            if violations:
                raise InvariantViolationError(violations) from exc
            return exc
        asked = len(self.gatekeeper.asked) > asked_before
        ran_code, result_ok = self._judge_result(tool, result)
        self.ledger.calls.append(CallRecord(tool, effect, asked, True, ran_code, result_ok))
        if isinstance(result, NotebookCreateOutput):
            assert isinstance(parsed, NotebookCreateInput)
            ops: list[NotebookOp] = list(
                new_notebook_cells(
                    [
                        InsertCellOp(kind=c.kind, source=c.source, name=c.name or "_")
                        for c in parsed.cells
                    ]
                )
            )
            ops += [SetSettingOp(key=k, value=v) for k, v in parsed.settings.items()]
            violations += self._apply_oracle(result.path, ops, [c.id for c in result.cells])
        elif isinstance(result, NotebookEditOutput) and not result.repeat:
            assert isinstance(parsed, NotebookEditInput)
            violations += self._apply_oracle(result.path, parsed.ops, result.created)
        elif isinstance(parsed, NotebookSettingsInput) and parsed.changes():
            path = self.agent_host.resolve(parsed.path)
            ops = [SetSettingOp(key=k, value=v) for k, v in parsed.changes().items()]
            violations += self._apply_oracle(path, ops, [])
        await self._touch(result)
        if violations:
            raise InvariantViolationError(violations)
        return result

    def _judge_result(self, tool: str, result: BaseModel) -> tuple[bool, bool]:
        """Whether the result reports code having run, and whether it is a valid
        result of the tool that says what ran and how."""
        output = TOOLS[tool].output
        try:
            output.model_validate(result.model_dump())
        except ValidationError:
            return False, False
        run = result if isinstance(result, NotebookRunOutput) else None
        if isinstance(result, NotebookWidgetOutput):
            run = result.run
        if run is None or run.status == "needs_confirmation":
            return tool in ("notebook.kernel", "notebook.inspect", "notebook.widget"), True
        complete = bool(run.reactivity) and bool(run.env.env_id) and run.plan is not None
        return True, complete

    async def _touch(self, result: BaseModel) -> None:
        path = getattr(result, "path", None)
        if isinstance(path, str) and path not in self.cursors.at:
            port = await self.agent_host.open(path)
            activity = await port.activity(EPOCH)
            await self.cursors.set(path, activity.cursor)

    # -- the person ---------------------------------------------------------------

    async def person_port(self, path: str) -> NotebookPort:
        return await self.person_host.open(path)

    async def person_create(self, path: str, cells: Sequence[tuple[str, str, str]]) -> list[str]:
        self.steps.append(
            Step("person", "create", {"path": path, "cells": [list(c) for c in cells]})
        )
        inserts = [InsertCellOp(kind=k, source=s, name=n) for k, s, n in cells]
        port = await self.person_host.create(path, inserts, {})
        view = await port.read(None, include_source=False, include_outputs=False)
        ops: list[NotebookOp] = list(inserts)
        violations = self._apply_oracle(path, ops, [c.id for c in view.cells])
        if violations:
            raise InvariantViolationError(violations)
        return [c.id for c in view.cells]

    async def person_edit(
        self, path: str, ops: Sequence[NotebookOp]
    ) -> OpsRecord | NotebookToolError:
        self.steps.append(
            Step(
                "person", "edit", {"path": path, "ops": [op.model_dump(mode="json") for op in ops]}
            )
        )
        port = await self.person_port(path)
        try:
            record = await port.apply(ops, None)
        except NotebookToolError as exc:
            violations = self._expect_refusal(path, ops, exc)
            if violations:
                raise InvariantViolationError(violations) from exc
            return exc
        violations = self._apply_oracle(path, ops, record.created)
        if violations:
            raise InvariantViolationError(violations)
        for op in ops:
            self.person_actions.append((path, getattr(op, "cell_id", None)))
        for cid in record.created:
            self.person_actions.append((path, cid))
        return record

    async def person_run(self, path: str, target: EngineRunTarget) -> Exception | None:
        self.steps.append(Step("person", "run", {"path": path, "target": target.model_dump()}))
        port = await self.person_port(path)
        try:
            await port.run(target, confirm_expensive=True)
        except NotebookToolError as exc:
            return exc
        self.person_actions.append((path, None))
        return None

    async def person_widget(self, path: str, model_id: str, value: Any) -> None:
        self.steps.append(Step("person", "widget", {"path": path, "model_id": model_id}))
        port = await self.person_port(path)
        try:
            await port.set_widget(model_id, {"value": value})
        except NotebookToolError:
            return
        self.person_actions.append((path, None))

    # -- checks ---------------------------------------------------------------------

    async def check(self, *, settle: bool = False) -> None:
        if settle:
            await self.target.settle()
        violations = check_all(self.target, self.ledger, settled=settle)
        if violations:
            raise InvariantViolationError(violations)

    async def digest(self) -> Digest:
        """The agent's digest for its next turn, with its accounting checked:
        every item by someone else is shown or counted, and every person action
        since the last digest is in the activity it was built from."""
        self.steps.append(Step("digest", "digest"))
        expected: dict[str, int] = {}
        violations: list[Violation] = []
        for path in await self.cursors.paths():
            since = await self.cursors.get(path)
            assert since is not None
            port = await self.agent_host.open(path)
            activity = await port.activity(since)
            others = [i for i in activity.items if i.actor.id != AGENT.id]
            expected[path] = len(others)
            seen_cells = {i.cell_id for i in others}
            for action_path, cid in self.person_actions:
                if action_path == path and cid is not None and cid not in seen_cells:
                    violations.append(
                        Violation(
                            "digest_coverage", path, f"person action on {cid} missing from activity"
                        )
                    )
        digest = await turn_digest(self.agent_host, self.cursors)
        accounted = {d.path: d.shown + d.omitted for d in digest.notebooks}
        for path, count in expected.items():
            if count and path not in digest.omitted_notebooks and accounted.get(path, 0) != count:
                violations.append(
                    Violation(
                        "digest_coverage",
                        path,
                        f"{count} items by others, digest accounts for {accounted.get(path, 0)}",
                    )
                )
        self.person_actions = [a for a in self.person_actions if a[0] not in expected]
        if violations:
            raise InvariantViolationError(violations)
        return digest

    async def close(self) -> None:
        await self.target.close()
        survivors = list(self.target.live_kernel_processes())
        if survivors:
            raise InvariantViolationError(
                [
                    Violation(
                        "no_orphan_kernels", "*", f"kernel processes alive after close: {survivors}"
                    )
                ]
            )


__all__ = [
    "AGENT",
    "PERSON",
    "MemoryCursors",
    "Mode",
    "SimDriver",
    "StanceGatekeeper",
    "Step",
]
