"""The simulator: scripted scenarios, bounded random runs with every invariant,
proof that the invariants catch planted bugs, and replay of saved regressions.

``sim_soak`` (long random runs) is opt in: set ``ALKERA_SIM_SOAK=1``.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from alkera_notebook.document.ops import NotebookOpsResult
from alkera_notebook.sim import machines, reference
from alkera_notebook.sim.driver import SimDriver, StanceGatekeeper
from alkera_notebook.sim.invariants import InvariantViolationError
from alkera_notebook.sim.machines import AgentMachine, CollaborationMachine, replay, run_machine
from alkera_notebook.sim.reference import ReferenceWorkspace
from alkera_notebook.sim.targets import ReferenceTarget
from alkera_notebook.tools import catalog
from alkera_notebook.tools.gates import GateEffect
from alkera_notebook.tools.models import ReplaceCellOp, RunCells
from alkera_notebook.tools.port import NotebookToolError

REGRESSIONS = Path(__file__).parent / "sim_regressions"
PATH = "nb.alknb.py"
SETUP = "import pandas as pd\nimport alkera"


def _driver(mode: str = "default", *, check_file: bool = False) -> SimDriver:
    target = ReferenceTarget(ReferenceWorkspace(seed=0), check_file=check_file)
    return SimDriver(target, gatekeeper=StanceGatekeeper(mode))  # type: ignore[arg-type]


async def _create(driver: SimDriver, *sources: str) -> list[str]:
    cells = [{"kind": "setup", "source": SETUP}, *({"source": s} for s in sources)]
    out = await driver.agent("notebook.create", {"path": PATH, "cells": cells})
    return [c.id for c in out.cells]  # type: ignore[union-attr]


# -- scripted scenarios ----------------------------------------------------------------


async def test_scenario_build_run_fix_and_rerun() -> None:
    driver = _driver()
    ids = await _create(driver, "df = pd.DataFrame({'v': [1, 2]})", "total = df.w.sum()\ntotal")
    out = await driver.agent(
        "notebook.run", {"path": PATH, "target": {"kind": "cells", "ids": [ids[2]]}}
    )
    assert out.cells[-1].output.error.ename == "AttributeError"  # type: ignore[union-attr]
    await driver.agent(
        "notebook.edit",
        {
            "path": PATH,
            "ops": [{"op": "edit", "cell_id": ids[2], "edits": [{"old": "df.w", "new": "df.v"}]}],
        },
    )
    out = await driver.agent(
        "notebook.run", {"path": PATH, "target": {"kind": "cells", "ids": [ids[2]]}}
    )
    assert out.cells[-1].status == "fresh" and out.cells[-1].output.text.content == "np.int64(3)"  # type: ignore[union-attr]
    await driver.check(settle=True)
    await driver.close()


async def test_scenario_a_person_types_in_the_agents_cell_and_gets_a_notice() -> None:
    driver = _driver()
    ids = await _create(driver, "x = 1")
    await driver.person_edit(PATH, [ReplaceCellOp(cell_id=ids[1], source="x = 1  # Bob")])
    out = await driver.agent(
        "notebook.edit",
        {
            "path": PATH,
            "ops": [{"op": "edit", "cell_id": ids[1], "edits": [{"old": "x = 1", "new": "x = 2"}]}],
        },
    )
    assert [n.kind for n in out.notices] == ["concurrent_edit"]  # type: ignore[union-attr]
    assert out.notices[0].by == "Bob"  # type: ignore[union-attr]
    assert driver.target.document(PATH)[1][3] == "x = 2  # Bob"
    digest = await driver.digest()
    assert f"Bob edited {ids[1]}" in digest.text
    await driver.check(settle=True)


async def test_scenario_a_person_running_and_the_agent_interrupting_asks_first() -> None:
    driver = _driver("auto")
    ids = await _create(driver, "x = 1")
    await driver.person_run(PATH, RunCells(ids=ids[1:]))
    await driver.agent("notebook.kernel", {"path": PATH, "action": "interrupt"})
    (subject,) = [s for s in driver.gatekeeper.asked if s.tool == "notebook.kernel"]
    assert subject.affects_others == ("Bob",)
    assert driver.gatekeeper.decision(subject) == "ask"


async def test_scenario_refused_runs_execute_nothing_in_read_only() -> None:
    driver = _driver("read_only")
    driver.gatekeeper.mode = "bypass"
    ids = await _create(driver, "x = 1")
    driver.gatekeeper.mode = "read_only"
    out = await driver.agent(
        "notebook.run", {"path": PATH, "target": {"kind": "cells", "ids": ids}}
    )
    assert getattr(out, "code", None) == "permission_denied"
    assert driver.target.executions(PATH) == []
    await driver.check(settle=True)


async def test_scenario_a_read_only_sql_plan_runs_without_asking() -> None:
    driver = _driver("read_only")
    driver.gatekeeper.mode = "bypass"
    made = await driver.agent(
        "notebook.create", {"path": PATH, "cells": [{"kind": "sql", "source": "SELECT 1 AS v"}]}
    )
    # Create gave the notebook its setup import; that Python step has run.
    setup, query = [c.id for c in made.cells]  # type: ignore[union-attr]
    await driver.agent("notebook.run", {"path": PATH, "target": {"kind": "cells", "ids": [setup]}})
    driver.gatekeeper.mode = "read_only"
    out = await driver.agent(
        "notebook.run", {"path": PATH, "target": {"kind": "cells", "ids": [query]}}
    )
    assert out.status == "finished"  # type: ignore[union-attr]
    assert [c.id for c in out.cells] == [query]  # type: ignore[union-attr]
    await driver.check(settle=True)


async def test_a_plan_that_still_has_the_setup_import_to_run_is_not_read_only() -> None:
    """The setup block is Python, so a run that includes it is refused in
    read-only mode like any other Python step."""
    driver = _driver("read_only")
    driver.gatekeeper.mode = "bypass"
    await driver.agent(
        "notebook.create", {"path": PATH, "cells": [{"kind": "sql", "source": "SELECT 1 AS v"}]}
    )
    driver.gatekeeper.mode = "read_only"
    out = await driver.agent("notebook.run", {"path": PATH, "target": {"kind": "all"}})
    assert isinstance(out, NotebookToolError), out
    await driver.check(settle=True)


async def test_the_digest_accounts_for_every_person_action() -> None:
    driver = _driver()
    ids = await _create(driver, "a = 1", "b = 2")
    for cid in ids[1:]:
        await driver.person_edit(PATH, [ReplaceCellOp(cell_id=cid, source="z = 0")])
    digest = await driver.digest()
    (part,) = digest.notebooks
    assert part.shown + part.omitted == 2


# -- random runs ---------------------------------------------------------------------------


@pytest.fixture
def regression_dir(tmp_path: Path) -> Iterator[Path]:
    yield tmp_path / "regressions"


@pytest.mark.parametrize("machine", [AgentMachine, CollaborationMachine], ids=lambda m: m.__name__)
def test_bounded_random_run_holds_every_invariant(
    machine: type[AgentMachine], regression_dir: Path
) -> None:
    run_machine(machine, max_examples=25, stateful_step_count=30, regressions=regression_dir)
    assert not regression_dir.exists() or not list(regression_dir.iterdir())


@pytest.mark.skipif(
    not os.environ.get("ALKERA_SIM_SOAK"), reason="set ALKERA_SIM_SOAK=1 for long runs"
)
@pytest.mark.parametrize("machine", [AgentMachine, CollaborationMachine], ids=lambda m: m.__name__)
def test_sim_soak(machine: type[AgentMachine]) -> None:
    run_machine(
        machine,
        max_examples=int(os.environ.get("ALKERA_SIM_SOAK_EXAMPLES", "2000")),
        stateful_step_count=80,
    )


# -- the invariants catch planted bugs ---------------------------------------------------------


def test_random_search_finds_an_ungated_run_and_saves_it(
    monkeypatch: pytest.MonkeyPatch, regression_dir: Path
) -> None:
    honest = catalog.gate_effect
    monkeypatch.setattr(
        catalog,
        "gate_effect",
        lambda name, args: GateEffect.READ if name == "notebook.run" else honest(name, args),
    )
    with pytest.raises(BaseException) as raised:
        run_machine(
            CollaborationMachine,
            max_examples=100,
            stateful_step_count=30,
            regressions=regression_dir,
        )
    error = raised.value
    leaves = list(error.exceptions) if isinstance(error, BaseExceptionGroup) else [error]
    assert any(isinstance(e, InvariantViolationError) and "gated" in str(e) for e in leaves)
    (saved,) = regression_dir.glob("*.json")
    steps = json.loads(saved.read_text(encoding="utf-8"))["steps"]
    assert any(s["action"] == "notebook.run" for s in steps)


async def test_a_status_reported_fresh_while_stale_is_caught(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    honest = reference.ReferenceNotebook.statuses

    def lying(self: reference.ReferenceNotebook) -> dict[str, Any]:
        return {k: ("fresh" if v == "stale" else v) for k, v in honest(self).items()}

    monkeypatch.setattr(reference.ReferenceNotebook, "statuses", lying)
    driver = _driver()
    ids = await _create(driver, "a = 1", "b = a + 1")
    await driver.agent("notebook.run", {"path": PATH, "target": {"kind": "all"}})
    await driver.agent("notebook.settings", {"path": PATH, "reactivity": "lazy"})
    await driver.agent(
        "notebook.edit",
        {"path": PATH, "ops": [{"op": "replace", "cell_id": ids[1], "source": "a = 2"}]},
    )
    await driver.agent("notebook.run", {"path": PATH, "target": {"kind": "cells", "ids": [ids[1]]}})
    with pytest.raises(InvariantViolationError, match="statuses_recomputed"):
        await driver.check(settle=True)


async def test_a_store_that_alters_what_it_was_told_is_caught(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    honest = reference.ReferenceNotebook.apply

    def altering(
        self: reference.ReferenceNotebook, ops: Any, base: Any, actor: Any, submit_id: Any = None
    ) -> Any:
        record = honest(self, ops, base, actor, submit_id)
        from dataclasses import replace

        last = self.doc.live()[-1]
        self.doc.cells[last.id] = replace(last, source=last.source + " ")
        return record

    monkeypatch.setattr(reference.ReferenceNotebook, "apply", altering)
    driver = _driver()
    with pytest.raises(InvariantViolationError, match="oracle_document"):
        await _create(driver, "a = 1")
        await driver.check()


async def test_runs_that_interleave_are_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    honest = reference.ReferenceNotebook._finish_step

    def logging_into_the_first_run(
        self: reference.ReferenceNotebook, run: Any, step: Any, result: Any, error: Any
    ) -> None:
        honest(self, run, step, result, error)
        if self.runs and self.runs[0] is not run:
            self.executions.append((self.runs[0].run_id, step.cell_id))

    monkeypatch.setattr(reference.ReferenceNotebook, "_finish_step", logging_into_the_first_run)
    driver = _driver()
    ids = await _create(driver, "a = 1", "b = 2")
    await driver.agent("notebook.run", {"path": PATH, "target": {"kind": "cells", "ids": [ids[1]]}})
    await driver.agent("notebook.run", {"path": PATH, "target": {"kind": "cells", "ids": [ids[2]]}})
    with pytest.raises(InvariantViolationError, match=r"one_run_at_a_time|outputs_name_runs"):
        await driver.check(settle=True)


async def test_a_refusal_the_rules_do_not_give_is_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    from alkera_notebook.tools import NotebookToolError

    honest = reference.ReferenceNotebook.apply

    def refusing_edits(self: Any, ops: Any, base: Any, actor: Any, submit_id: Any = None) -> Any:
        if any(getattr(op, "op", "") == "edit" for op in ops):
            raise NotebookToolError("edit_ambiguous", "refused", op_index=0)
        return honest(self, ops, base, actor, submit_id)

    driver = _driver()
    ids = await _create(driver, "a = 1")
    monkeypatch.setattr(reference.ReferenceNotebook, "apply", refusing_edits)
    with pytest.raises(InvariantViolationError, match="refused a valid batch"):
        await driver.agent(
            "notebook.edit",
            {
                "path": PATH,
                "ops": [{"op": "edit", "cell_id": ids[1], "edits": [{"old": "1", "new": "2"}]}],
            },
        )


# -- saved regressions replay forever ---------------------------------------------------------


@pytest.mark.parametrize("saved", sorted(REGRESSIONS.glob("*.json")), ids=lambda p: p.stem)
def test_saved_regressions_replay_clean(saved: Path) -> None:
    payload = json.loads(saved.read_text(encoding="utf-8"))

    async def go() -> None:
        driver = SimDriver(machines.TARGET_FACTORY(), gatekeeper=StanceGatekeeper("default"))
        try:
            await replay(payload["steps"], driver)
        finally:
            await driver.close()

    asyncio.run(go())


# -- the format's round trip under the simulator's documents ------------------------------------

FORMAT_FINDINGS = [
    pytest.param("import pandas as pd\nimport alkera  # note", id="setup_inline_comment_survives"),
    pytest.param("", id="empty_setup_cell_survives"),
    pytest.param("import pandas as pd\n# note", id="setup_comment_line_survives"),
]


@pytest.mark.parametrize("setup", FORMAT_FINDINGS)
async def test_the_file_reads_back_to_the_document(setup: str) -> None:
    driver = _driver(check_file=True)
    await driver.agent(
        "notebook.create",
        {"path": PATH, "cells": [{"kind": "setup", "source": setup}, {"source": "x = 1"}]},
    )
    await driver.check(settle=True)


async def test_an_edit_names_the_cells_it_left_stale_and_answers_the_engine_s_shape() -> None:
    """After a run, editing the upstream cell leaves its dependent stale: the
    edit's result says so beside the engine's own result fields."""
    driver = _driver()
    ids = await _create(driver, "a = 1", "b = a + 1")
    await driver.agent("notebook.run", {"path": PATH, "target": {"kind": "all"}})
    out = await driver.agent(
        "notebook.edit",
        {"path": PATH, "ops": [{"op": "replace", "cell_id": ids[1], "source": "a = 5"}]},
    )
    assert isinstance(out, NotebookOpsResult), out
    assert out.stale == [ids[2]]  # type: ignore[attr-defined]
    (edited,) = out.cells
    assert (edited.id, edited.status) == (ids[1], "edited")
    await driver.check(settle=True)
