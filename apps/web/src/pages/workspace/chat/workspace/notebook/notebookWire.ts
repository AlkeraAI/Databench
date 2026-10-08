// The one place the notebook's wire shapes become the editor's view model.
//
// The routes and the notebook channel carry the engine's own models, typed
// by the generated SDK (`NotebookView`, `CellState`, `KernelInfo`,
// `GraphSummary`, ...). The editor (`@alkera/notebook-ui`) is a presentation
// package with view-model types of its own. Every field read here is read
// through the SDK type, so a backend rename is a type error in this file
// rather than a silently empty panel. What arrives on the channel is still
// untrusted data, so each reader also tolerates a missing or mistyped field.

import {
  CELL_STATUSES,
  EMPTY_RUNTIME,
  type ActingFor,
  type ActorRef,
  type CellRuntime,
  type CellStatus,
  type EnvActionName,
  type EnvInfo,
  type EnvPackage,
  type FrameQuery,
  type GraphView,
  type KernelState,
  type KernelView,
  type RunAttribution,
  type RunTrigger,
} from "@alkera/notebook-ui";

import type {
  GraphCellSummary,
  GraphError,
  GraphSummary,
  NotebookCellState,
  NotebookEnvInfo,
  NotebookKernelInfo,
  NotebookPackageInfo,
  NotebookPresence,
  NotebookView,
} from "@/api/notebooks";

const KERNEL_STATES: ReadonlySet<string> = new Set<KernelState>(["absent", "starting", "idle", "busy", "restarting", "stopped"]);
const STATUSES: ReadonlySet<string> = new Set<CellStatus>(CELL_STATUSES);

const isRecord = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);
const str = (v: unknown): string | null => (typeof v === "string" ? v : null);
const strings = (v: unknown): string[] => (Array.isArray(v) ? v.filter((x): x is string => typeof x === "string") : []);
/** An id the wire uses for someone (`user:<uuid>`, `agent:<id>`, a bare
 *  uuid): never shown as a name. */
const ID_LIKE = /^(?:user|agent|system|person):|^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

// -- graph errors ---------------------------------------------------------------

/** A graph error as the editor reads it: its code, or `code:name` when it
 *  names the name. The wire sends `{code, name?, cells?}`; an older server
 *  sent the bare code. Anything else is no error. */
export function graphErrorText(error: unknown): string | null {
  if (typeof error === "string") return error === "" ? null : error;
  if (!isRecord(error)) return null;
  const e = error as Partial<GraphError>;
  const code = str(e.code);
  if (code === null || code === "") return null;
  const name = str(e.name);
  return name ? `${code}:${name}` : code;
}

/** Every readable graph error of a list. */
export function graphErrors(list: unknown): string[] {
  if (!Array.isArray(list)) return [];
  return list.map(graphErrorText).filter((e): e is string => e !== null);
}

/** The graph a `graph` event or an ops result carries: edges, errors per
 *  cell, and each cell's names. An older server's flat `errors` map is read
 *  too. */
export function graphOf(v: unknown): { graph: GraphView; names: Record<string, { defs: string[]; refs: string[] }> } {
  const summary: Partial<GraphSummary> & { errors?: unknown } = isRecord(v) ? v : {};
  const edges = (Array.isArray(summary.edges) ? (summary.edges as unknown[]) : [])
    .filter((e): e is [string, string] => Array.isArray(e) && typeof e[0] === "string" && typeof e[1] === "string")
    .map((e): [string, string] => [e[0], e[1]]);
  const errors: Record<string, string[]> = {};
  if (isRecord(summary.errors)) for (const [id, list] of Object.entries(summary.errors)) errors[id] = graphErrors(list);
  const names: Record<string, { defs: string[]; refs: string[] }> = {};
  for (const [id, raw] of Object.entries(isRecord(summary.cells) ? summary.cells : {})) {
    const cell: Partial<GraphCellSummary> = isRecord(raw) ? raw : {};
    names[id] = { defs: strings(cell.defs), refs: strings(cell.refs) };
    if (Array.isArray(cell.errors)) errors[id] = graphErrors(cell.errors);
  }
  return { graph: { edges, errors }, names };
}

// -- who did something ------------------------------------------------------------

