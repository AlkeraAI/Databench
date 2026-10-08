// The engine's state for one notebook, folded from its events.
//
// The notebook channel (`nb:<item_id>`) delivers a snapshot on join and then
// the engine's events in order (`kernel.state`, `run.*`, `cell.*`, `graph`,
// `comm.*`, `notice`, `env.state`, `resync`), each with the `(kernel_id, seq)`
// it belongs to. The shapes are the engine's (`alkera_notebook.events`): a
// `cell.output` carries a MIME bundle, an error arrives on `cell.finished`,
// the graph and a notice arrive nested, and a `snapshot` or `resync`
// carries the notebook's view (read through `notebookWire.ts`). Comm
// traffic is not state: the tab hands it to the frames' comm bridge. The
// platform adds `presence` (which agents are in which cells, each for how
// long) whenever an agent edits; a view carries the same list. The fold
// is pure: event in, state out, so every event's
// effect is a test. An event from an older kernel, or one already folded (a
// replay after a reconnect), changes nothing. Unknown event types are kept out
// rather than guessed at.

import {
  ABSENT_KERNEL,
  EMPTY_RUNTIME,
  type ActorRef,
  type CellOutput,
  type CellRuntime,
  type CellStatus,
  type EnvInfo,
  type EnvPackage,
  type EnvActionName,
  type InstallStatus,
  type InterruptPhase,
  type KernelState,
  type KernelView,
  type QueuedRun,
  type RunConfirmation,
  type RunProblem,
  type RunTarget,
  type RunTrigger,
  type RuntimeSnapshot,
  type VarSummary,
  installLine,
  sentenceStart,
} from "@alkera/notebook-ui";

import type { NotebookPresence } from "@/api/notebooks";

import {
  actorOf,
  agentsOfView,
  attributionOf,
  cellIdOf,
  cellOfView,
  envOf,
  graphErrors,
  graphOf,
  kernelOfView,
  packageOf,
  queuedBy,
  viewOf,
  type AgentInCell,
} from "./notebookWire";

export interface RuntimeState extends RuntimeSnapshot {
  kernelId: string | null;
  seq: number;
  /** Who asked for each run still remembered, for attribution. */
  runs: Readonly<Record<string, KnownRun>>;
  /** The cells each run this tab knows of was for, by run id: from its
   *  announcement, or from the request this tab sent. */
  targets: Readonly<Record<string, RunProblem["cells"]>>;
  /** The cell the last notice named and why, for the refusal of the same
   *  kind that follows it (the engine says which cell first, then refuses). */
  noticeCell: { kind: string; cell_id: string } | null;
  /** The run whose outputs each cell shows, when known: an output of any
   *  other run starts the cell's outputs over. */
  outputRun: Readonly<Record<string, string>>;
  /** What the server last said about why the kernel could not start, for
   *  the refusal of the run that needed it. */
  failure: string | null;
  /** The environment the kernel runs in, as the engine last named it. */
  kernelEnvId: string | null;
  /** The environment the notebook names (the engine's listing), which a
   *  kernel started now would use. */
  selectedEnv: EnvInfo | null;
  /** The agents the server last placed in cells, and when that was heard. */
  agents: AgentsHeard;
}

/** The server's latest word on which agents are in which cells, heard at
 *  `heardAt` (ms on this tab's clock). A later word replaces it whole; an
 *  earlier one (a cached view read again) changes nothing; each claim goes
 *  once its `until` passes. */
export interface AgentsHeard {
  list: readonly AgentInCell[];
  heardAt: number;
}

/** Who asked for a run, and how. */
export interface KnownRun {
  by: ActorRef;
  trigger: RunTrigger;
  started_at: string | null;
}

export const INITIAL_RUNTIME: RuntimeState = {
  cells: {},
  kernel: ABSENT_KERNEL,
  graph: { edges: [], errors: {} },
  presence: [],
  confirmation: null,
  envs: [],
  packages: [],
  installing: false,
  notice: null,
  kernelId: null,
  seq: 0,
  runs: {},
  targets: {},
  noticeCell: null,
  outputRun: {},
  failure: null,
  runProblem: null,
  install: null,
  kernelEnvId: null,
  selectedEnv: null,
  agents: { list: [], heardAt: Number.NEGATIVE_INFINITY },
};

/** One engine event as the channel carries it. */
export interface NotebookEvent {
  type: string;
  kernel_id?: string | null;
  seq?: number;
  /** When this tab received it (stamped by the store): the finish time of a
   *  cell, which the engine's event does not carry. */
  received_at?: string;
  [key: string]: unknown;
}

const STATUSES = new Set<CellStatus>(["fresh", "edited", "stale", "not_run", "queued", "running", "error", "interrupted", "skipped", "stopped", "disabled"]);
const KERNEL_STATES = new Set<KernelState>(["absent", "starting", "idle", "busy", "restarting", "stopped"]);
const INTERRUPTS = new Set<InterruptPhase>(["none", "signalled", "resignalled", "restarting"]);

const rec = (v: unknown): Record<string, unknown> => (typeof v === "object" && v !== null && !Array.isArray(v) ? (v as Record<string, unknown>) : {});
const str = (v: unknown): string | null => (typeof v === "string" ? v : null);
const num = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);
const strings = (v: unknown): string[] => (Array.isArray(v) ? v.filter((x): x is string => typeof x === "string") : []);

const actor = actorOf;

