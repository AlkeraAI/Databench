"""What a client watching a notebook sees of a run, on the real kernel.

- Who ran a cell: the person or agent whose action caused the run (a widget
  change and an autorun descendant included), an agent named for the person
  it acts for, never an id.
- Every executed cell of every run kind reports ``running`` before its first
  output, even to a client that reads late, so a client clears the previous
  outputs instead of piling the new ones beside them.
- An interrupt ends with events a client can clear its state on.
- A client joining after a run reads the outputs everyone else sees, and so
  does one joining after the engine restarted.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from alkera_notebook.engine import (
    ActingFor,
    Actor,
    AllTarget,
    CellsTarget,
    NotebookClient,
    ReadQuery,
    RunActor,
    WidgetAction,
)
from alkera_notebook.engine.models import DisplayOutput, ErrorOutput, StreamOutput
from alkera_notebook.events.models import AnyEvent
from alkera_notebook.tools.engine_adapter import EnginePort
from alkera_notebook.tools.port import ActorRef
from nbeng_harness import ANN, BOB, engine_for, notebook, run_cells, statuses, text_of, until

AGENT_FOR_ANN = Actor(
    kind="agent",
    id="agent:chat-1",
    display_name="Agent",
    can_edit=True,
    can_run=True,
    acting_for=ActingFor(id=ANN.id, display_name="Ann"),
)
SLIDER = "import alkera.ui as ui\nslider = ui.slider(0, 10, value=5)\nslider"
READER = "t = slider.value * 2\nprint('t is', t)\nt"
TICKS = "import time\nfor i in range(120):\n    print('tick', i, flush=True)\n    time.sleep(0.2)"


def drain(client: NotebookClient) -> list[AnyEvent]:
    """Everything the client has not read yet, read at once (a slow reader)."""
    out = list(client.queue._items)
    client.queue._items.clear()
    return out


def fold_outputs(events: list[AnyEvent]) -> dict[str, list[Any]]:
    """The outputs a client shows, folded the way the event contract says:
    ``running`` clears a cell's outputs, outputs and streams append."""
    shown: dict[str, list[Any]] = {}
    for e in events:
        cid = getattr(e, "cell_id", None)
        if cid is None:
            continue
        if e.type == "cell.status" and e.status == "running":  # type: ignore[union-attr]
            shown[cid] = []
        elif e.type == "cell.output":
            shown.setdefault(cid, []).append(e.output)  # type: ignore[union-attr]
        elif e.type == "cell.stream":
            shown.setdefault(cid, []).append(e.text)  # type: ignore[union-attr]
    return shown


def assert_running_before_output(events: list[AnyEvent], cid: str, run_id: str) -> None:
    def of(e: AnyEvent) -> bool:
        return getattr(e, "cell_id", None) == cid and getattr(e, "run_id", None) == run_id

    running = [
        i
        for i, e in enumerate(events)
        if of(e) and e.type == "cell.status" and e.status == "running"  # type: ignore[union-attr]
    ]
    produced = [
        i for i, e in enumerate(events) if of(e) and e.type in ("cell.output", "cell.stream")
    ]
    assert running, f"no running status for {cid} in {run_id}"
    assert produced, f"no output for {cid} in {run_id}"
    assert running[0] < produced[0]


# ------------------------------------------------------------------ order of events


async def test_a_late_reader_sees_running_before_each_cells_output(tmp_path: Path) -> None:
    async with engine_for(tmp_path) as engine:
        session, ann, (a, b) = await notebook(engine, ["print('a')\n1", "print('b')\n2"])
        late = session.attach(BOB)
        first = await run_cells(ann, a, b)
        second = await run_cells(ann, a, b)
        events = drain(late)
        for record in (first, second):
            for cid in (a, b):
                assert_running_before_output(events, cid, record.run_id)
        # Two runs, folded: each cell shows one run's outputs, not two.
        shown = fold_outputs(events)
        assert [len(shown[a]), len(shown[b])] == [2, 2]


async def test_an_autorun_descendant_reports_running_before_its_output(tmp_path: Path) -> None:
    async with engine_for(tmp_path) as engine:
        session, ann, (a, b) = await notebook(engine, ["x = 1", "print('x is', x)\nx * 2"])
        await (await ann.run(AllTarget())).wait(30)
        late = session.attach(BOB)
        record = await run_cells(bob := session.attach(BOB), a)
        assert [p.cell_id for p in record.plan] == [a, b]
        events = drain(late)
        assert_running_before_output(events, b, record.run_id)
        assert len(fold_outputs(events)[b]) == 2
        # The descendant ran because Bob ran its ancestor: it is Bob's run.
        view = await bob.read()
        by_id = {c.id: c for c in view.cells}
        assert by_id[b].last_run is not None and by_id[b].last_run.by == RunActor.of(BOB)
        assert by_id[b].last_run.trigger == "run"