function actingFor(v: unknown): ActingFor | null {
  if (!isRecord(v)) return null;
  const id = str(v.id) ?? "";
  const name = str(v.display_name) ?? "";
  return id === "" && name === "" ? null : { id, display_name: name };
}

/** An actor as the wire sends one: `{kind, id, display_name, acting_for}`.
 *  A bare string (an older server's `by`) is an id, never a name, so it
 *  carries no display name and the label falls back rather than showing it. */
export function actorOf(v: unknown): ActorRef {
  if (typeof v === "string") {
    const kind = v.startsWith("agent:") ? "agent" : v.startsWith("system") ? "system" : "person";
    return { kind, id: v, display_name: "", acting_for: null };
  }
  const r = isRecord(v) ? v : {};
  const kind = r.kind === "agent" || r.kind === "system" ? r.kind : "person";
  return { kind, id: str(r.id) ?? "", display_name: str(r.display_name) ?? "", acting_for: actingFor(r.acting_for) };
}

/** Who asked for a queued run, as a view's queue names them: by the label
 *  the engine gives (`Alkera agent for Ada`), or by an id from an older one,
 *  which is never shown. */
export function queuedBy(v: unknown): ActorRef {
  if (typeof v === "string" && !ID_LIKE.test(v)) return { kind: "person", id: "", display_name: v, acting_for: null };
  return actorOf(v);
}

/** A run's attribution: `{run_id, by, trigger, started_at, finished_at}`. */
export function attributionOf(v: unknown): RunAttribution | null {
  if (!isRecord(v)) return null;
  const runId = str(v.run_id);
  if (runId === null) return null;
  return {
    run_id: runId,
    by: actorOf(v.by),
    trigger: (str(v.trigger) ?? "run") as RunTrigger,
    started_at: str(v.started_at),
    finished_at: str(v.finished_at),
  };
}

// -- environments ---------------------------------------------------------------

export function envOf(v: unknown): EnvInfo | null {
  if (!isRecord(v)) return null;
  const env = v as Partial<NotebookEnvInfo>;
  const id = str(env.env_id);
  if (id === null) return null;
  return {
    env_id: id,
    kind: str(env.kind) ?? "",
    spec_root: str(env.spec_root) ?? "",
    python: str(env.python) ?? "",
    state: str(env.state) ?? "",
    recorded_in_file: env.recorded_in_file === true,
    ...(str(env.recorded) ? { recorded: str(env.recorded) ?? "" } : {}),
    ...(str(env.last_failure) ? { last_failure: str(env.last_failure) ?? "" } : {}),
    ...(Array.isArray(env.allowed_actions) ? { allowed_actions: env.allowed_actions.filter(isEnvAction) } : {}),
  };
}

const ENV_ACTIONS: readonly EnvActionName[] = ["build", "install", "remove", "cancel"];

function isEnvAction(v: unknown): v is EnvActionName {
  return typeof v === "string" && (ENV_ACTIONS as readonly string[]).includes(v);
}

export function packageOf(v: unknown): EnvPackage | null {
  if (!isRecord(v)) return null;
  const pkg = v as Partial<NotebookPackageInfo>;
  const name = str(pkg.name);
  return name === null ? null : { name, version: str(pkg.version) };
}

// -- table pages ----------------------------------------------------------------

/** A table's sort as the table route takes it: `col:asc,col2:desc`. A column
 *  whose name holds a comma cannot be named in that syntax, so asking to sort
 *  by one is refused here rather than sent as a different column. */
export function sortParam(sort: FrameQuery["sort"]): string | null {
  if (!sort || sort.length === 0) return null;
  return sort
    .map(({ column, descending }) => {
      if (column.includes(",")) throw new Error(`The column ${column} can't be sorted.`);
      return `${column}:${descending ? "desc" : "asc"}`;
    })
    .join(",");
}

// -- the view -------------------------------------------------------------------

/** The view a `snapshot` or `resync` event carries, or `null` when there is
 *  none to read. */
export function viewOf(v: unknown): NotebookView | null {
  if (!isRecord(v)) return null;
  const view = v as Partial<NotebookView>;
  if (!Array.isArray(view.cells) && !isRecord(view.kernel)) return null;
  return view as NotebookView;
}

/** The kernel a view reports, over what was known. A view always names a
 *  state: `absent` when the notebook has no kernel. */