/** The run statuses after which a run is over. */
const RUN_OVER = new Set(["ok", "finished", "error", "interrupted", "kernel_restarted", "refused", "coalesced"]);
/** The run statuses of a run that went through. */
const RUN_OK = new Set(["ok", "finished"]);
/** The cell statuses a run leaves on a cell it did not execute. */
const NOT_EXECUTED = new Set<CellStatus>(["skipped", "disabled", "queued"]);
/** Notices about a kernel that could not start, cleared by the next run
 *  that goes through. */
const START_NOTICE = /^(?:refused|start)-/;
function cellOf(state: RuntimeState, id: string): CellRuntime {
  return state.cells[id] ?? EMPTY_RUNTIME;
}

function withCell(state: RuntimeState, id: string, patch: Partial<CellRuntime>): RuntimeState {
  return { ...state, cells: { ...state.cells, [id]: { ...cellOf(state, id), ...patch } } };
}

/** A cell about to take an output of `runId`: when its outputs are another
 *  run's, or saved ones, they are cleared first, so a re-run replaces what an
 *  earlier run showed even when the `running` status that normally clears
 *  them never arrived. Outputs of no named run that this kernel produced are
 *  taken to be this run's. */
function forRun(state: RuntimeState, id: string, runId: string | null): RuntimeState {
  if (runId === null) return state;
  const shown = state.outputRun[id];
  if (shown === runId) return state;
  const cell = cellOf(state, id);
  if (shown === undefined && !cell.saved) return { ...state, outputRun: { ...state.outputRun, [id]: runId } };
  const cleared = withCell(state, id, { outputs: [], saved: false, outdated: false });
  return { ...cleared, outputRun: { ...state.outputRun, [id]: runId } };
}

function withoutKey<T>(map: Readonly<Record<string, T>>, key: string): Record<string, T> {
  const rest = { ...map };
  delete rest[key];
  return rest;
}

/** A cell's outputs as a view lists them, or `null` when it lists none. */
export function outputsOf(id: string, list: unknown): CellOutput[] | null {
  if (!Array.isArray(list)) return null;
  return list.map((o, i) => output(o, `${id}/${i}`)).filter((o): o is CellOutput => o !== null);
}

/** `map` with the run that produced `id`'s outputs, or with none known. */
function runOf(map: Readonly<Record<string, string>>, id: string, runId: string | null): Readonly<Record<string, string>> {
  return runId === null ? withoutKey(map, id) : { ...map, [id]: runId };
}

/** A message the server sent, as the end of one of ours: no trailing period
 *  (ours adds it). */
function detail(v: unknown): string | null {
  const text = str(v)?.trim().replace(/\.+$/, "") ?? "";
  return text === "" ? null : text;
}

function output(v: unknown, fallbackId: string): CellOutput | null {
  const r = rec(v);
  // The engine sends a bare MIME bundle; a typed output is accepted too.
  if (typeof r.type !== "string") {
    return Object.keys(r).length === 0 ? null : { output_id: fallbackId, type: "display", data: r };
  }
  const id = str(r.output_id) ?? fallbackId;
  if (r.type === "stream" && (r.name === "stdout" || r.name === "stderr") && typeof r.text === "string") {
    return { output_id: id, type: "stream", name: r.name, text: r.text };
  }
  if (r.type === "error") {
    const e = rec(r.error);
    return {
      output_id: id,
      type: "error",
      error: {
        ename: str(e.ename) ?? "Error",
        evalue: str(e.evalue) ?? "",
        traceback: strings(e.traceback),
        ...(str(e.kind) ? { kind: str(e.kind)! } : {}),
        ...(Array.isArray(e.names) ? { names: strings(e.names) } : {}),
        ...(Array.isArray(e.cells) ? { cells: strings(e.cells) } : {}),
      },
    };
  }
  const data = rec(r.data);
  if (Object.keys(data).length === 0) return null;
  return { output_id: id, type: "display", data, ...(r.metadata ? { metadata: rec(r.metadata) } : {}) };
}

function kernelPatch(prev: KernelView, v: Record<string, unknown>): KernelView {
  const next: KernelView = { ...prev };
  if (KERNEL_STATES.has(v.state as KernelState)) next.state = v.state as KernelState;
  if (v.reactivity === "autorun" || v.reactivity === "lazy") next.reactivity = v.reactivity;
  if ("memory_bytes" in v) next.memory_bytes = num(v.memory_bytes);
  if ("memory_limit_bytes" in v) next.memory_limit_bytes = num(v.memory_limit_bytes);
  if ("threshold_bytes" in v) next.memory_limit_bytes = num(v.threshold_bytes);
  if ("started_at" in v) next.started_at = str(v.started_at);
  if (INTERRUPTS.has(v.interrupt as InterruptPhase)) next.interrupt = v.interrupt as InterruptPhase;
  if ("env" in v && v.env !== null) next.env = envOf(v.env);
  if (next.state === "idle" || next.state === "stopped" || next.state === "absent") next.interrupt = "none";
  return next;
}

/** The environment a kernel event or view names for its kernel, if any: a
 *  `kernel.state` carries its `env_id`, a view's kernel its `env`. */
function kernelEnvIdOf(v: unknown): string | null {
  const r = rec(v);
  return str(r.env_id) ?? str(rec(r.env).env_id);
}

/** The kernel states in which a kernel is there, on an environment. */
const KERNEL_UP = new Set<KernelState>(["starting", "idle", "busy", "restarting"]);

/** The environment states that say it is not built: wrong for one a kernel
 *  runs in, which only an older listing can say. */
const NOT_BUILT = new Set(["missing", ""]);

/** `kernel.env` as the editor shows it: the environment the running kernel
 *  uses when one runs (from the listing, which knows its name and state), the
 *  one the notebook names otherwise. A kernel's environment is built, whatever
 *  a listing read before it started says. */
