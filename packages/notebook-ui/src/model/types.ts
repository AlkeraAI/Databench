// The notebook as the editor draws it.
//
// Two halves meet here. The document half (cells, their order, names, kinds,
// text) comes from the live notebook document, which several people and
// agents edit at once. The runtime half (statuses, outputs, the kernel, the run
// queue, the graph, variables) comes from the engine's events. The components
// take both as plain data; they never fetch, never hold the document and never
// talk to a kernel. Field spellings that mirror the engine's wire shapes keep
// the engine's snake case, so a payload is read without a renaming layer.

/** The kinds the format derives from a cell's code. A kind this build does not
 *  know is drawn as Python (an older reader's rule for a newer kind). */
export type KnownCellKind = "setup" | "python" | "function" | "class" | "sql" | "markdown" | "unparsable";
export type CellKind = KnownCellKind | (string & {});

/** A cell's state relative to the current kernel. */
export type CellStatus =
  | "fresh"
  | "edited"
  | "stale"
  | "not_run"
  | "queued"
  | "running"
  | "error"
  | "interrupted"
  | "skipped"
  | "stopped"
  | "disabled";

export const CELL_STATUSES: readonly CellStatus[] = [
  "fresh",
  "edited",
  "stale",
  "not_run",
  "queued",
  "running",
  "error",
  "interrupted",
  "skipped",
  "stopped",
  "disabled",
];

/** marimo's per-cell config: only non-default keys are present. */
export interface CellConfig {
  disabled?: boolean;
  hide_code?: boolean;
  expand_output?: boolean;
  column?: number | null;
  [key: string]: unknown;
}

/** One cell of the live document. */
export interface DocCell {
  id: string;
  kind: CellKind;
  /** `_` or an identifier. */
  name: string;
  /** The editor text: Python code, the SQL body or the Markdown body. */
  source: string;
  config: CellConfig;
  /** Kind-specific: `connection` and `output_var` for SQL, `quote` for Markdown. */
  meta: Record<string, unknown>;
}

/** The person an agent acts for. */
export interface ActingFor {
  id: string;
  display_name: string;
}

/** Who did something, as the engine names them. An agent working for a
 *  person names that person in `acting_for`. */
export interface ActorRef {
  kind: "person" | "agent" | "system";
  id: string;
  display_name: string;
  acting_for?: ActingFor | null;
}

export interface RunAttribution {
  run_id: string;
  by: ActorRef;
  trigger: RunTrigger;
  started_at: string | null;
  finished_at: string | null;
}

export type RunTrigger = "run" | "run_all" | "run_stale" | "widget" | "autorun";

/** A MIME bundle: the richest type a renderer knows wins. */
export type MimeBundle = Record<string, unknown>;

export interface ErrorInfo {
  ename: string;
  evalue: string;
  traceback: string[];
  /** A graph or planning error the editor can act on, such as
   *  `multiple_definitions` or `cycle`; absent for an exception. */
  kind?: string;
  /** The names a graph error is about. */
  names?: string[];
  /** Other cells a graph error involves. */
  cells?: string[];
}

export type CellOutput =
  | { output_id: string; type: "display"; data: MimeBundle; metadata?: Record<string, unknown> }
  | { output_id: string; type: "stream"; name: "stdout" | "stderr"; text: string }
  | { output_id: string; type: "error"; error: ErrorInfo };

export interface VarSummary {
  name: string;
  type: string;
  repr: string;
  size_bytes?: number | null;
  shape?: number[] | null;
  columns?: string[] | null;
  /** The cell that defines it. */
  cell_id?: string;
}

