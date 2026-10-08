// The engine-state fold: each event's effect, replays and old kernels
// ignored, a new kernel turning outputs into saved ones, attribution carried
// from the run to the cell.

import { describe, expect, it } from "vitest";

import { graphErrorLabel, multipleDefinitions } from "@alkera/notebook-ui";

import type { GraphSummary, NotebookView } from "@/api/notebooks";
import {
  INITIAL_RUNTIME,
  MACHINE_SILENT_MESSAGE,
  INSTALL_RESULT_MS,
  KERNEL_RESTARTED_MESSAGE,
  NotebookRuntimeStore,
  envMayHaveChanged,
  followInstall,
  followInterrupt,
  followPresence,
  reduceRuntime,
  type NotebookEvent,
  type RuntimeState,
} from "@/pages/workspace/chat/workspace/notebook/notebookRuntime";
import { cellOfView, graphErrors } from "@/pages/workspace/chat/workspace/notebook/notebookWire";

const K1 = "k1";
const ALICE = { kind: "person", id: "u1", display_name: "Alice", acting_for: null };

function fold(events: NotebookEvent[], from: RuntimeState = INITIAL_RUNTIME): RuntimeState {
  return events.reduce(reduceRuntime, from);
}

let seq = 0;
const ev = (type: string, body: Record<string, unknown> = {}, kernel = K1): NotebookEvent => ({ type, kernel_id: kernel, seq: ++seq, ...body });

describe("the fold", () => {
  // A socket's numbers rise but skip (the server keeps a box's answers and
  // other people's frame traffic, and the tab takes widget traffic itself);
  // a loss is said by the server's resync, never read from a skip.
  it.each([
    ["a skip of one", 3],
    ["a skip of many", 40],
  ])("folds every event past %s in the kernel's numbers", (_, skipTo) => {
    const at = (n: number, type: string, body: Record<string, unknown> = {}): NotebookEvent => ({ type, kernel_id: K1, seq: n, ...body });
    const s = fold([
      at(1, "kernel.state", { state: "idle" }),
      at(skipTo, "cell.started", { cell_id: "c1", run_id: "r1" }),
      at(skipTo + 2, "cell.finished", { cell_id: "c1", run_id: "r1", status: "fresh" }),
      at(skipTo + 3, "cell.variables", { cell_id: "c1", run_id: "r1", variables: [{ name: "x", type: "int", repr: "1" }] }),
    ]);
    expect(s.cells.c1!.status).toBe("fresh");
    expect(s.cells.c1!.variables.map((v) => [v.name, v.cell_id])).toEqual([["x", "c1"]]);
    expect(s.seq).toBe(skipTo + 3);
  });

  it("still drops an event at or behind the number it stands at", () => {
    const at = (n: number, type: string, body: Record<string, unknown> = {}): NotebookEvent => ({ type, kernel_id: K1, seq: n, ...body });
    const s = fold([
      at(1, "kernel.state", { state: "idle" }),
      at(5, "cell.variables", { cell_id: "c1", variables: [{ name: "x", type: "int", repr: "1" }] }),
      at(5, "cell.variables", { cell_id: "c1", variables: [] }),
      at(4, "cell.variables", { cell_id: "c1", variables: [] }),
    ]);
    expect(s.cells.c1!.variables.map((v) => v.name)).toEqual(["x"]);
  });

  it("starts a kernel and tracks its state, memory and interrupt escalation", () => {
    const s = fold([
      ev("kernel.state", { state: "busy", memory_bytes: 10, memory_limit_bytes: 100 }),
      ev("kernel.state", { interrupt: "signalled" }),
      ev("kernel.state", { interrupt: "resignalled" }),
    ]);
    expect(s.kernel).toMatchObject({ state: "busy", memory_bytes: 10, memory_limit_bytes: 100, interrupt: "resignalled" });
    expect(s.kernelId).toBe(K1);
    expect(fold([ev("kernel.state", { state: "idle" })], s).kernel.interrupt).toBe("none");
  });

  it("queues, starts and finishes runs, keeping who asked", () => {
    const s1 = fold([ev("kernel.state", { state: "idle" }), ev("run.queued", { run_id: "r1", requested_by: ALICE, trigger: "run", targets: ["c1"] })]);
    expect(s1.kernel.queue).toEqual([{ run_id: "r1", by: ALICE, trigger: "run", status: "queued", targets: ["c1"] }]);
    const s2 = fold([ev("run.started", { run_id: "r1", started_at: "2026-10-05T10:00:00Z" })], s1);
    expect(s2.kernel.queue[0]!.status).toBe("running");
    expect(s2.kernel.state).toBe("busy");
    const s3 = fold(
      [
        ev("cell.status", { cell_id: "c1", status: "running" }),
        ev("cell.stream", { cell_id: "c1", name: "stdout", text: "hi\n" }),
        ev("cell.output", { cell_id: "c1", output: { type: "display", output_id: "o1", data: { "text/plain": "1" } } }),
        ev("cell.finished", { cell_id: "c1", run_id: "r1", status: "fresh", duration_ms: 1200, finished_at: "2026-10-05T10:00:01Z", defs: ["x"] }),
        ev("run.finished", { run_id: "r1", status: "finished" }),
      ],
      s2,
    );
    const c1 = s3.cells.c1!;
    expect(c1.status).toBe("fresh");
    expect(c1.outputs.map((o) => o.type)).toEqual(["stream", "display"]);
    expect(c1.last_run).toEqual({ run_id: "r1", by: ALICE, trigger: "run", started_at: "2026-10-05T10:00:00Z", finished_at: "2026-10-05T10:00:01Z" });
    expect(c1.duration_ms).toBe(1200);
    expect(c1.defs).toEqual(["x"]);
    expect(s3.kernel.queue).toEqual([]);
    expect(s3.kernel.state).toBe("idle");
  });

  it("clears a cell's outputs when it starts running again", () => {
    const s = fold([
      ev("kernel.state", { state: "idle" }),
      ev("cell.output", { cell_id: "c1", output: { type: "stream", name: "stdout", text: "old" } }),
      ev("cell.status", { cell_id: "c1", status: "running" }),
    ]);
    expect(s.cells.c1!.outputs).toEqual([]);
  });

  it("replaces an output in place when told to", () => {
    const s = fold([
      ev("kernel.state", { state: "idle" }),
      ev("cell.output", { cell_id: "c1", output: { type: "display", output_id: "a", data: { "text/plain": "1" } } }),
      ev("cell.output", { cell_id: "c1", mode: "replace", output: { type: "display", output_id: "b", data: { "text/plain": "2" } } }),
    ]);
    expect(s.cells.c1!.outputs).toEqual([{ output_id: "b", type: "display", data: { "text/plain": "2" } }]);
  });

  it("drops a replayed event and one of an earlier kernel", () => {
    const start = fold([ev("kernel.state", { state: "idle" })]);
    const out = ev("cell.stream", { cell_id: "c1", name: "stdout", text: "once" });
    const once = fold([out], start);
    expect(fold([out], once)).toBe(once);
    const k2 = fold([ev("kernel.state", { state: "starting" }, "k2")], once);
    const stale = fold([ev("cell.stream", { cell_id: "c1", name: "stdout", text: "late" }, K1)], k2);
    expect(stale.cells.c1!.outputs.map((o) => (o.type === "stream" ? o.text : ""))).toEqual(["once"]);
  });

  it("marks outputs saved and cells not run when a new kernel starts", () => {
    const s = fold([
      ev("kernel.state", { state: "idle" }),
      ev("cell.output", { cell_id: "c1", output: { type: "stream", name: "stdout", text: "x" } }),
      ev("cell.status", { cell_id: "c1", status: "fresh" }),
      ev("kernel.state", { state: "starting" }, "k2"),
    ]);
    expect(s.cells.c1).toMatchObject({ status: "not_run", saved: true });
    expect(s.kernelId).toBe("k2");
  });

  it("asks for confirmation and forgets it when the run starts", () => {
    const s = fold([
      ev("kernel.state", { state: "idle" }),
      ev("run.needs_confirmation", { run_id: "r2", estimate_s: 600, plan: [{ cell_id: "c9", name: "train", reason: "descendant", last_duration_ms: 600000 }] }),
    ]);
    expect(s.confirmation).toEqual({ run_id: "r2", estimate_s: 600, plan: [{ cell_id: "c9", name: "train", reason: "descendant", last_duration_ms: 600000 }] });
    expect(fold([ev("run.started", { run_id: "r2" })], s).confirmation).toBeNull();
  });

  it.each([
    ["out_of_memory", "ran out of memory", "danger"],
    ["interrupt_restart", "kernel was restarted", "danger"],
    ["shutdown", "stopped", "info"],
    ["crashed", "stopped", "info"],
  ])("says why the kernel exited (%s)", (reason, text, tone) => {
    const s = fold([ev("kernel.state", { state: "busy" }), ev("kernel.exited", { reason })]);
    expect(s.kernel.state).toBe("stopped");
    expect(s.notice?.message).toContain(text);
    expect(s.notice?.tone).toBe(tone);
  });

  it("says a restart someone asked for restarted the kernel, not that it stopped", () => {
    const s = fold([ev("kernel.state", { state: "idle" }), ev("kernel.state", { state: "restarting" }), ev("kernel.exited", { reason: "restart" })]);
    expect(s.notice).toMatchObject({ message: KERNEL_RESTARTED_MESSAGE, tone: "info" });
    expect(s.notice?.message).toBe("Kernel restarted.");
    expect(s.kernel.state).toBe("restarting");
  });

  it("starts the engine's own notice with a capital", () => {
    const s = fold([ev("notice", { notice: { kind: "info", message: "autoreload is off for this notebook" } })]);
    expect(s.notice?.message).toBe("Autoreload is off for this notebook");
  });

  it("takes a newer environment build as the kernel's state, not a banner, until a new kernel starts", () => {
    const up = ev("kernel.state", { state: "idle" });
    const told = fold([up, ev("notice", { notice: { kind: "env_newer", message: "A newer environment is ready. Restart the kernel to use it." } })]);
    expect(told.kernel.env_outdated).toBe(true);
    expect(told.notice).toBeNull();
    const restarted = fold([ev("kernel.state", { state: "idle" }, "k2")], told);
    expect(restarted.kernel.env_outdated).toBe(false);
  });

  it("takes a newer environment build from a snapshot's kernel", () => {
    const s = fold([{ type: "snapshot", seq: 3, view: { kernel: { state: "idle", reactivity: "autorun", kernel_id: K1, env_outdated: true }, cells: [] } }]);
    expect(s.kernel.env_outdated).toBe(true);
  });

  it("takes the graph's edges, errors and names", () => {
    const s = fold([
      ev("kernel.state", { state: "idle" }),
      ev("graph", { edges: [["c1", "c2"]], errors: { c2: ["multiple_definitions"] }, cells: { c1: { defs: ["x"], refs: [] }, c2: { defs: ["x"], refs: ["x"] } } }),
    ]);
    expect(s.graph).toEqual({ edges: [["c1", "c2"]], errors: { c2: ["multiple_definitions"] } });
    expect(s.cells.c2).toMatchObject({ defs: ["x"], refs: ["x"], graph_errors: ["multiple_definitions"] });
  });

  it("keeps variables per cell", () => {
    const s = fold([ev("kernel.state", { state: "idle" }), ev("cell.variables", { cell_id: "c1", variables: [{ name: "df", type: "DataFrame", repr: "..." }, { bad: 1 }] })]);
    expect(s.cells.c1!.variables).toEqual([{ name: "df", type: "DataFrame", repr: "...", cell_id: "c1" }]);
  });

  it("restores everything from a join snapshot", () => {
    const s = fold([
      {
        type: "snapshot",
        kernel_id: "k7",
        seq: 40,
        kernel: { state: "idle", reactivity: "lazy" },
        queue: [{ run_id: "r1", by: ALICE, trigger: "run_all", status: "running" }],
        cells: { c1: { status: "stale", outputs: [{ type: "stream", name: "stderr", text: "warn" }], defs: ["a"] } },
      },
    ]);
    expect(s.kernelId).toBe("k7");
    expect(s.seq).toBe(40);
    expect(s.kernel).toMatchObject({ state: "idle", reactivity: "lazy" });
    expect(s.kernel.queue[0]!.trigger).toBe("run_all");
    expect(s.cells.c1).toMatchObject({ status: "stale", defs: ["a"] });
    expect(s.cells.c1!.outputs[0]).toMatchObject({ type: "stream", name: "stderr", text: "warn" });
  });

  it.each([
    ["an unknown type", { type: "mystery" }],
    ["a status it does not know", { type: "cell.status", cell_id: "c1", status: "exploded" }],
    ["an output with no data", { type: "cell.output", cell_id: "c1", output: { type: "display", data: {} } }],
  ])("changes nothing for %s", (_name, body) => {
    const start = fold([ev("kernel.state", { state: "idle" })]);
    const next = reduceRuntime(start, { ...body, kernel_id: K1, seq: ++seq } as NotebookEvent);
    expect(next.cells).toEqual(start.cells);
  });
});