function withShownEnv(state: RuntimeState): RuntimeState {
  const id = state.kernelEnvId;
  let shown: EnvInfo | null;
  if (id !== null && KERNEL_UP.has(state.kernel.state)) {
    const known =
      (state.selectedEnv?.env_id === id ? state.selectedEnv : null) ??
      state.envs.find((e) => e.env_id === id) ??
      (state.kernel.env?.env_id === id ? state.kernel.env : null);
    const env = known ?? { env_id: id, kind: id.split(":")[0] ?? "", spec_root: "", python: "", state: "ready", recorded_in_file: false };
    shown = NOT_BUILT.has(env.state) ? { ...env, state: "ready" } : env;
  } else {
    shown = state.selectedEnv ?? state.kernel.env;
  }
  if (sameEnv(shown, state.kernel.env)) return state;
  return { ...state, kernel: { ...state.kernel, env: shown } };
}

function sameEnv(a: EnvInfo | null, b: EnvInfo | null): boolean {
  if (a === b) return true;
  if (a === null || b === null) return false;
  return (
    a.env_id === b.env_id &&
    a.kind === b.kind &&
    a.spec_root === b.spec_root &&
    a.python === b.python &&
    a.state === b.state &&
    a.recorded_in_file === b.recorded_in_file
  );
}

/** Whether an event may have changed the environment or its packages, so the
 *  listing and the package list are asked again: an install's outcome, an
 *  environment's change, or a kernel now on another environment. */
export function envMayHaveChanged(before: RuntimeState, after: RuntimeState, event: NotebookEvent): boolean {
  if (event.type === "env.install" || event.type === "env.state") return true;
  return after.kernelEnvId !== before.kernelEnvId;
}

/** Fold one event into the state. */
export function reduceRuntime(state: RuntimeState, event: NotebookEvent): RuntimeState {
  const next = fold(state, event);
  return next === state ? state : withShownEnv(next);
}

/** When an event was heard, in ms on this tab's clock. */
function heardAt(event: NotebookEvent): number {
  const at = typeof event.received_at === "string" ? Date.parse(event.received_at) : Number.NaN;
  return Number.isFinite(at) ? at : Date.now();
}

/** The agents a presence list places in cells, when it is newer than what
 *  this tab holds. Claims already lapsed when heard are left out. */
function withAgents(state: RuntimeState, presence: unknown, event: NotebookEvent): RuntimeState {
  if (!Array.isArray(presence)) return state;
  const at = heardAt(event);
  if (at < state.agents.heardAt) return state;
  const rows = presence.filter((p): p is NotebookPresence => typeof rec(p).who === "string" && typeof rec(p).cell_id === "string");
  const list = agentsOfView(rows, at).filter((a) => a.until === null || a.until > at);
  return { ...state, agents: { list, heardAt: at } };
}

/** The earliest moment one of `agents` lapses, or `null` when none will. */
export function nextLapse(agents: AgentsHeard): number | null {
  let soonest: number | null = null;
  for (const a of agents.list) if (a.until !== null && (soonest === null || a.until < soonest)) soonest = a.until;
  return soonest;
}