/** What the engine says about one cell. */
export interface CellRuntime {
  status: CellStatus;
  outputs: CellOutput[];
  /** Outputs restored from the saved snapshot rather than produced by this
   *  kernel: shown dimmed as "Not run in this kernel". */
  saved: boolean;
  /** A saved output whose lineage or environment no longer matches. */
  outdated: boolean;
  last_run: RunAttribution | null;
  /** How long the last run of this cell took, in milliseconds. */
  duration_ms: number | null;
  defs: string[];
  refs: string[];
  graph_errors: string[];
  variables: VarSummary[];
  /** Whose editing this cell's re-run waits for (an autorun skipped it while
   *  they were in it); null once it ran, changed or they left. */
  rerun_waits_for: string | null;
  /** The last thing the engine said about this cell alone, until its next run. */
  notice: { kind: string; message: string } | null;
}

export const EMPTY_RUNTIME: CellRuntime = {
  status: "not_run",
  outputs: [],
  saved: false,
  outdated: false,
  last_run: null,
  duration_ms: null,
  defs: [],
  refs: [],
  graph_errors: [],
  variables: [],
  rerun_waits_for: null,
  notice: null,
};

/** Why the last run did not go through: it was refused, it did not reach
 *  the machine, or its environment failed before any cell ran. Shown in the
 *  run status area and on the cells it was for, until a run goes through. */
export interface RunProblem {
  run_id: string;
  message: string;
  tone: "warning" | "danger";
  /** The cells the run was for: every cell, or the ones named. */
  cells: "all" | readonly string[];
}

export type KernelState = "absent" | "starting" | "idle" | "busy" | "restarting" | "stopped";
export type Reactivity = "autorun" | "lazy";

/** Where an interrupt is: the signal sent, the second signal, then the restart. */
export type InterruptPhase = "none" | "signalled" | "resignalled" | "restarting";

/** What a person can do to an environment, as the engine says it admits. */
export type EnvActionName = "build" | "install" | "remove" | "cancel";

export interface EnvInfo {
  env_id: string;
  kind: string;
  spec_root: string;
  python: string;
  state: string;
  recorded_in_file: boolean;
  /** What the notebook's `env` setting says to choose this environment. */
  recorded?: string;
  /** Why the last build attempt failed, while an older build stays in use. */
  last_failure?: string;
  /** What the environment admits now; absent from an engine that predates
   *  it, which offered only installs. */
  allowed_actions?: readonly EnvActionName[];
}

/** A package install: asked for and waiting, done, refused, or never heard
 *  back from. */
export interface InstallStatus {
  status: "running" | "ok" | "error" | "unknown";
  /** What was asked (absent: an install). */
  action?: EnvActionName;
  packages: readonly string[];
  /** Why it failed, in the server's words, when it said. */
  message: string | null;
}

export interface QueuedRun {
  run_id: string;
  by: ActorRef;
  trigger: RunTrigger;
  status: "queued" | "running";
  /** The cells the run asked for. */
  targets: string[];
}

export interface KernelView {
  state: KernelState;
  env: EnvInfo | null;
  reactivity: Reactivity;
  memory_bytes: number | null;
  /** The memory guard's threshold for this workspace. */
  memory_limit_bytes: number | null;
  started_at: string | null;
  queue: QueuedRun[];
  interrupt: InterruptPhase;
  /** The engine's word that the kernel runs on an environment build an
   *  install or rebuild has since replaced; a restart moves it on. */
  env_outdated: boolean;
}

export const ABSENT_KERNEL: KernelView = {
  state: "absent",
  env: null,
  reactivity: "autorun",
  memory_bytes: null,
  memory_limit_bytes: null,
  started_at: null,
  queue: [],
  interrupt: "none",
  env_outdated: false,
};

/** The cells a run will touch and why, when the engine asks first. */
export interface PlannedStep {
  cell_id: string;
  name: string;
  reason: "target" | "upstream" | "descendant";
  /** The duration it last ran for, when known. */
  last_duration_ms?: number | null;
}

export interface RunConfirmation {
  run_id: string;
  plan: PlannedStep[];
  estimate_s: number;
}

export type RunTarget =
  | { kind: "cells"; ids: string[] }
  | { kind: "all" }
  | { kind: "stale" }
  | { kind: "above"; id: string }
  | { kind: "below"; id: string };

export type KernelAction = "interrupt" | "interrupt_all" | "restart" | "shutdown";