describe("the store", () => {
  it("tells listeners only when something changed", () => {
    const store = new NotebookRuntimeStore();
    let calls = 0;
    store.subscribe(() => (calls += 1));
    const e = ev("kernel.state", { state: "idle" });
    store.dispatch(e);
    store.dispatch(e);
    expect(calls).toBe(1);
    expect(store.snapshot.kernel.state).toBe("idle");
  });
});

describe("the engine's own event shapes", () => {
  it("reads the memory guard's threshold", () => {
    const s = fold([ev("kernel.state", { state: "busy", memory_bytes: 800, threshold_bytes: 1000 })]);
    expect(s.kernel).toMatchObject({ memory_bytes: 800, memory_limit_bytes: 1000 });
  });

  it("takes a bare MIME bundle as a display output", () => {
    const s = fold([ev("kernel.state", { state: "idle" }), ev("cell.output", { cell_id: "c1", run_id: "r1", mode: "append", output: { "text/plain": "3", "text/html": "<b>3</b>" } })]);
    expect(s.cells.c1!.outputs).toEqual([{ output_id: "c1/0", type: "display", data: { "text/plain": "3", "text/html": "<b>3</b>" } }]);
  });

  it("shows a finished cell's error and when it finished", () => {
    const store = new NotebookRuntimeStore(() => new Date("2026-10-05T12:00:00Z"));
    store.dispatch(ev("kernel.state", { state: "idle" }));
    store.dispatch(ev("run.queued", { run_id: "r1", requested_by: ALICE, trigger: "run", position: 0 }));
    store.dispatch(ev("run.started", { run_id: "r1", plan: [{ cell_id: "c1", name: "_", reason: "target" }] }));
    expect(store.snapshot.kernel.queue[0]).toMatchObject({ status: "running", targets: ["c1"] });
    store.dispatch(ev("cell.finished", { cell_id: "c1", run_id: "r1", status: "error", duration_ms: 5, error: { ename: "ValueError", evalue: "bad", traceback: ["line"] } }));
    const c1 = store.snapshot.cells.c1!;
    expect(c1.status).toBe("error");
    expect(c1.outputs).toEqual([{ output_id: "c1/error", type: "error", error: { ename: "ValueError", evalue: "bad", traceback: ["line"] } }]);
    expect(c1.last_run).toMatchObject({ by: ALICE, finished_at: "2026-10-05T12:00:00.000Z" });
  });

  it("reads the graph and a notice nested as the engine sends them", () => {
    const s = fold([
      ev("kernel.state", { state: "idle" }),
      ev("graph", { graph: { cells: { a: { defs: ["x"], refs: [], errors: ["multiple_definitions"] }, b: { defs: ["x"], refs: [] } }, edges: [] } }),
      ev("notice", { notice: { kind: "upstream_being_edited", cell_id: "a", message: "Cell load is being edited by Bob" } }),
    ]);
    expect(s.cells.a).toMatchObject({ defs: ["x"], graph_errors: ["multiple_definitions"] });
    // A notice about one cell is said on that cell, not as a banner.
    expect(s.cells.a?.notice).toEqual({ kind: "upstream_being_edited", message: "Cell load is being edited by Bob" });
    expect(s.notice).toBeNull();
  });

  it("keeps a notice about one cell on that cell until the cell runs", () => {
    const said = fold([
      ev("kernel.state", { state: "idle" }),
      ev("notice", { notice: { kind: "module_missing", cell_id: "a", message: "no module named polars" } }),
    ]);
    expect(said.cells.a?.notice).toEqual({ kind: "module_missing", message: "No module named polars" });
    expect(said.notice).toBeNull();
    expect(fold([ev("cell.status", { cell_id: "a", status: "queued" })], said).cells.a?.notice).not.toBeNull();
    expect(fold([ev("cell.status", { cell_id: "a", status: "running" })], said).cells.a?.notice).toBeNull();
  });

  it("says whose editing a skipped cell waits for from the cell's status, and stops when the status does", () => {
    const held = fold([
      ev("kernel.state", { state: "idle" }),
      ev("notice", { notice: { kind: "cell_skipped_editing", cell_id: "b", by: "Ada", message: "Not re-run while Ada is editing it" } }),
      ev("cell.status", { cell_id: "b", status: "stale", rerun_waits_for: "Ada" }),
    ]);
    expect(held.cells.b?.rerun_waits_for).toBe("Ada");
    // Nothing to dismiss: no banner, and no second line on the cell.
    expect(held.notice).toBeNull();
    expect(held.cells.b?.notice).toBeNull();
    // Ada left: the engine's next status for the cell names nobody.
    const left = fold([ev("cell.status", { cell_id: "b", status: "stale", rerun_waits_for: null })], held);
    expect(left.cells.b?.rerun_waits_for).toBeNull();
    // A status from an engine that never names it reads as nobody too.
    expect(fold([ev("cell.status", { cell_id: "b", status: "fresh" })], held).cells.b?.rerun_waits_for).toBeNull();
  });

  it("reads whose editing a cell waits for from a view's cell", () => {
    expect(cellOfView(undefined, { id: "b", status: "stale", rerun_waits_for: "Ada" }).rerun_waits_for).toBe("Ada");
    expect(cellOfView(undefined, { id: "b", status: "stale" }).rerun_waits_for).toBeNull();
  });

  it("says why a run was refused", () => {
    const s = fold([ev("kernel.state", { state: "idle" }), ev("run.finished", { run_id: "r5", status: "refused", reason: "upstream_being_edited" })]);
    expect(s.runProblem?.message).toContain("being edited");
  });

  it.each(["ok", "coalesced", "interrupted", "kernel_restarted", "error"])("takes a run that ended %s off the queue without a notice", (status) => {
    const s = fold([ev("kernel.state", { state: "idle" }), ev("run.queued", { run_id: "r6", requested_by: ALICE, trigger: "run" }), ev("run.finished", { run_id: "r6", status })]);
    expect(s.kernel.queue).toEqual([]);
    expect(s.notice).toBeNull();
  });

  it("rebuilds statuses and the queue from a resync's view", () => {
    const s = fold([
      ev("kernel.state", { state: "idle" }),
      ev("cell.output", { cell_id: "c1", output: { "text/plain": "kept" } }),
      ev("run.queued", { run_id: "r9", requested_by: { kind: "person", id: "user:b0b", display_name: "Bob" }, trigger: "run_all" }),
      ev("resync", {
        reason: "overflow",
        view: {
          kernel: {
            state: "busy",
            reactivity: "lazy",
            kernel_id: K1,
            queue: [
              { run_id: "r9", by: "user:b0b", trigger: "run_all", status: "running" },
              { run_id: "r10", by: "user:c4c", trigger: "run", status: "queued" },
            ],
          },
          cells: [{ id: "c1", status: "stale", defs: ["x"], refs: [], graph_errors: [], output_outdated: true, output_origin: "saved" }],
        },
      }),
    ]);
    expect(s.kernel).toMatchObject({ state: "busy", reactivity: "lazy" });
    // The view names who asked by id: the name comes from the run's own
    // announcement, and an id is never shown as one.
    expect(s.kernel.queue[0]).toMatchObject({ run_id: "r9", by: { display_name: "Bob" } });
    expect(s.kernel.queue[1]).toMatchObject({ run_id: "r10", by: { id: "user:c4c", display_name: "" } });
    expect(s.cells.c1).toMatchObject({ status: "stale", outdated: true, saved: true });
    expect(s.cells.c1!.outputs).toHaveLength(1);
  });
});