function fold(state: RuntimeState, event: NotebookEvent): RuntimeState {
  // A replay of something already folded, or an event of a kernel that is
  // gone, is dropped. `kernel.state` and `snapshot` may name a new kernel.
  const kernelId = event.kernel_id ?? null;
  const seq = typeof event.seq === "number" ? event.seq : null;
  const newKernel = kernelId !== null && kernelId !== state.kernelId;
  // The numbers a socket is sent rise but skip: the server keeps a box's
  // answers and other people's frame traffic, and the tab takes widget
  // traffic itself. A skip is never a loss; the server says a loss with a
  // resync, and the snapshot it subscribes again for replaces this state.
  if (!newKernel && seq !== null && seq <= state.seq && event.type !== "snapshot") return state;
  if (newKernel && event.type !== "kernel.state" && event.type !== "snapshot" && state.kernelId !== null) {
    // Events of a different kernel before it was announced: only the
    // announcement switches kernels.
    if (event.type !== "kernel.exited") return state;
  }
  let next: RuntimeState = state;
  if (seq !== null) next = { ...next, seq: newKernel ? seq : Math.max(seq, state.seq) };
  if (kernelId !== null && (event.type === "kernel.state" || event.type === "snapshot")) next = { ...next, kernelId };

  switch (event.type) {
    case "snapshot": {
      // The notebook's view, then what the kernel's own snapshot adds to it
      // (outputs, durations, variables), when the channel held one.
      // A snapshot that carries no outputs for a cell (a join, a resync, a
      // worker that restarted) never takes away the ones already shown: they
      // stay until a snapshot or a run names others.
      const view = viewOf(event.view);
      const cells: Record<string, CellRuntime> = {};
      const outputRun: Record<string, string> = {};
      const keep = (id: string) => {
        const run = state.outputRun[id];
        if (run !== undefined) outputRun[id] = run;
      };
      const replaced = (id: string, list: unknown[], runId: string | null) => {
        delete outputRun[id];
        if (runId !== null) outputRun[id] = runId;
        return outputsOf(id, list) ?? [];
      };
      for (const raw of view?.cells ?? []) {
        const id = cellIdOf(raw);
        if (id === null) continue;
        const cell = cellOfView(state.cells[id], raw);
        keep(id);
        const listed = rec(raw).outputs;
        cells[id] = Array.isArray(listed) ? { ...cell, outputs: replaced(id, listed, cell.last_run?.run_id ?? null) } : cell;
      }
      for (const [id, raw] of Object.entries(rec(event.cells))) {
        const c = rec(raw);
        const from = cells[id] ?? state.cells[id] ?? EMPTY_RUNTIME;
        if (cells[id] === undefined) keep(id);
        const lastRun = c.last_run ? attribution(c.last_run) : from.last_run;
        cells[id] = {
          ...from,
          status: STATUSES.has(c.status as CellStatus) ? (c.status as CellStatus) : from.status,
          outputs: Array.isArray(c.outputs) ? replaced(id, c.outputs, str(c.run_id) ?? lastRun?.run_id ?? null) : from.outputs,
          saved: "saved" in c ? c.saved === true : from.saved,
          outdated: "outdated" in c ? c.outdated === true : from.outdated,
          defs: "defs" in c ? strings(c.defs) : from.defs,
          refs: "refs" in c ? strings(c.refs) : from.refs,
          graph_errors: "graph_errors" in c ? graphErrors(c.graph_errors) : from.graph_errors,
          duration_ms: "duration_ms" in c ? num(c.duration_ms) : from.duration_ms,
          last_run: lastRun,
          variables: Array.isArray(c.variables) ? (c.variables as VarSummary[]) : from.variables,
        };
      }
      const g = event.graph ? graphOf(event.graph) : null;
      const kernel = kernelPatch(kernelOfView({ ...ABSENT_KERNEL, env: state.kernel.env }, view?.kernel), rec(event.kernel));
      kernel.queue = queueOf("queue" in event ? event.queue : view?.kernel?.queue, state.runs);
      return withAgents(
        {
          ...next,
          cells,
          outputRun,
          kernel,
          kernelEnvId: kernelEnvIdOf(view?.kernel) ?? kernelEnvIdOf(event.kernel) ?? next.kernelEnvId,
          kernelId: kernelId ?? view?.kernel?.kernel_id ?? null,
          graph: g?.graph ?? next.graph,
          seq: seq ?? 0,
          confirmation: null,
        },
        view?.presence,
        event,
      );
    }
    case "kernel.state": {
      const envId = kernelEnvIdOf(event);
      if (envId !== null) next = { ...next, kernelEnvId: envId };
      const kernel = kernelPatch(
        newKernel && state.kernelId !== null ? { ...ABSENT_KERNEL, reactivity: state.kernel.reactivity, env: state.kernel.env } : next.kernel,
        event,
      );
      if ("queue" in event) kernel.queue = queueOf(event.queue, next.runs);
      let out: RuntimeState = { ...next, kernel };
      if (newKernel && state.kernelId !== null) {
        // A new kernel: nothing ran in it yet. Outputs stay, as saved ones.
        const cells: Record<string, CellRuntime> = {};
        for (const [id, c] of Object.entries(state.cells)) {
          if (c) cells[id] = { ...c, status: c.status === "disabled" ? "disabled" : "not_run", saved: c.outputs.length > 0 };
        }
        out = { ...out, cells, confirmation: null };
      }
      return out;
    }
    case "kernel.exited": {
      const reason = str(event.reason) ?? "";
      const why = detail(event.message);
      const startFailed = reason === "start_failed";
      // A restart someone asked for is not a stop: a new kernel follows.
      const restarted = reason === "restart";
      const message =
        restarted
          ? KERNEL_RESTARTED_MESSAGE
          : reason === "out_of_memory"
          ? "The kernel ran out of memory and was stopped. Variables were lost; run the cells again."
          : reason === "interrupt_restart"
            ? "The interrupt did not stop the run, so the kernel was restarted."
            : startFailed
              ? why ? `The kernel did not start: ${why}.` : "The kernel did not start."
              : "The kernel stopped.";
      return {
        ...next,
        kernel: { ...next.kernel, state: restarted ? "restarting" : "stopped", interrupt: "none", queue: [] },
        failure: startFailed ? (why ?? next.failure) : next.failure,
        notice: {
          id: `${startFailed ? "start" : "exit"}-${kernelId ?? ""}-${seq ?? 0}`,
          message,
          tone: reason === "out_of_memory" || reason === "interrupt_restart" || startFailed ? "danger" : "info",
        },
      };
    }
    case "kernel.interrupt": {
      const step = str(event.step);
      const phase: InterruptPhase | null =
        step === "signalled"
          ? "signalled"
          : step === "second_signal"
            ? "resignalled"
            : step === "restarting"
              ? "restarting"
              : step === "done"
                ? "none"
                : null;
      if (phase === null) return next;
      if (phase === "none") return { ...next, kernel: { ...next.kernel, interrupt: "none" } };
      // An escalation for a run that is no longer going says nothing.
      const runId = str(event.run_id);
      if (runId !== null && !next.kernel.queue.some((r) => r.run_id === runId && r.status === "running")) return next;
      return { ...next, kernel: { ...next.kernel, interrupt: phase } };
    }
    case "run.queued": {
      const runId = str(event.run_id);
      if (runId === null) return next;
      const by = actor(event.requested_by ?? event.by);
      const trigger = (str(event.trigger) ?? "run") as RunTrigger;
      const targets = strings(event.targets).length > 0 ? strings(event.targets) : planTargets(event.plan);
      if (next.kernel.queue.some((r) => r.run_id === runId)) return next;
      const queue = next.kernel.queue.concat({ run_id: runId, by, trigger, status: "queued", targets });
      const cells = trigger === "run_all" ? "all" : targets.length > 0 ? targets : next.targets[runId];
      return {
        ...next,
        kernel: { ...next.kernel, queue },
        runs: { ...next.runs, [runId]: { by, trigger, started_at: null } },
        targets: cells === undefined ? next.targets : { ...next.targets, [runId]: cells },
      };
    }
    case "run.started": {
      const runId = str(event.run_id);
      if (runId === null) return next;
      const at = str(event.started_at) ?? str(event.received_at);
      const targets = planTargets(event.plan);
      const listed = next.kernel.queue.some((r) => r.run_id === runId);
      const known = next.runs[runId];
      const queue = listed
        ? next.kernel.queue.map((r) => (r.run_id === runId ? { ...r, status: "running" as const, targets: targets.length > 0 ? targets : r.targets } : r))
        : next.kernel.queue.concat({ run_id: runId, by: known?.by ?? actor(null), trigger: known?.trigger ?? "run", status: "running", targets });
      const runs = known ? { ...next.runs, [runId]: { ...known, started_at: at } } : next.runs;
      const runTargets = targets.length > 0 && next.targets[runId] === undefined ? { ...next.targets, [runId]: targets } : next.targets;
      const reloaded = strings(event.reloaded);
      const notice = reloaded.length > 0 ? { id: `reload-${runId}`, message: `Reloaded ${reloaded.join(", ")}`, tone: "info" as const } : next.notice;
      return { ...next, kernel: { ...next.kernel, queue, state: "busy" }, runs, targets: runTargets, notice, confirmation: next.confirmation?.run_id === runId ? null : next.confirmation };
    }
    case "run.needs_confirmation": {
      const runId = str(event.run_id);
      if (runId === null) return next;
      const plan = (Array.isArray(event.plan) ? event.plan : []).map((p) => {
        const r = rec(p);
        return {
          cell_id: str(r.cell_id) ?? "",
          name: str(r.name) ?? "_",
          reason: (r.reason === "upstream" || r.reason === "descendant" ? r.reason : "target") as "target" | "upstream" | "descendant",
          last_duration_ms: num(r.last_duration_ms),
        };
      });
      const confirmation: RunConfirmation = { run_id: runId, plan, estimate_s: num(event.estimate_s) ?? 0 };
      return { ...next, confirmation, kernel: { ...next.kernel, queue: next.kernel.queue.filter((r) => r.run_id !== runId) } };
    }
    case "run.finished": {
      const runId = str(event.run_id);
      if (runId === null) return next;
      const queue = next.kernel.queue.filter((r) => r.run_id !== runId);
      const status = str(event.status) ?? "";
      const refused = status === "refused";
      const ok = RUN_OK.has(status);
      // An interrupt is over once no run it could be stopping is still going.
      const interrupt = RUN_OVER.has(status) && !queue.some((r) => r.status === "running") ? "none" : next.kernel.interrupt;
      const reason = str(event.reason);
      const why =
        detail(event.message) ?? detail(event.detail) ?? detail(rec(event.error).message) ?? (reason === "start_failed" ? next.failure : null);
      const message = runProblemMessage(status, reason, why);
      const named = next.targets[runId] ?? (next.runs[runId]?.trigger === "run_all" ? "all" : []);
      const about = refused && next.noticeCell !== null && next.noticeCell.kind === reason ? next.noticeCell.cell_id : null;
      const cells: RunProblem["cells"] = about !== null && named !== "all" && !named.includes(about) ? [...named, about] : named;
      const runProblem: RunProblem | null | undefined =
        message !== null ? { run_id: runId, message, tone: refused ? "warning" : "danger", cells } : ok ? null : next.runProblem;
      // The status area says why the run did not go through; a notice that
      // the kernel could not start says the same, so it gives way to it.
      const notice =
        (message !== null || ok) && next.notice !== null && START_NOTICE.test(next.notice.id) ? null : next.notice;
      return {
        ...next,
        kernel: { ...next.kernel, queue, interrupt, state: queue.length === 0 && next.kernel.state === "busy" ? "idle" : next.kernel.state },
        confirmation: next.confirmation?.run_id === runId ? null : next.confirmation,
        failure: ok ? null : next.failure,
        notice,
        runProblem,
        noticeCell: null,
        targets: withoutKey(next.targets, runId),
      };
    }
    case "cell.started":
    case "cell.status": {
      const id = str(event.cell_id);
      const status = event.type === "cell.started" ? "running" : (event.status as CellStatus);
      if (id === null || !STATUSES.has(status)) return next;
      // Every status says whose editing the cell's re-run waits for, or nobody's.
      const patch: Partial<CellRuntime> = { status, rerun_waits_for: str(event.rerun_waits_for) };
      let out = next;
      // A run starting replaces the outputs: what follows is this run's.
      if (status === "running") {
        patch.notice = null;
        patch.outputs = [];
        patch.saved = false;
        patch.outdated = false;
        const runId = str(event.run_id);
        out = { ...out, outputRun: runId === null ? withoutKey(out.outputRun, id) : { ...out.outputRun, [id]: runId } };
      }
      if ("outdated" in event) patch.outdated = event.outdated === true;
      return withCell(out, id, patch);
    }
    case "cell.output": {
      const id = str(event.cell_id);
      if (id === null) return next;
      next = forRun(next, id, str(event.run_id));
      const cell = cellOf(next, id);
      const out = output(event.output ?? event, `${id}/${cell.outputs.length}`);
      if (out === null) return next;
      const outputs = event.mode === "replace" ? [out] : cell.outputs.filter((o) => o.output_id !== out.output_id).concat(out);
      return withCell(next, id, { outputs, saved: false });
    }
    case "cell.outputs_cleared": {
      // Someone cleared these cells' outputs for everyone.
      const ids = Array.isArray(event.cell_ids) ? event.cell_ids.filter((c): c is string => typeof c === "string") : [];
      for (const id of ids) next = withCell(next, id, { outputs: [], saved: false });
      return next;
    }
    case "cell.stream": {
      const id = str(event.cell_id);
      const text = str(event.text);
      if (id === null || text === null) return next;
      const name = event.name === "stderr" ? "stderr" : "stdout";
      next = forRun(next, id, str(event.run_id));
      const cell = cellOf(next, id);
      return withCell(next, id, {
        outputs: [...cell.outputs, { output_id: `${id}/s${seq ?? cell.outputs.length}`, type: "stream", name, text }],
        saved: false,
      });
    }
    case "cell.finished": {
      const id = str(event.cell_id);
      if (id === null) return next;
      const runId = str(event.run_id);
      const run = runId ? next.runs[runId] : undefined;
      if (!NOT_EXECUTED.has(event.status as CellStatus)) next = forRun(next, id, runId);
      const patch: Partial<CellRuntime> = { duration_ms: num(event.duration_ms) };
      if (STATUSES.has(event.status as CellStatus)) patch.status = event.status as CellStatus;
      if (event.error) {
        const err = output({ type: "error", error: event.error }, `${id}/error`);
        if (err) patch.outputs = cellOf(next, id).outputs.filter((o) => o.output_id !== err.output_id).concat(err);
      }
      if ("defs" in event) patch.defs = strings(event.defs);
      if ("refs" in event) patch.refs = strings(event.refs);
      if (runId !== null) {
        patch.last_run = {
          run_id: runId,
          by: event.by ? actor(event.by) : (run?.by ?? { kind: "system", id: "", display_name: "" }),
          trigger: (str(event.trigger) as RunTrigger | null) ?? run?.trigger ?? "run",
          started_at: str(event.started_at) ?? run?.started_at ?? null,
          finished_at: str(event.finished_at) ?? str(event.received_at),
        };
      }
      return withCell(next, id, patch);
    }
    case "cell.variables": {
      const id = str(event.cell_id);
      if (id === null || !Array.isArray(event.variables)) return next;
      const variables = (event.variables as unknown[])
        .map((v) => rec(v))
        .filter((v) => typeof v.name === "string")
        .map((v) => ({ ...(v as unknown as VarSummary), cell_id: id }));
      return withCell(next, id, { variables });
    }
    case "graph": {
      const { graph, names } = graphOf(event.graph ?? event);
      let out: RuntimeState = { ...next, graph };
      for (const [id, n] of Object.entries(names)) out = withCell(out, id, { defs: n.defs, refs: n.refs, graph_errors: graph.errors[id] ?? [] });
      return out;
    }
    case "notice": {
      const notice = rec(event.notice);
      const said = str(notice.message) ?? str(event.message);
      if (said === null) return next;
      // A newer environment build is the kernel's state, offered as a
      // restart beside the kernel rather than a banner to close.
      if (notice.kind === ENV_NEWER_NOTICE) return { ...next, kernel: { ...next.kernel, env_outdated: true } };
      // The engine's words, shown as a sentence of their own.
      const message = sentenceStart(said);
      const unavailable = notice.kind === "kernel_unavailable";
      const about = str(notice.cell_id);
      if (about !== null && !unavailable) {
        // A notice about one cell is said on that cell, never as a banner
        // someone has to close. It stays until the cell next runs.
        const kind = str(notice.kind) ?? "";
        const marked: RuntimeState = { ...next, noticeCell: { kind, cell_id: about } };
        // A cell an autorun skipped says so from its own state
        // (`rerun_waits_for`), which clears itself.
        return kind === "cell_skipped_editing" ? marked : withCell(marked, about, { notice: { kind, message } });
      }
      return {
        ...next,
        noticeCell: about !== null ? { kind: str(notice.kind) ?? "", cell_id: about } : next.noticeCell,
        failure: unavailable ? (detail(said) ?? next.failure) : next.failure,
        notice: { id: `${unavailable ? "start" : "n"}-${seq ?? Date.now()}`, message, tone: "warning" },
      };
    }
    case "resync": {
      // A resync with no view asks the tab to subscribe again (the tab does);
      // the snapshot that answers it restores everything.
      const view = viewOf(event.view);
      if (view === null) return next;
      const kernel = kernelOfView({ ...next.kernel }, view.kernel);
      kernel.queue = queueOf(view.kernel?.queue, next.runs);
      const cells: Record<string, CellRuntime | undefined> = { ...next.cells };
      let outputRun = next.outputRun;
      for (const raw of view.cells ?? []) {
        const id = cellIdOf(raw);
        if (id === null) continue;
        const cell = cellOfView(cellOf(next, id), raw);
        const listed = outputsOf(id, rec(raw).outputs);
        cells[id] = listed === null ? cell : { ...cell, outputs: listed };
        if (listed !== null) outputRun = runOf(outputRun, id, cell.last_run?.run_id ?? null);
      }
      return withAgents(
        {
          ...next,
          kernel,
          cells,
          outputRun,
          kernelEnvId: kernelEnvIdOf(view.kernel) ?? next.kernelEnvId,
          kernelId: view.kernel?.kernel_id ?? next.kernelId,
          seq: seq ?? next.seq,
        },
        view.presence,
        event,
      );
    }
    case "view": {
      // The notebook's view as its route answers it, read when the tab
      // opens: it fills in the outputs of cells that show none yet (a member
      // opening after a run), and never replaces what the channel brought.
      const view = viewOf(event.view);
      if (view === null) return next;
      let out = withAgents(next, view.presence, event);
      for (const raw of view.cells ?? []) {
        const id = cellIdOf(raw);
        const listed = id === null ? null : outputsOf(id, rec(raw).outputs);
        if (id === null || listed === null || listed.length === 0) continue;
        const cell = cellOf(out, id);
        if (cell.outputs.length > 0 || out.outputRun[id] !== undefined) continue;
        const shown = cellOfView(cell, raw);
        out = withCell(out, id, { outputs: listed, saved: shown.saved, outdated: shown.outdated, last_run: shown.last_run });
      }
      return out;
    }
    case "presence": {
      // The server's word on who is in which cell, sent when an agent edits:
      // the whole list, replacing the one held.
      return withAgents(next, event.presence, event);
    }
    case "presence.lapse": {
      // This tab's clock passed a claim's `until`: that agent is gone.
      const now = heardAt(event);
      const list = next.agents.list.filter((a) => a.until === null || a.until > now);
      return list.length === next.agents.list.length ? next : { ...next, agents: { ...next.agents, list } };
    }
    case "env.state": {
      // The notebook's environment (the engine's listing, or its word that
      // the environment changed): what a kernel started now would use. The
      // running kernel's own is named by its `kernel.state`.
      const env = envOf(event.env);
      const listed = Array.isArray(event.envs) ? event.envs.map(envOf).filter((e): e is EnvInfo => e !== null) : next.envs;
      return {
        ...next,
        selectedEnv: env ?? next.selectedEnv,
        envs: env === null ? listed : listed.map((e) => (e.env_id === env.env_id ? env : e)),
        packages: Array.isArray(event.packages) ? event.packages.map(packageOf).filter((p): p is EnvPackage => p !== null) : next.packages,
        requirements: Array.isArray(event.requirements) ? strings(event.requirements) : next.requirements,
        installing: typeof event.installing === "boolean" ? event.installing : next.installing,
        envsShared: typeof event.shared === "boolean" ? event.shared : next.envsShared,
      };
    }
    case "env.install": {
      // How an install ended, which the request to install never says: it
      // only answers that the install was accepted.
      const status = event.status === "ok" ? "ok" : event.status === "error" ? "error" : null;
      if (status === null) return next;
      const action = event.action === "build" || event.action === "remove" || event.action === "cancel" ? event.action : "install";
      const install: InstallStatus = { status, action, packages: strings(event.packages), message: detail(event.message) };
      return {
        ...next,
        installing: false,
        install,
        notice: { id: `install-${Date.now()}`, message: installLine(install), tone: status === "ok" ? "info" : "danger" },
      };
    }
    default:
      return next;
  }
}