/** Someone in the notebook: a person or an agent, and the cell they are in. */
export interface Presence {
  id: string;
  kind: "person" | "agent";
  display_name: string;
  /** For an agent: the person it works for. */
  acting_for?: ActingFor | null;
  hue: number;
  cell_id: string | null;
}

/** A notebook's settings, as its header records them. */
export interface NotebookSettings {
  reactivity: Reactivity;
  dataframe?: string;
  env?: string;
  outputs_in_git?: boolean;
  autoreload?: string;
  /** Every setting the file itself holds, by name, as the schema names them. */
  stored?: Readonly<Record<string, unknown>>;
}

// -- the document operations the editor writes -----------------------------

export interface InsertCellOp {
  op: "insert";
  kind?: CellKind;
  source?: string;
  name?: string;
  after?: string | null;
  before?: string | null;
  config?: CellConfig;
  meta?: Record<string, unknown>;
}
export interface TextEdit {
  old: string;
  new: string;
  occurrence?: number | null;
}
export interface EditCellOp {
  op: "edit";
  cell_id: string;
  edits: TextEdit[];
}
export interface ReplaceCellOp {
  op: "replace";
  cell_id: string;
  source: string;
}
export interface DeleteCellOp {
  op: "delete";
  cell_id: string;
}
export interface RestoreCellOp {
  op: "restore";
  cell_id: string;
  after?: string | null;
}
export interface MoveCellOp {
  op: "move";
  cell_id: string;
  after?: string | null;
  before?: string | null;
}
export interface RenameCellOp {
  op: "rename";
  cell_id: string;
  name: string;
}
export interface SetKindOp {
  op: "set_kind";
  cell_id: string;
  kind: CellKind;
}
export interface SetConfigOp {
  op: "set_config";
  cell_id: string;
  config: CellConfig;
}
/** A SQL or Markdown cell's settings (`connection`, `output_var`,
 *  `show_output`; `quote`). Keys not named keep their value; `null` resets a
 *  key to its default (a SQL cell with no `connection` runs in DuckDB). */
export interface SetMetaOp {
  op: "set_meta";
  cell_id: string;
  meta: Record<string, unknown>;
}
export interface SetSettingOp {
  op: "set_setting";
  key: string;
  value: unknown;
}

export type NotebookOp =
  | InsertCellOp
  | EditCellOp
  | ReplaceCellOp
  | DeleteCellOp
  | RestoreCellOp
  | MoveCellOp
  | RenameCellOp
  | SetKindOp
  | SetConfigOp
  | SetMetaOp
  | SetSettingOp;

// -- the dependency graph ---------------------------------------------------

/** Who reads from whom: an edge `[a, b]` says `b` reads a name `a` defines. */
export interface GraphView {
  edges: [string, string][];
  /** Graph errors per cell (`multiple_definitions`, `cycle`, ...). */
  errors: Record<string, string[]>;
}

// -- table outputs ------------------------------------------------------------

export interface TableField {
  name: string;
  type: string;
}

/** `application/vnd.alkera.table+json`: a frame's schema, its first rows, and
 *  the name the kernel registered it under so more can be asked for. */
export interface TablePayload {
  /** The registered frame the rows came from, for paging through
   *  `notebook.inspect frame`. Absent: the rows are all there is. */
  name?: string;
  schema: { fields: TableField[] };
  data: Record<string, unknown>[];
  total_rows: number;
}

export interface FrameQuery {
  name: string;
  /** The cell whose table output is paged: the host asks for the page by it. */
  cell_id?: string;
  offset: number;
  limit: number;
  sort?: { column: string; descending: boolean }[];
  /** A single SELECT over the table named `frame`. */
  filter_sql?: string;
}

/** A table page as the wire carries it:
 *  `{schema: [{name, type}], rows: [[...]], total_rows, offset}`. The kernel
 *  makes every page this way, the one a table output carries first and each
 *  one read after it, and `readTablePayload` is the one reader of both. */
export type TablePageWire = unknown;