describe("following an interrupt", () => {
  function clock() {
    const pending: { at: number; fn: () => void }[] = [];
    let now = 0;
    return {
      timers: {
        setTimeout: (fn: () => void, ms: number) => {
          const t = { at: now + ms, fn };
          pending.push(t);
          return t;
        },
        clearTimeout: (h: unknown) => {
          const i = pending.indexOf(h as { at: number; fn: () => void });
          if (i >= 0) pending.splice(i, 1);
        },
      },
      advance(ms: number) {
        now += ms;
        for (const t of pending.filter((p) => p.at <= now)) {
          pending.splice(pending.indexOf(t), 1);
          t.fn();
        }
      },
    };
  }

  it("shows the second signal at 3 s and the restart at 7 s while the run goes on", () => {
    const store = new NotebookRuntimeStore();
    store.dispatch(ev("kernel.state", { state: "busy" }));
    const c = clock();
    followInterrupt(store, c.timers);
    expect(store.snapshot.kernel.interrupt).toBe("signalled");
    c.advance(2999);
    expect(store.snapshot.kernel.interrupt).toBe("signalled");
    c.advance(1);
    expect(store.snapshot.kernel.interrupt).toBe("resignalled");
    c.advance(4000);
    expect(store.snapshot.kernel.interrupt).toBe("restarting");
  });

  it("stops escalating once the kernel is idle again", () => {
    const store = new NotebookRuntimeStore();
    store.dispatch(ev("kernel.state", { state: "busy" }));
    const c = clock();
    followInterrupt(store, c.timers);
    store.dispatch(ev("kernel.state", { state: "idle" }));
    c.advance(8000);
    expect(store.snapshot.kernel.interrupt).toBe("none");
  });
});

describe("graph errors and reloads", () => {
  it.each([
    ["an object naming the name", [{ code: "multiple_definitions", name: "df", cells: ["a", "b"] }], ["multiple_definitions:df"]],
    ["an object with only a code", [{ code: "cycle" }], ["cycle"]],
    ["the engine's object with a null name", [{ code: "cycle", name: null, cells: ["a", "b"] }], ["cycle"]],
    ["a bare code from an older engine", ["cycle"], ["cycle"]],
    ["something else", [{ name: "x" }, 7], []],
  ])("reads %s", (_name, wire, expected) => {
    expect(graphErrors(wire)).toEqual(expected);
  });

  it("names the modules a run reloaded", () => {
    const s = fold([ev("kernel.state", { state: "idle" }), ev("run.started", { run_id: "r1", plan: [], reloaded: ["helpers", "etl.load"] })]);
    expect(s.notice?.message).toBe("Reloaded helpers, etl.load");
  });

  it("says nothing when a run reloaded nothing", () => {
    const s = fold([ev("kernel.state", { state: "idle" }), ev("run.started", { run_id: "r2", plan: [] })]);
    expect(s.notice).toBeNull();
  });
});