function attribution(v: unknown) {
  return attributionOf(v) ?? { run_id: str(rec(v).run_id) ?? "", by: actor(null), trigger: "run" as RunTrigger, started_at: null, finished_at: null };
}

/** The engine's notice that a newer environment build is ready while the
 *  kernel still runs on an older one. */
const ENV_NEWER_NOTICE = "env_newer";

/** What the notebook says once a restart someone asked for has stopped the
 *  old kernel. */
export const KERNEL_RESTARTED_MESSAGE = "Kernel restarted.";
/** A run the machine never answered. */
export const MACHINE_SILENT_MESSAGE = "The machine did not answer. Try again.";

/** Why a run that ended `status` did not go through, or null when it did or
 *  when its own cells say why (a cell that raised shows its error; an
 *  interrupt or a restart was asked for). A run refused, or one that failed
 *  before any cell ran (`error` with a reason), has nothing else to show it. */
export function runProblemMessage(status: string, reason: string | null, why: string | null): string | null {
  const refused = status === "refused";
  if (!refused && !(status === "error" && reason !== null)) return null;
  if (reason === "machine_silent") return MACHINE_SILENT_MESSAGE;
  if (refused) return refusedMessage(reason, why);
  return why !== null ? `The run failed: ${why}.` : "The run failed before any cell ran.";
}