async def test_a_widget_rerun_reports_running_and_belongs_to_who_moved_it(
    tmp_path: Path,
) -> None:
    async with engine_for(tmp_path) as engine:
        session, ann, (_slider, r) = await notebook(engine, [SLIDER, READER])
        await (await ann.run(AllTarget())).wait(30)
        assert await text_of(ann, r) == "t is 10\n10"
        mid = (await ann.widget(WidgetAction(action="list"))).widgets[0].model_id
        bob = session.attach(BOB)
        late = session.attach(ANN)
        bob.attach_frame("bob-f1", mid)
        await bob.comm_send(
            mid, "m-1", {"data": {"method": "update", "state": {"value": 3}}}, [], frame_id="bob-f1"
        )

        async def reran() -> bool:
            return await text_of(ann, r) == "t is 6\n6"

        await until(reran, timeout_s=30)
        await until(lambda: session.runtime.current is None and not session.runtime.queue)
        events = drain(late)
        widget_runs = [e.run_id for e in events if e.type == "run.queued" and e.trigger == "widget"]  # type: ignore[union-attr]
        rerun = [
            rid
            for rid in widget_runs
            if any(p.cell_id == r for p in session.runtime.records[rid].plan)
        ]
        assert len(rerun) == 1
        assert_running_before_output(events, r, rerun[0])
        assert fold_outputs(events)[r] == ["t is 6\n", {"text/plain": "6"}]
        view = await ann.read()
        last = next(c for c in view.cells if c.id == r).last_run
        assert last is not None and last.trigger == "widget"
        assert last.by == RunActor.of(BOB) and last.by.label() == "Bob"


# ------------------------------------------------------------------ attribution


async def test_an_agents_run_is_named_for_the_person_it_acts_for(tmp_path: Path) -> None:
    since = datetime.now(UTC) - timedelta(seconds=5)
    async with engine_for(tmp_path) as engine:
        session, ann, (slow, a) = await notebook(engine, ["import time\ntime.sleep(1.0)", "1"])
        agent = session.attach(AGENT_FOR_ANN)
        blocking = await ann.run(CellsTarget(ids=[slow]))
        queued = await agent.run(CellsTarget(ids=[a]))
        # Waiting runs are named, never by an id.
        assert [q.by for q in (await ann.kernel("status")).queue] == [
            "Ann",
            "Agent for Ann",
        ]
        await blocking.wait(20)
        record = await queued.wait(20)
        assert record.requested_by == AGENT_FOR_ANN
        last = next(c for c in (await ann.read()).cells if c.id == a).last_run
        assert last is not None
        assert last.by == RunActor(
            kind="agent",
            id="agent:chat-1",
            display_name="Agent",
            acting_for=ActingFor(id=ANN.id, display_name="Ann"),
        )
        assert last.by.label() == "Agent for Ann"
        assert last.started_at is not None and last.finished_at is not None
        assert last.started_at <= last.finished_at
        # The activity feed (the digest's source) keeps the person too.
        runs = [e for e in (await ann.activity(since)).entries if e.kind == "cell_run"]
        assert [(e.actor.kind, e.actor.label()) for e in runs] == [
            ("person", "Ann"),
            ("agent", "Agent for Ann"),
        ]
        # The agent tools read the same name.
        port = EnginePort(
            session,
            agent,
            ActorRef(
                kind="agent",
                id="agent:chat-1",
                display_name="Agent",
                acting_for=ActingFor(id=ANN.id, display_name="Ann"),
            ),
        )
        cell = next(
            c
            for c in (await port.read(None, include_source=False, include_outputs=True)).cells
            if c.id == a
        )
        assert cell.last_run is not None and cell.last_run.by == "Agent for Ann"
        assert cell.output is not None and cell.output.author == "Agent for Ann"
        detail = await port.output(a, "all")
        assert detail.author == "Agent for Ann"
        assert detail.run is not None and detail.run.by == "Agent for Ann"


# ------------------------------------------------------------------ interrupt