// The notebook's view as the routes and the channel's snapshot carry it.
function view(over: Partial<NotebookView> = {}): NotebookView {
  return {
    path: "analysis.alknb.py",
    token: "t1",
    settings: { format: "1.0", reactivity: "autorun", dataframe: "auto", outputs_in_git: false, autoreload: "off" },
    kernel: { state: "absent", env: null, reactivity: "autorun", kernel_id: null, seq: null, env_outdated: false },
    cells: [],
    presence: [],
    ...over,
  };
}

const cellState = (id: string, over: Record<string, unknown> = {}) => ({ id, name: "_", kind: "python", index: 0, status: "not_run", output_outdated: false, ...over });

describe("the view on the channel", () => {
  it("reads a join snapshot with no kernel: absent, every cell from the view", () => {
    const s = fold([
      {
        type: "snapshot",
        kernel_id: null,
        seq: 0,
        frames: {},
        view: view({
          cells: [
            cellState("c1", { defs: ["x"], graph_errors: [] }) as NotebookView["cells"][number],
            cellState("c2", { refs: ["x"], output_origin: "saved", output_outdated: true }) as NotebookView["cells"][number],
          ],
        }),
      },
    ]);
    expect(s.kernel.state).toBe("absent");
    expect(s.kernelId).toBeNull();
    expect(s.cells.c1).toMatchObject({ status: "not_run", defs: ["x"] });
    expect(s.cells.c2).toMatchObject({ status: "not_run", refs: ["x"], saved: true, outdated: true });
  });

  it("takes statuses and the kernel from a snapshot's view, and outputs from the kernel's own snapshot", () => {
    const s = fold([
      {
        type: "snapshot",
        kernel_id: "k3",
        seq: 12,
        view: view({
          kernel: { state: "idle", env: { env_id: "e1", kind: "uv", spec_root: "/w", python: "3.13", state: "ready", recorded_in_file: true, recorded: "default", last_failure: "" }, reactivity: "lazy", kernel_id: "k3", seq: 12, memory_bytes: 2048, env_outdated: false },
          cells: [cellState("c1", { status: "fresh", defs: ["x"] }) as NotebookView["cells"][number]],
        }),
        cells: { c1: { outputs: [{ type: "stream", name: "stdout", text: "1" }], duration_ms: 4 } },
      },
    ]);
    expect(s.kernelId).toBe("k3");
    expect(s.kernel).toMatchObject({ state: "idle", reactivity: "lazy", memory_bytes: 2048, env: { env_id: "e1", python: "3.13" } });
    expect(s.cells.c1).toMatchObject({ status: "fresh", defs: ["x"], duration_ms: 4 });
    expect(s.cells.c1!.outputs).toHaveLength(1);
  });

  it("rebuilds a cell's graph errors from a resync's view, objects and bare codes alike", () => {
    const s = fold([
      ev("kernel.state", { state: "idle" }),
      ev("resync", {
        view: view({
          kernel: { state: "idle", env: null, reactivity: "autorun", kernel_id: K1, seq: 99, env_outdated: false },
          cells: [
            cellState("a", { graph_errors: [{ code: "multiple_definitions", name: "x", cells: ["a", "b"] }] }) as NotebookView["cells"][number],
            cellState("b", { graph_errors: ["cycle"] }) as NotebookView["cells"][number],
          ],
        }),
      }),
    ]);
    expect(s.cells.a!.graph_errors).toEqual(["multiple_definitions:x"]);
    expect(s.cells.b!.graph_errors).toEqual(["cycle"]);
  });

  it("ignores a resync with no view (the tab subscribes again)", () => {
    const start = fold([ev("kernel.state", { state: "busy" })]);
    expect(reduceRuntime(start, { type: "resync" })).toBe(start);
  });
});

describe("graph errors as objects", () => {
  const summary: GraphSummary = {
    computed: true,
    cells: {
      a: { defs: ["x"], refs: [], errors: [{ code: "multiple_definitions", name: "x", cells: ["a", "b"] }] },
      b: { defs: ["x"], refs: [], errors: [{ code: "multiple_definitions", name: "x", cells: ["a", "b"] }] },
      c: { defs: ["y"], refs: ["y"], errors: [{ code: "cycle", cells: ["c"] }] },
    },
    edges: [["a", "c"]],
  };

  it("reads the graph event's GraphSummary into the graph and the cells", () => {
    const s = fold([ev("kernel.state", { state: "idle" }), ev("graph", { graph: summary })]);
    expect(s.graph).toEqual({ edges: [["a", "c"]], errors: { a: ["multiple_definitions:x"], b: ["multiple_definitions:x"], c: ["cycle"] } });
    expect(s.cells.c).toMatchObject({ defs: ["y"], refs: ["y"], graph_errors: ["cycle"] });
  });

  it("shows an error object as text naming the name, never as an object", () => {
    const s = fold([ev("kernel.state", { state: "idle" }), ev("graph", { graph: summary })]);
    expect(s.graph.errors.a!.map(graphErrorLabel)).toEqual(["Defines a name another cell also defines: x"]);
    expect(s.graph.errors.c!.map(graphErrorLabel)).toEqual(["Part of a cycle"]);
    // The quick fixes find the name the object named.
    expect(multipleDefinitions("a", s.cells, s.graph)?.names).toEqual(["x"]);
  });

  it("still takes a bare code from an older server", () => {
    const s = fold([ev("kernel.state", { state: "idle" }), ev("graph", { graph: { computed: true, cells: { a: { defs: [], refs: [], errors: ["cycle"] } }, edges: [] } })]);
    expect(s.cells.a!.graph_errors).toEqual(["cycle"]);
  });
});

describe("a re-run replaces a cell's outputs", () => {
  const text = (s: RuntimeState, id = "c1") =>
    s.cells[id]!.outputs.map((o) => (o.type === "stream" ? o.text : o.type === "display" ? String(o.data["text/plain"]) : o.error.ename));
  const ran = (runId: string) => [
    ev("cell.stream", { cell_id: "c1", run_id: runId, name: "stdout", text: `out ${runId}` }),
    ev("cell.output", { cell_id: "c1", run_id: runId, mode: "append", output: { "text/plain": `value ${runId}` } }),
    ev("cell.finished", { cell_id: "c1", run_id: runId, status: "fresh" }),
  ];

  it("even when no running status arrives between the runs", () => {
    const s = fold([ev("kernel.state", { state: "idle" }), ...ran("r1"), ...ran("r2")]);
    expect(text(s)).toEqual(["out r2", "value r2"]);
  });

  it("when the first sign of the new run is its output", () => {
    const s = fold([ev("kernel.state", { state: "idle" }), ...ran("r1"), ev("cell.output", { cell_id: "c1", run_id: "r2", output: { "text/plain": "new" } })]);
    expect(text(s)).toEqual(["new"]);
  });

  it("when the new run only finishes with an error", () => {
    const s = fold([
      ev("kernel.state", { state: "idle" }),
      ...ran("r1"),
      ev("cell.finished", { cell_id: "c1", run_id: "r2", status: "error", error: { ename: "KeyError", evalue: "x", traceback: [] } }),
    ]);
    expect(text(s)).toEqual(["KeyError"]);
  });

  it("when a cell.started event opens it", () => {
    const s = fold([ev("kernel.state", { state: "idle" }), ...ran("r1"), ev("cell.started", { cell_id: "c1", run_id: "r2" })]);
    expect(s.cells.c1).toMatchObject({ status: "running", outputs: [] });
  });

  it("keeps every output of one run, however they arrive", () => {
    const s = fold([
      ev("kernel.state", { state: "idle" }),
      ev("cell.status", { cell_id: "c1", run_id: "r1", status: "running" }),
      ...ran("r1"),
    ]);
    expect(text(s)).toEqual(["out r1", "value r1"]);
  });

  it("but leaves a cell the run skipped as it was", () => {
    const s = fold([ev("kernel.state", { state: "idle" }), ...ran("r1"), ev("cell.finished", { cell_id: "c1", run_id: "r2", status: "skipped" })]);
    expect(text(s)).toEqual(["out r1", "value r1"]);
  });

  it("and replaces saved outputs with the first output of a run", () => {
    const s = fold([
      ev("kernel.state", { state: "idle" }),
      ...ran("r1"),
      ev("kernel.state", { state: "starting" }, "k2"),
      ev("cell.output", { cell_id: "c1", run_id: "r3", output: { "text/plain": "fresh" } }, "k2"),
    ]);
    expect(text(s)).toEqual(["fresh"]);
    expect(s.cells.c1!.saved).toBe(false);
  });
});