/** The cells a request for `target` is for, as far as the request says. */
export function targetCells(target: RunTarget): RunProblem["cells"] {
  if (target.kind === "all") return "all";
  if (target.kind === "cells") return target.ids;
  if (target.kind === "above" || target.kind === "below") return [target.id];
  return [];
}

/** Why a run did not start, in the words the server gave when it gave any. */
export function refusedMessage(reason: string | null, why: string | null = null): string {
  if (reason === "upstream_being_edited") return "The run did not start: a cell it needs is being edited.";
  if (why !== null) return `The run did not start: ${why}.`;
  if (reason === "start_failed") return "The run did not start: the kernel did not start.";
  if (reason === "folder_not_held") return "The run did not start: the machine did not take the workspace's files. Try again.";
  return "The run did not start.";
}

function planTargets(plan: unknown): string[] {
  if (!Array.isArray(plan)) return [];
  return plan.map(rec).filter((p) => p.reason === "target" && typeof p.cell_id === "string").map((p) => p.cell_id as string);
}

/** A view's queue. A run's `by` there may be a bare id, so who asked is
 *  taken from the run's own announcement when this tab heard it. */
function queueOf(v: unknown, runs: RuntimeState["runs"]): QueuedRun[] {
  if (!Array.isArray(v)) return [];
  return v.map(rec).filter((r) => typeof r.run_id === "string").map((r) => ({
    run_id: r.run_id as string,
    by: runs[r.run_id as string]?.by ?? queuedBy(r.by ?? r.requested_by),
    trigger: (str(r.trigger) ?? "run") as RunTrigger,
    status: r.status === "running" ? "running" : "queued",
    targets: strings(r.targets),
  }));
}