async def test_an_interrupt_ends_with_events_a_client_clears_on(tmp_path: Path) -> None:
    async with engine_for(tmp_path, escalation=(3.0, 7.0)) as engine:
        session, ann, (a,) = await notebook(engine, [TICKS])
        late = session.attach(BOB)
        handle = await ann.run(CellsTarget(ids=[a]))

        async def ticking() -> bool:
            return "tick 1" in await text_of(ann, a)

        await until(ticking, timeout_s=15)
        await ann.kernel("interrupt")
        record = await handle.wait(10)
        assert record.status == "interrupted"
        await until(lambda: session.runtime.kernel_state == "idle")
        events = drain(late)
        marks = [
            (
                e.type,
                getattr(e, "step", None) or getattr(e, "state", None) or getattr(e, "status", None),
            )
            for e in events
            if e.type in ("kernel.interrupt", "kernel.state", "run.finished")
        ]
        assert marks == [
            ("kernel.state", "starting"),
            ("kernel.state", "idle"),
            ("kernel.state", "busy"),
            ("kernel.interrupt", "signalled"),
            ("kernel.interrupt", "done"),
            ("run.finished", "interrupted"),
            ("kernel.state", "idle"),
        ]
        assert (await ann.kernel("status")).state == "idle"
        # The cell's traceback reads as Python prints one, naming the
        # interrupt once, last; and who ran it is named.
        cell = next(c for c in (await ann.read()).cells if c.id == a)
        assert cell.status == "interrupted"
        assert cell.output is not None and cell.output.error is not None
        error = cell.output.error
        assert (error.ename, error.evalue) == ("KeyboardInterrupt", "")
        assert error.traceback[0] == "Traceback (most recent call last):\n"
        assert error.traceback[-1] == "KeyboardInterrupt\n"
        assert sum("KeyboardInterrupt" in line for line in error.traceback) == 1
        # Where it landed in the person's code is kept.
        assert any(f'"cell-{a}"' in line for line in error.traceback), error.traceback
        assert cell.last_run is not None and cell.last_run.by.label() == "Ann"


async def test_runs_flip_the_kernel_busy_then_idle_once_the_queue_drains(
    tmp_path: Path,
) -> None:
    async with engine_for(tmp_path) as engine:
        session, ann, (a, b) = await notebook(engine, ["import time\ntime.sleep(0.5)", "1"])
        late = session.attach(BOB)
        first = await ann.run(CellsTarget(ids=[a]))
        second = await ann.run(CellsTarget(ids=[b]))
        await first.wait(20)
        await second.wait(20)
        await until(lambda: session.runtime.kernel_state == "idle")
        states = [e.state for e in drain(late) if e.type == "kernel.state"]  # type: ignore[union-attr]
        # Busy through both queued runs, idle only once nothing is left.
        assert states == ["starting", "idle", "busy", "idle"]


# ------------------------------------------------------------------ joining


async def test_a_client_joining_after_a_run_reads_every_output(tmp_path: Path) -> None:
    async with engine_for(tmp_path) as engine:
        session, ann, (a, b, c) = await notebook(
            engine, ["print('hello')\ndisplay('shown')\n2", "1 / 0", "z = 3"]
        )
        await (await ann.run(AllTarget())).wait(30)
        joined = session.attach(BOB)
        view = await joined.read()
        by_id = {cell.id: cell for cell in view.cells}
        # In the order the cell made them: printed, displayed, its value.
        assert by_id[a].outputs == [
            StreamOutput(output_id=f"{a}/s0", name="stdout", text="hello\n"),
            DisplayOutput(output_id=f"{a}/0", data={"text/plain": "'shown'"}),
            DisplayOutput(output_id=f"{a}/1", data={"text/plain": "2"}),
        ]
        [error] = by_id[b].outputs
        assert isinstance(error, ErrorOutput) and error.error.ename == "ZeroDivisionError"
        assert by_id[c].outputs == []
        # A reader that wants only the summary (the agent tools) skips them.
        lean = await joined.read(ReadQuery(include_output_items=False))
        assert all(cell.outputs == [] for cell in lean.cells)
        assert next(x for x in lean.cells if x.id == a).output is not None


async def test_outputs_survive_an_engine_restart(tmp_path: Path) -> None:
    async with engine_for(tmp_path) as engine:
        _session, ann, (a,) = await notebook(engine, ["print('hello')\n2"])
        await run_cells(ann, a)
        before = next(c for c in (await ann.read()).cells if c.id == a)
    async with engine_for(tmp_path) as engine:
        session = await engine.open("nb.alknb.py")
        joined = session.attach(BOB)
        after = next(c for c in (await joined.read()).cells if c.id == a)
        assert after.output_origin == "saved"
        assert after.outputs == before.outputs and after.outputs
        assert after.last_run is not None and before.last_run is not None
        assert after.last_run.by == RunActor.of(ANN)
        assert after.last_run.run_id == before.last_run.run_id
        assert (await statuses(joined))[a] == "not_run"


async def test_the_only_files_a_run_leaves_beside_the_notebook_are_its_saved_outputs(
    tmp_path: Path,
) -> None:
    """Outputs at rest live beside the notebook in marimo's session shape,
    written by the engine; nothing else (no kernel or marimo session file)
    lands in the person's folder."""
    async with engine_for(tmp_path) as engine:
        _session, ann, (a,) = await notebook(
            engine, ["print('hi')\ndisplay({'k': 1})\n2"], path="reports/weekly.alknb.py"
        )
        await run_cells(ann, a)
    folder = tmp_path / "ws" / "reports"
    assert sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*")) == [
        "__marimo__",
        "__marimo__/session",
        "__marimo__/session/.gitignore",
        "__marimo__/session/weekly.alknb.py.json",
        "weekly.alknb.py",
    ]