describe("outputs survive a join, a resync and a worker restart", () => {
  const viewCell = { id: "c1", status: "fresh", defs: [], refs: [], graph_errors: [], output_origin: "kernel" };
  const shown = (s: RuntimeState) => s.cells.c1!.outputs.map((o) => (o.type === "display" ? o.data["text/plain"] : null));

  it("a snapshot without outputs keeps the ones shown", () => {
    const s = fold([
      ev("kernel.state", { state: "idle" }),
      ev("cell.output", { cell_id: "c1", run_id: "r1", output: { "text/plain": "42" } }),
      { type: "snapshot", seq: 3, view: { kernel: { state: "idle", reactivity: "autorun", kernel_id: K1 }, cells: [viewCell] } },
    ]);
    expect(shown(s)).toEqual(["42"]);
  });

  it("a snapshot of a new kernel (a restarted worker) keeps them too", () => {
    const s = fold([
      ev("kernel.state", { state: "idle" }),
      ev("cell.output", { cell_id: "c1", run_id: "r1", output: { "text/plain": "42" } }),
      { type: "snapshot", seq: 1, view: { kernel: { state: "idle", reactivity: "autorun", kernel_id: "k-new" }, cells: [{ ...viewCell, output_origin: "saved" }] } },
    ]);
    expect(shown(s)).toEqual(["42"]);
    expect(s.cells.c1!.saved).toBe(true);
    expect(s.kernelId).toBe("k-new");
  });

  it.each([
    ["on the view's cells", (outputs: unknown[]) => ({ view: { kernel: { state: "idle", reactivity: "autorun" }, cells: [{ ...viewCell, outputs }] } })],
    ["beside the view, per cell", (outputs: unknown[]) => ({ view: { kernel: { state: "idle", reactivity: "autorun" }, cells: [viewCell] }, cells: { c1: { outputs } } })],
  ])("a snapshot's outputs %s are applied for a member who opens it", (_where, shape) => {
    const s = fold([{ type: "snapshot", seq: 5, kernel_id: K1, ...shape([{ "text/plain": "from the run" }]) }]);
    expect(shown(s)).toEqual(["from the run"]);
  });

  it("a snapshot's outputs replace the ones shown", () => {
    const s = fold([
      ev("kernel.state", { state: "idle" }),
      ev("cell.output", { cell_id: "c1", run_id: "r1", output: { "text/plain": "old" } }),
      { type: "snapshot", seq: 9, kernel_id: K1, view: { kernel: { state: "idle", reactivity: "autorun" }, cells: [viewCell] }, cells: { c1: { outputs: [{ "text/plain": "new" }] } } },
    ]);
    expect(shown(s)).toEqual(["new"]);
  });

  it("a cell the snapshot no longer has is dropped", () => {
    const s = fold([
      ev("kernel.state", { state: "idle" }),
      ev("cell.output", { cell_id: "gone", run_id: "r1", output: { "text/plain": "x" } }),
      { type: "snapshot", seq: 3, view: { kernel: { state: "idle", reactivity: "autorun", kernel_id: K1 }, cells: [viewCell] } },
    ]);
    expect(s.cells.gone).toBeUndefined();
  });

  it("a resync's view keeps the outputs", () => {
    const s = fold([
      ev("kernel.state", { state: "idle" }),
      ev("cell.output", { cell_id: "c1", run_id: "r1", output: { "text/plain": "42" } }),
      ev("resync", { view: { kernel: { state: "idle", reactivity: "autorun", kernel_id: K1 }, cells: [viewCell] } }),
    ]);
    expect(shown(s)).toEqual(["42"]);
  });

  it("a resync's view that lists outputs applies them", () => {
    const s = fold([
      ev("kernel.state", { state: "idle" }),
      ev("cell.output", { cell_id: "c1", run_id: "r1", output: { "text/plain": "old" } }),
      ev("resync", {
        view: {
          kernel: { state: "idle", reactivity: "autorun", kernel_id: K1 },
          cells: [{ ...viewCell, outputs: [{ output_id: "o1", type: "display", data: { "text/plain": "new" } }] }],
        },
      }),
    ]);
    expect(shown(s)).toEqual(["new"]);
  });

  it("the route's view fills in a cell the channel showed nothing for, and nothing else", () => {
    const listed = (text: string) => [{ output_id: `o-${text}`, type: "display", data: { "text/plain": text } }];
    const s = fold([
      ev("kernel.state", { state: "idle" }),
      ev("cell.output", { cell_id: "c1", run_id: "r1", output: { "text/plain": "live" } }),
      {
        type: "view",
        view: {
          kernel: { state: "idle", reactivity: "autorun" },
          cells: [
            { ...viewCell, outputs: listed("stale") },
            { ...viewCell, id: "c2", output_origin: "saved", outputs: listed("saved") },
          ],
        },
      },
    ]);
    expect(shown(s)).toEqual(["live"]);
    expect(s.cells.c2!.outputs.map((o) => (o.type === "display" ? o.data["text/plain"] : null))).toEqual(["saved"]);
    expect(s.cells.c2!.saved).toBe(true);
  });

  it("the view's last run names who ran each cell", () => {
    const s = fold([
      {
        type: "snapshot",
        seq: 1,
        view: {
          kernel: { state: "idle", reactivity: "autorun" },
          cells: [{ ...viewCell, last_run: { run_id: "r1", by: { kind: "agent", id: "agent:a1", display_name: "Analyst", acting_for: { id: "u1", display_name: "Ada" } }, trigger: "run", finished_at: "2026-10-05T10:00:00Z" } }],
        },
      },
    ]);
    expect(s.cells.c1!.last_run).toEqual({
      run_id: "r1",
      by: { kind: "agent", id: "agent:a1", display_name: "Analyst", acting_for: { id: "u1", display_name: "Ada" } },
      trigger: "run",
      started_at: null,
      finished_at: "2026-10-05T10:00:00Z",
    });
  });
});