/** The engine's interrupt escalation (a signal, a second one, then a restart),
 *  in seconds after the first: its default configuration. The engine reports
 *  only the restart, so the steps before it are shown on this schedule. */
export const INTERRUPT_ESCALATION_S: readonly [number, number] = [3, 7];

/** Show an interrupt's escalation while the run it targets is still going.
 *  Returns a function that stops following it. */
export function followInterrupt(
  store: NotebookRuntimeStore,
  timers: { setTimeout(fn: () => void, ms: number): unknown; clearTimeout(handle: unknown): void } = {
    setTimeout: (fn, ms) => globalThis.setTimeout(fn, ms),
    clearTimeout: (h) => globalThis.clearTimeout(h as ReturnType<typeof setTimeout>),
  },
): () => void {
  const busy = () => store.snapshot.kernel.state === "busy" && store.snapshot.kernel.interrupt !== "none";
  const set = (interrupt: InterruptPhase) => store.patch({ kernel: { ...store.snapshot.kernel, interrupt } });
  set("signalled");
  const [second, restart] = INTERRUPT_ESCALATION_S;
  const a = timers.setTimeout(() => {
    if (busy()) set("resignalled");
  }, second * 1000);
  const b = timers.setTimeout(() => {
    if (busy()) set("restarting");
  }, restart * 1000);
  return () => {
    timers.clearTimeout(a);
    timers.clearTimeout(b);
  };
}