export function kernelOfView(prev: KernelView, v: unknown): KernelView {
  if (!isRecord(v)) return prev;
  const kernel = v as Partial<NotebookKernelInfo>;
  const next: KernelView = { ...prev };
  const state = str(kernel.state);
  if (state !== null && KERNEL_STATES.has(state)) next.state = state as KernelState;
  if (kernel.reactivity === "autorun" || kernel.reactivity === "lazy") next.reactivity = kernel.reactivity;
  if ("memory_bytes" in kernel) next.memory_bytes = typeof kernel.memory_bytes === "number" ? kernel.memory_bytes : null;
  if ("started_at" in kernel) next.started_at = str(kernel.started_at);
  if (typeof kernel.env_outdated === "boolean") next.env_outdated = kernel.env_outdated;
  // The environment is the notebook's, not the kernel's: a view with no
  // kernel names none, and that does not take the known one away.
  if ("env" in kernel && kernel.env !== null) next.env = envOf(kernel.env);
  if (next.state === "absent") next.started_at = null;
  if (next.state === "idle" || next.state === "stopped" || next.state === "absent") next.interrupt = "none";
  return next;
}

/** The view's kernel as a `kernel.state` event, for the fold. Its id and
 *  sequence are named only when the view has them. */
export function kernelEventOfView(kernel: NotebookKernelInfo): Record<string, unknown> & { type: "kernel.state" } {
  return {
    ...kernel,
    type: "kernel.state",
    kernel_id: kernel.kernel_id ?? null,
    ...(typeof kernel.seq === "number" ? { seq: kernel.seq } : {}),
  };
}

/** One cell of a view over what was known of it: its run status (from the
 *  kernel's latest snapshot, `not_run` otherwise), names, graph errors, who
 *  ran it last, and whether its output is a saved or outdated one. The view
 *  carries no outputs, so the ones already held are kept. */
export function cellOfView(prev: CellRuntime | undefined, v: unknown): CellRuntime {
  const base = prev ?? EMPTY_RUNTIME;
  if (!isRecord(v)) return base;
  const cell = v as Partial<NotebookCellState>;
  const status = str(cell.status);
  return {
    ...base,
    status: status !== null && STATUSES.has(status) ? (status as CellStatus) : base.status,
    defs: strings(cell.defs),
    refs: strings(cell.refs),
    graph_errors: graphErrors(cell.graph_errors),
    outdated: cell.output_outdated === true,
    saved: cell.output_origin === "saved" || cell.output_origin === "unknown",
    rerun_waits_for: str(cell.rerun_waits_for),
    last_run: "last_run" in cell ? (attributionOf(cell.last_run) ?? base.last_run) : base.last_run,
  };
}

/** The id of a view's cell, when it has one. */
export function cellIdOf(v: unknown): string | null {
  return isRecord(v) ? str((v as Partial<NotebookCellState>).id) : null;
}

/** An agent the server placed in a cell. `actor` is who it is (one per chat,
 *  whatever cells it is in). `until` is when the claim lapses on this tab's
 *  clock, from how long the server said it holds when it was heard; `null`
 *  when the server named no expiry. */
export interface AgentInCell {
  actor: string;
  who: string;
  display_name: string;
  acting_for: ActingFor | null;
  cell_id: string;
  until: number | null;
}

/** The agents a presence list places in cells (people's carets come from the
 *  live document instead), heard at `heardAt` (ms on this tab's clock), with
 *  the name and the person each works for when the server sends them. `who`
 *  is a name only when it is not an id. */
export function agentsOfView(presence: readonly NotebookPresence[] | undefined, heardAt: number): AgentInCell[] {
  return (presence ?? [])
    .filter((p) => p.kind === "agent")
    .map((p) => {
      const extra = p as NotebookPresence & { display_name?: unknown; acting_for?: unknown };
      const named = str(extra.display_name);
      const holds = typeof p.expires_in === "number" && Number.isFinite(p.expires_in) ? Math.max(p.expires_in, 0) : null;
      return {
        actor: str(p.actor_id) ?? p.who,
        who: p.who,
        display_name: named ?? (ID_LIKE.test(p.who) ? "" : p.who),
        acting_for: actingFor(extra.acting_for),
        cell_id: p.cell_id,
        until: holds === null ? null : heardAt + holds * 1000,
      };
    });
}