describe("a run that did not start", () => {
  const failedStart = [
    ev("kernel.state", { state: "starting" }),
    ev("kernel.exited", { reason: "start_failed", message: "kernel exited with code 2 before connecting" }),
    ev("notice", { notice: { kind: "kernel_unavailable", data: { reason: "start_failed" }, message: "kernel exited with code 2 before connecting" } }),
    ev("run.finished", { run_id: "r1", status: "refused", reason: "start_failed" }),
  ];

  it("says the reason the server gave, once: in the run status area rather than beside it", () => {
    const s = fold(failedStart);
    expect(s.runProblem?.message).toBe("The run did not start: kernel exited with code 2 before connecting.");
    expect(s.notice).toBeNull();
  });

  it("says the kernel's own message when the refusal carries one", () => {
    const s = fold([ev("kernel.state", { state: "idle" }), ev("run.finished", { run_id: "r1", status: "refused", reason: "start_failed", message: "no such environment." })]);
    expect(s.runProblem?.message).toBe("The run did not start: no such environment.");
  });

  it("says the kernel did not start when nothing more is known", () => {
    const s = fold([ev("kernel.state", { state: "idle" }), ev("run.finished", { run_id: "r1", status: "refused", reason: "start_failed" })]);
    expect(s.runProblem?.message).toBe("The run did not start: the kernel did not start.");
  });

  it("says the machine never took the workspace's files when the platform gave up waiting", () => {
    const s = fold([ev("run.finished", { run_id: "r1", status: "refused", reason: "folder_not_held" })]);
    expect(s.runProblem?.message).toBe("The run did not start: the machine did not take the workspace's files. Try again.");
  });

  it("says why the kernel did not start when it exits", () => {
    const s = fold(failedStart.slice(0, 2));
    expect(s.notice).toMatchObject({ message: "The kernel did not start: kernel exited with code 2 before connecting.", tone: "danger" });
  });

  it("clears once a later run goes through", () => {
    const s = fold([
      ...failedStart,
      ev("kernel.state", { state: "idle" }, "k2"),
      ev("run.queued", { run_id: "r2", requested_by: ALICE, trigger: "run" }, "k2"),
      ev("run.started", { run_id: "r2" }, "k2"),
      ev("run.finished", { run_id: "r2", status: "ok" }, "k2"),
    ]);
    expect(s.notice).toBeNull();
    expect(s.failure).toBeNull();
    expect(s.runProblem).toBeNull();
  });

  it("stays while later runs fail too, and a notice of another kind is not cleared by a run", () => {
    const failing = fold([...failedStart, ev("run.finished", { run_id: "r2", status: "error" })]);
    expect(failing.runProblem?.message).toContain("The run did not start");
    const other = fold([ev("kernel.state", { state: "idle" }), ev("kernel.exited", { reason: "out_of_memory" }), ev("kernel.state", { state: "idle" }, "k3"), ev("run.finished", { run_id: "r3", status: "ok" }, "k3")]);
    expect(other.notice?.message).toContain("ran out of memory");
  });

  it("names nobody when a cell it needs is being edited", () => {
    const s = fold([ev("kernel.state", { state: "idle" }), ev("run.finished", { run_id: "r5", status: "refused", reason: "upstream_being_edited" })]);
    expect(s.runProblem?.message).toBe("The run did not start: a cell it needs is being edited.");
  });
});

describe("the environment", () => {
  const env = { env_id: "default:.alkera/envs/default", kind: "default", spec_root: "/w/.alkera/envs/default", python: "3.12.13", state: "ready", recorded_in_file: true };

  it("is read from the envs listing", () => {
    const s = fold([{ type: "env.state", env, envs: [env] }]);
    expect(s.kernel.env).toEqual(env);
    expect(s.envs).toEqual([env]);
  });

  it("keeps the box's word on whether environments are shared until it says otherwise", () => {
    expect(fold([{ type: "env.state", env, envs: [env] }]).envsShared).toBeUndefined();
    const notShared = fold([{ type: "env.state", env, envs: [env], shared: false }]);
    expect(notShared.envsShared).toBe(false);
    // The channel's environment events carry no word on it: the listing's stands.
    expect(fold([{ type: "env.state", env, envs: [env], shared: false }, { type: "env.state", env }]).envsShared).toBe(false);
    expect(fold([{ type: "env.state", env, shared: false }, { type: "env.state", env, shared: true }]).envsShared).toBe(true);
  });

  it("stays known when a new kernel starts or a view without a kernel arrives", () => {
    const s = fold([
      { type: "env.state", env },
      ev("kernel.state", { state: "idle" }),
      ev("kernel.state", { state: "starting" }, "k2"),
      ev("kernel.state", { state: "absent" }, "k2"),
      ev("kernel.exited", { reason: "start_failed" }, "k2"),
      { type: "snapshot", seq: 1, view: { kernel: { state: "absent", reactivity: "autorun", env: null }, cells: [] } },
      ev("resync", { view: { kernel: { state: "absent", reactivity: "autorun", env: null }, cells: [] } }),
    ]);
    expect(s.kernel.env).toEqual(env);
  });
});

describe("an interrupt ends", () => {
  const busy = () =>
    fold([
      ev("kernel.state", { state: "busy" }),
      ev("run.queued", { run_id: "r1", requested_by: ALICE, trigger: "run" }),
      ev("run.started", { run_id: "r1" }),
      ev("kernel.state", { interrupt: "signalled" }),
    ]);

  it.each(["interrupted", "error", "ok", "kernel_restarted"])("when the run finishes %s", (status) => {
    const start = busy();
    const s = fold([ev("run.finished", { run_id: "r1", status })], start);
    expect(s.kernel.interrupt).toBe("none");
  });

  it("not while another run is still going", () => {
    const start = busy();
    const s = fold(
      [
        ev("run.queued", { run_id: "r2", requested_by: ALICE, trigger: "run" }),
        ev("run.started", { run_id: "r2" }),
        ev("run.finished", { run_id: "r1", status: "interrupted" }),
      ],
      start,
    );
    expect(s.kernel.interrupt).toBe("signalled");
  });

  it.each([
    ["signalled", "signalled"],
    ["second_signal", "resignalled"],
    ["restarting", "restarting"],
  ])("and follows the engine's %s step", (step, phase) => {
    const start = busy();
    const s = fold([ev("kernel.interrupt", { run_id: "r1", step, at: "2026-10-05T10:00:00Z" })], start);
    expect(s.kernel.interrupt).toBe(phase);
  });

  it("when the engine says the interrupt is done", () => {
    const start = busy();
    expect(fold([ev("kernel.interrupt", { run_id: "r1", step: "done", at: "2026-10-05T10:00:01Z" })], start).kernel.interrupt).toBe("none");
  });

  it("an escalation step for a run that is over changes nothing", () => {
    const start = busy();
    const over = fold([ev("run.finished", { run_id: "r1", status: "interrupted" })], start);
    expect(fold([ev("kernel.interrupt", { run_id: "r1", step: "restarting" })], over).kernel.interrupt).toBe("none");
  });
});