/** How long an install is waited on for its outcome before the notebook says
 *  none arrived. Building an environment can take minutes. */
export const INSTALL_RESULT_MS = 180_000;

/** Follow an install the server accepted until its outcome arrives on the
 *  channel (an `env.install` event), or say after `INSTALL_RESULT_MS` that
 *  none did. `onSettled` runs once either way, for the tab to read the
 *  environment again. Returns a function that stops following it. */
export function followInstall(
  store: NotebookRuntimeStore,
  packages: readonly string[],
  onSettled: () => void,
  timers: { setTimeout(fn: () => void, ms: number): unknown; clearTimeout(handle: unknown): void } = {
    setTimeout: (fn, ms) => globalThis.setTimeout(fn, ms),
    clearTimeout: (h) => globalThis.clearTimeout(h as ReturnType<typeof setTimeout>),
  },
  action: EnvActionName = "install",
): () => void {
  store.patch({ installing: true, install: { status: "running", action, packages, message: null } });
  let over = false;
  let stopListening = () => {};
  let timer: unknown = null;
  const stop = () => {
    over = true;
    stopListening();
    if (timer !== null) timers.clearTimeout(timer);
  };
  const settle = () => {
    if (over) return;
    stop();
    onSettled();
  };
  stopListening = store.subscribe(() => {
    if (store.snapshot.install?.status !== "running") settle();
  });
  timer = timers.setTimeout(() => {
    if (over) return;
    const install: InstallStatus = { status: "unknown", action, packages, message: null };
    stop();
    store.patch({ installing: false, install, notice: { id: `install-${Date.now()}`, message: installLine(install), tone: "warning" } });
    onSettled();
  }, INSTALL_RESULT_MS);
  return stop;
}

/** Let each agent the server placed in a cell go when its claim lapses: a
 *  `presence.lapse` is dispatched at the earliest `until` the store holds,
 *  then at the next one. Returns a function that stops following. */
export function followPresence(
  store: NotebookRuntimeStore,
  timers: { setTimeout(fn: () => void, ms: number): unknown; clearTimeout(handle: unknown): void } = {
    setTimeout: (fn, ms) => globalThis.setTimeout(fn, ms),
    clearTimeout: (h) => globalThis.clearTimeout(h as ReturnType<typeof setTimeout>),
  },
  now: () => number = () => Date.now(),
): () => void {
  let armedFor: number | null = null;
  let timer: unknown = null;
  const arm = () => {
    const soonest = nextLapse(store.snapshot.agents);
    if (soonest === armedFor) return;
    if (timer !== null) timers.clearTimeout(timer);
    timer = null;
    armedFor = soonest;
    if (soonest === null) return;
    timer = timers.setTimeout(
      () => {
        timer = null;
        armedFor = null;
        store.dispatch({ type: "presence.lapse" });
        arm();
      },
      Math.max(0, soonest - now()),
    );
  };
  const stop = store.subscribe(arm);
  arm();
  return () => {
    stop();
    if (timer !== null) timers.clearTimeout(timer);
  };
}

/** A store around the fold, for `useSyncExternalStore`. */
export class NotebookRuntimeStore {
  private state: RuntimeState = INITIAL_RUNTIME;
  private readonly listeners = new Set<() => void>();

  get snapshot(): RuntimeState {
    return this.state;
  }

  constructor(private readonly now: () => Date = () => new Date()) {}

  dispatch(event: NotebookEvent): void {
    const next = reduceRuntime(this.state, { ...event, received_at: event.received_at ?? this.now().toISOString() });
    if (next === this.state) return;
    this.state = next;
    this.listeners.forEach((l) => l());
  }

  /** The run this tab asked for, by the id the server gave it, so a refusal
   *  that names no cells is still shown on the cells it was for. A refusal
   *  that arrived before the answer to the request takes them now. */
  noteRun(runId: string, target: RunTarget): void {
    const cells = targetCells(target);
    const problem = this.state.runProblem ?? null;
    if (problem !== null && problem.run_id === runId) {
      if (problem.cells !== "all" && problem.cells.length === 0) this.patch({ runProblem: { ...problem, cells } });
      return;
    }
    if (this.state.targets[runId] !== undefined) return;
    this.patch({ targets: { ...this.state.targets, [runId]: cells } });
  }

  patch(patch: Partial<RuntimeState>): void {
    this.state = { ...this.state, ...patch };
    this.listeners.forEach((l) => l());
  }

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => void this.listeners.delete(listener);
  };
}