describe("why a run did not go through", () => {
  const queued = (runId: string, body: Record<string, unknown> = {}) => ev("run.queued", { run_id: runId, requested_by: ALICE, trigger: "run", ...body });

  it.each([
    ["a run the machine never answered", { status: "refused", reason: "machine_silent" }, MACHINE_SILENT_MESSAGE, "warning"],
    [
      "an environment that failed to build",
      { status: "error", reason: "env_build_failed", message: "uv sync failed: permission denied" },
      "The run failed: uv sync failed: permission denied.",
      "danger",
    ],
    ["a failure in the engine with nothing more said", { status: "error", reason: "engine" }, "The run failed before any cell ran.", "danger"],
    ["a refusal with its message nested in an error", { status: "refused", reason: "kernel_unavailable", error: { message: "no kernel slot free" } }, "The run did not start: no kernel slot free.", "warning"],
  ])("says so for %s", (_case, finished, message, tone) => {
    const s = fold([ev("kernel.state", { state: "idle" }), queued("r1", { trigger: "run_all" }), ev("run.finished", { run_id: "r1", ...finished })]);
    expect(s.runProblem).toEqual({ run_id: "r1", message, tone, cells: "all" });
  });

  it.each([
    ["a cell raised", { status: "error" }],
    ["it was interrupted", { status: "interrupted", reason: "interrupt" }],
    ["the kernel restarted under it", { status: "kernel_restarted", reason: "restart" }],
    ["it joined another", { status: "coalesced" }],
  ])("says nothing of its own when %s: the cells show that", (_case, finished) => {
    const s = fold([ev("kernel.state", { state: "idle" }), queued("r1"), ev("run.finished", { run_id: "r1", ...finished })]);
    expect(s.runProblem).toBeNull();
  });

  it("is shown on the cells the run named, and on the cell a refusal's notice was about", () => {
    const s = fold([
      ev("kernel.state", { state: "idle" }),
      queued("r1", { targets: ["c2"] }),
      ev("notice", { notice: { kind: "upstream_being_edited", cell_id: "c1", message: "Cell load is being edited by Bob" } }),
      ev("run.finished", { run_id: "r1", status: "refused", reason: "upstream_being_edited" }),
    ]);
    expect(s.runProblem?.cells).toEqual(["c2", "c1"]);
    expect(s.noticeCell).toBeNull();
  });

  it("does not take a cell from a notice about something else", () => {
    const s = fold([
      ev("kernel.state", { state: "idle" }),
      queued("r1", { targets: ["c2"] }),
      ev("notice", { notice: { kind: "cell_skipped_editing", cell_id: "c1", message: "not re-run: someone is editing it" } }),
      ev("run.finished", { run_id: "r1", status: "refused", reason: "machine_silent" }),
    ]);
    expect(s.runProblem?.cells).toEqual(["c2"]);
  });

  it("is on the cells this tab asked for, whether the refusal comes before the answer to the request or after", () => {
    const after = new NotebookRuntimeStore();
    after.noteRun("r1", { kind: "cells", ids: ["c3"] });
    after.dispatch(ev("run.finished", { run_id: "r1", status: "refused", reason: "machine_silent" }));
    expect(after.snapshot.runProblem).toMatchObject({ message: MACHINE_SILENT_MESSAGE, cells: ["c3"] });

    const before = new NotebookRuntimeStore();
    before.dispatch(ev("run.finished", { run_id: "r2", status: "refused", reason: "machine_silent" }));
    expect(before.snapshot.runProblem?.cells).toEqual([]);
    before.noteRun("r2", { kind: "all" });
    expect(before.snapshot.runProblem?.cells).toBe("all");
    // A request answered for another run leaves the problem alone.
    before.noteRun("r9", { kind: "cells", ids: ["c1"] });
    expect(before.snapshot.runProblem?.cells).toBe("all");
  });

  it("clears on the next run that goes through, and not on one that fails in a cell", () => {
    const refused = fold([ev("kernel.state", { state: "idle" }), ev("run.finished", { run_id: "r1", status: "refused", reason: "machine_silent" })]);
    // Each continues from `refused`, so each is its next event.
    const after = (body: Record<string, unknown>): NotebookEvent => ({ ...ev("run.finished", body), seq: refused.seq + 1 });
    expect(fold([after({ run_id: "r2", status: "error" })], refused).runProblem?.message).toBe(MACHINE_SILENT_MESSAGE);
    expect(fold([after({ run_id: "r3", status: "ok" })], refused).runProblem).toBeNull();
  });

  it("is replaced by a later run's reason", () => {
    const s = fold([
      ev("kernel.state", { state: "idle" }),
      ev("run.finished", { run_id: "r1", status: "refused", reason: "machine_silent" }),
      ev("run.finished", { run_id: "r2", status: "refused", reason: "start_failed" }),
    ]);
    expect(s.runProblem).toMatchObject({ run_id: "r2", message: "The run did not start: the kernel did not start." });
  });
});


describe("the environment the running kernel uses", () => {
  const env = (env_id: string, kind: string, state = "ready") => ({ env_id, kind, spec_root: "", python: "3.12.13", state, recorded_in_file: false });
  const DEFAULT = env("default:.alkera/envs/default", "default");
  const PROJECT = env("uv_project:.", "uv_project");

  it("is shown over the one the notebook names, which waits for a restart", () => {
    const s = fold([
      ev("kernel.state", { state: "idle", env_id: DEFAULT.env_id }),
      { type: "env.state", env: PROJECT, envs: [DEFAULT, PROJECT] },
    ]);
    expect(s.kernel.env).toEqual(DEFAULT);
    expect(s.selectedEnv).toEqual(PROJECT);
  });

  it("is the named one once no kernel runs", () => {
    const s = fold([
      ev("kernel.state", { state: "idle", env_id: DEFAULT.env_id }),
      { type: "env.state", env: PROJECT, envs: [DEFAULT, PROJECT] },
      ev("kernel.exited", { reason: "shutdown" }),
    ]);
    expect(s.kernel.env).toEqual(PROJECT);
  });

  it("moves with a new kernel on another environment", () => {
    const s = fold([
      ev("kernel.state", { state: "idle", env_id: DEFAULT.env_id }),
      { type: "env.state", env: PROJECT, envs: [DEFAULT, PROJECT] },
      ev("kernel.state", { state: "starting", env_id: PROJECT.env_id }, "k2"),
    ]);
    expect(s.kernel.env).toEqual(PROJECT);
  });

  it("never reads missing while a kernel runs in it, whatever an older listing said", () => {
    const stale = { ...DEFAULT, state: "missing" };
    const s = fold([{ type: "env.state", env: stale, envs: [stale] }, ev("kernel.state", { state: "busy", env_id: DEFAULT.env_id })]);
    expect(s.kernel.env?.state).toBe("ready");
    // With no kernel the listing's word stands.
    expect(fold([{ type: "env.state", env: stale, envs: [stale] }]).kernel.env?.state).toBe("missing");
  });

  it("is read from a snapshot's kernel too", () => {
    const s = fold([
      { type: "env.state", env: PROJECT, envs: [DEFAULT, PROJECT] },
      { type: "snapshot", kernel_id: "k1", seq: 1, view: { kernel: { state: "idle", reactivity: "autorun", env: DEFAULT, kernel_id: "k1" }, cells: [] } },
    ]);
    expect(s.kernel.env?.env_id).toBe(DEFAULT.env_id);
  });
});

describe("an install's outcome", () => {
  it.each([
    [{ status: "ok", packages: ["polars"] }, "ok", "Installed polars.", "info"],
    [{ status: "error", packages: ["six"], message: "add failed (exit 1)" }, "error", "Could not install six: add failed (exit 1).", "danger"],
  ])("ends the install and says how it went (%o)", (body, status, message, tone) => {
    const start = { ...INITIAL_RUNTIME, installing: true, install: { status: "running" as const, packages: body.packages, message: null } };
    const s = fold([{ type: "env.install", ...body }], start);
    expect(s.installing).toBe(false);
    expect(s.install).toMatchObject({ status, packages: body.packages });
    expect(s.notice).toMatchObject({ message, tone });
  });

  it("ignores an outcome it cannot read", () => {
    const start = { ...INITIAL_RUNTIME, installing: true };
    expect(fold([{ type: "env.install", status: "maybe", packages: ["x"] }], start)).toBe(start);
  });
});

describe("when the environment is read again", () => {
  const DEFAULT = { env_id: "default:x", kind: "default", spec_root: "", python: "", state: "ready", recorded_in_file: false };

  it.each([
    ["an install's outcome", INITIAL_RUNTIME, { type: "env.install", status: "ok", packages: [] }, true],
    ["an environment change", INITIAL_RUNTIME, { type: "env.state", env: DEFAULT }, true],
    ["a kernel on a new environment", INITIAL_RUNTIME, ev("kernel.state", { state: "starting", env_id: "default:x" }), true],
    ["the same kernel's next state", { ...INITIAL_RUNTIME, kernelEnvId: "default:x" }, ev("kernel.state", { state: "idle", env_id: "default:x" }), false],
    ["a cell's output", INITIAL_RUNTIME, ev("cell.output", { cell_id: "c1", output: { "text/plain": "1" } }), false],
  ])("on %s: %s", (_, before, event, expected) => {
    const after = reduceRuntime(before, event as NotebookEvent);
    expect(envMayHaveChanged(before, after, event as NotebookEvent)).toBe(expected);
  });
});

describe("following an install", () => {
  function clock() {
    const pending: { at: number; fn: () => void }[] = [];
    let now = 0;
    return {
      timers: {
        setTimeout: (fn: () => void, ms: number) => {
          const t = { at: now + ms, fn };
          pending.push(t);
          return t;
        },
        clearTimeout: (h: unknown) => {
          const i = pending.indexOf(h as { at: number; fn: () => void });
          if (i >= 0) pending.splice(i, 1);
        },
      },
      advance(ms: number) {
        now += ms;
        for (const t of pending.filter((p) => p.at <= now)) {
          pending.splice(pending.indexOf(t), 1);
          t.fn();
        }
      },
    };
  }

  it("is under way until the outcome arrives, then reads the environment again once", () => {
    const store = new NotebookRuntimeStore();
    const c = clock();
    let settled = 0;
    followInstall(store, ["polars"], () => (settled += 1), c.timers);
    expect(store.snapshot.installing).toBe(true);
    expect(store.snapshot.install).toEqual({ status: "running", action: "install", packages: ["polars"], message: null });
    store.dispatch({ type: "env.install", status: "error", packages: ["polars"], message: "no such package" });
    expect(settled).toBe(1);
    c.advance(INSTALL_RESULT_MS);
    expect(settled).toBe(1);
    expect(store.snapshot.install?.status).toBe("error");
  });

  it("says no result arrived when none did in time, and reads the environment again", () => {
    const store = new NotebookRuntimeStore();
    const c = clock();
    let settled = 0;
    followInstall(store, ["polars"], () => (settled += 1), c.timers);
    c.advance(INSTALL_RESULT_MS - 1);
    expect(store.snapshot.installing).toBe(true);
    expect(settled).toBe(0);
    c.advance(1);
    expect(store.snapshot.installing).toBe(false);
    expect(store.snapshot.install?.status).toBe("unknown");
    expect(store.snapshot.notice).toMatchObject({ message: "No result arrived for installing polars.", tone: "warning" });
    expect(settled).toBe(1);
  });

  it("does nothing more once stopped", () => {
    const store = new NotebookRuntimeStore();
    const c = clock();
    let settled = 0;
    const stop = followInstall(store, ["polars"], () => (settled += 1), c.timers);
    stop();
    c.advance(INSTALL_RESULT_MS);
    expect(settled).toBe(0);
    expect(store.snapshot.install?.status).toBe("running");
  });
});

describe("outputs cleared for everyone", () => {
  it("drops the named cells' outputs and keeps the others and their status", () => {
    const ran = fold([
      ev("cell.output", { cell_id: "c1", output: { type: "display", output_id: "o1", data: { "text/plain": "1" } } }),
      ev("cell.output", { cell_id: "c2", output: { type: "display", output_id: "o2", data: { "text/plain": "2" } } }),
      ev("cell.finished", { cell_id: "c1", run_id: "r1", status: "fresh" }),
    ]);
    const cleared = fold([ev("cell.outputs_cleared", { cell_ids: ["c1"], actor_id: "u1" })], ran);
    expect(cleared.cells.c1!.outputs).toEqual([]);
    expect(cleared.cells.c1!.status).toBe(ran.cells.c1!.status);
    expect(cleared.cells.c2!.outputs.map((o) => o.output_id)).toEqual(["o2"]);
  });

  it("ignores a malformed list", () => {
    const ran = fold([ev("cell.output", { cell_id: "c1", output: { type: "display", output_id: "o1", data: { "text/plain": "1" } } })]);
    const after = fold([ev("cell.outputs_cleared", { cell_ids: "c1" })], ran);
    expect(after.cells.c1!.outputs.map((o) => o.output_id)).toEqual(["o1"]);
  });
});

describe("agents in cells", () => {
  const T0 = Date.parse("2026-10-06T12:00:00Z");
  const at = (ms: number) => new Date(T0 + ms).toISOString();
  const agent = (cell: string, expiresIn?: number) => ({
    who: "Alkera agent for Ada",
    kind: "agent",
    cell_id: cell,
    actor_id: "agent:chat-1",
    caret: false,
    ...(expiresIn === undefined ? {} : { expires_in: expiresIn }),
  });
  const pushed = (presence: unknown[], ms: number): NotebookEvent => ({ type: "presence", presence, received_at: at(ms) });
  const viewed = (presence: unknown[], ms: number): NotebookEvent => ({ type: "view", view: { cells: [], presence }, received_at: at(ms) });
  const placed = (s: RuntimeState) => s.agents.list.map((a) => [a.actor, a.cell_id, a.until === null ? null : a.until - T0]);

  it("places the agent where the server says, each claim lapsing when it said", () => {
    const s = fold([pushed([agent("c1", 15), agent("c2", 14)], 2000)]);
    expect(placed(s)).toEqual([
      ["agent:chat-1", "c1", 17_000],
      ["agent:chat-1", "c2", 16_000],
    ]);
  });

  it("lets each claim go once its time has passed, and keeps the rest", () => {
    const s = fold([pushed([agent("c1", 15), agent("c2", 14)], 2000)]);
    expect(placed(reduceRuntime(s, { type: "presence.lapse", received_at: at(15_999) }))).toHaveLength(2);
    const later = reduceRuntime(s, { type: "presence.lapse", received_at: at(16_000) });
    expect(placed(later)).toEqual([["agent:chat-1", "c1", 17_000]]);
    expect(placed(reduceRuntime(later, { type: "presence.lapse", received_at: at(17_000) }))).toEqual([]);
  });

  it("takes a later list whole, so an agent the server no longer names is gone", () => {
    const s = fold([pushed([agent("c1", 15), agent("c2", 15)], 0), pushed([agent("c2", 15)], 1000)]);
    expect(placed(s)).toEqual([["agent:chat-1", "c2", 16_000]]);
  });

  it("never lets a view heard earlier bring back an agent a later word let go", () => {
    const s = fold([pushed([], 5000), viewed([agent("c1", 15)], 1000)]);
    expect(placed(s)).toEqual([]);
  });

  it("leaves out a claim that had already lapsed when it was heard", () => {
    expect(placed(fold([viewed([agent("c1", 0)], 1000)]))).toEqual([]);
  });

  it("takes a snapshot's view as a word on presence too", () => {
    const s = fold([{ type: "snapshot", kernel_id: K1, seq: 1, view: { cells: [], kernel: { state: "idle" }, presence: [agent("c1", 10)] }, received_at: at(0) }]);
    expect(placed(s)).toEqual([["agent:chat-1", "c1", 10_000]]);
  });

  it("holds a claim from a server that names no expiry until a later word", () => {
    const s = fold([viewed([agent("c1")], 0), { type: "presence.lapse", received_at: at(3_600_000) }]);
    expect(placed(s)).toEqual([["agent:chat-1", "c1", null]]);
  });

  it("names the agent by its actor id, never by a person's caret", () => {
    const s = fold([pushed([{ ...agent("c1", 5), actor_id: undefined }, { who: "Bo", kind: "person", cell_id: "c1", expires_in: 30 }], 0)]);
    expect(placed(s)).toEqual([["Alkera agent for Ada", "c1", 5000]]);
  });

  it("is followed to the end: each agent goes at its time with no word from the server", () => {
    let now = T0;
    const pending: { at: number; fn: () => void }[] = [];
    const timers = {
      setTimeout: (fn: () => void, ms: number) => {
        const t = { at: now + ms, fn };
        pending.push(t);
        return t;
      },
      clearTimeout: (h: unknown) => {
        const i = pending.indexOf(h as { at: number; fn: () => void });
        if (i >= 0) pending.splice(i, 1);
      },
    };
    const advance = (ms: number) => {
      now += ms;
      for (const t of pending.filter((p) => p.at <= now).sort((a, b) => a.at - b.at)) {
        const i = pending.indexOf(t);
        if (i < 0) continue;
        pending.splice(i, 1);
        t.fn();
      }
    };
    const store = new NotebookRuntimeStore(() => new Date(now));
    const stop = followPresence(store, timers, () => now);
    store.dispatch({ type: "presence", presence: [agent("c1", 15), agent("c2", 10)] });
    advance(9_999);
    expect(placed(store.snapshot).map(([, cell]) => cell)).toEqual(["c1", "c2"]);
    advance(1);
    expect(placed(store.snapshot).map(([, cell]) => cell)).toEqual(["c1"]);
    advance(5_000);
    expect(store.snapshot.agents.list).toEqual([]);
    expect(pending).toEqual([]);
    stop();
  });
});
