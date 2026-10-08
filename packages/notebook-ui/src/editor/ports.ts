// What the notebook editor needs from its host.
//
// The editor is the same component in the Alkera portal (a live Loro
// document, the engine behind the backend), in a standalone open-source host
// (a file and a local engine) and in a test (plain objects). It reaches each
// of those through four seams, and nothing else:
//
// * the document: the cells as plain data, structure edits as operations,
//   and one history of this person's own changes;
// * a cell's text: a CodeMirror extension keeping one cell's editor and the
//   document's text in step (the host's binding, or a plain one);
// * the engine's state: statuses, outputs, the kernel, the queue, the graph,
//   variables and who is where, as plain data;
// * actions: run, interrupt, restart, install, inspect, all of which the
//   host turns into requests.

import type { Extension } from "@codemirror/state";

import type {
  CellRuntime,
  DocCell,
  EnvInfo,
  GraphView,
  InstallStatus,
  KernelAction,
  KernelView,
  NotebookOp,
  NotebookSettings,
  Presence,
  RunConfirmation,
  RunProblem,
  RunTarget,
} from "../model/types";
import type { EnvPackage } from "../components/panels/EnvironmentPanel";
import type { SettingSource } from "../model/settings";
import type { FrameQuery, TablePageWire } from "../model/types";
import type { FrameServices, OutputTheme } from "../outputs/types";

export interface DocumentSnapshot {
  cells: readonly DocCell[];
  settings: NotebookSettings;
  /** Soft-deleted cells, newest last. */
  deleted: readonly DocCell[];
  version: number;
}

export interface NotebookDocumentPort {
  snapshot(): DocumentSnapshot;
  subscribe(listener: () => void): () => void;
  /** Applies a batch atomically; throws when it cannot. Returns new cell ids. */
  apply(ops: readonly NotebookOp[]): { created: string[] };
  /** This person's last change, anywhere; the cells it touched, or null. */
  undo(): string[] | null;
  redo(): string[] | null;
  readonly canWrite: boolean;
}

/** One cell's editor kept in step with the document's text. */
export interface CellTextBinding {
  /** The text the editor starts from. */
  text(): string;
  /** Everything the editor needs from the binding (sync, history keys,
   *  carets). */
  extension: Extension;
  dispose(): void;
}

export interface CellTextPort {
  bind(cellId: string): CellTextBinding;
}

export interface RuntimeSnapshot {
  cells: Readonly<Record<string, CellRuntime | undefined>>;
  kernel: KernelView;
  graph: GraphView;
  presence: readonly Presence[];
  /** A run the engine wants confirmed before it starts. */
  confirmation: RunConfirmation | null;
  /** Each setting's value in effect and where it came from, as the server
   *  resolved them (the workspace's defaults are only known there). */
  settings?: { values: Readonly<Record<string, unknown>>; sources: Readonly<Record<string, SettingSource>> } | null;
  /** Environments this notebook may switch to. */
  envs: readonly EnvInfo[];
  /** Whether the workspace's members share its environments on the machine
   *  running it (absent: they do). */
  envsShared?: boolean;
  packages: readonly EnvPackage[];
  /** The environment's own requirements, as its spec lists them. */
  requirements?: readonly string[];
  installing: boolean;
  /** The machine serving the notebook is being woken: what needs it waits,
   *  and the kernel chip says so instead of the kernel's own state. */
  waking?: boolean;
  /** The last package install this notebook heard of: under way, or how it
   *  ended. */
  install?: InstallStatus | null;
  /** The environment the notebook names, when it is not the one the running
   *  kernel uses (`kernel.env`): the kernel moves to it on a restart. */
  selectedEnv?: EnvInfo | null;
  /** A notice to show once (an interrupt that restarted the kernel, a
   *  memory kill, an edit the server would not keep); cleared by the host.
   *  `restorable` is text the reader would otherwise lose, offered to copy. */
  notice: { id: string; message: string; tone: "info" | "warning" | "danger"; restorable?: string } | null;
  /** Why the last run did not go through, until one does. */
  runProblem?: RunProblem | null;
}

export interface NotebookActions {
  run(target: RunTarget): void;
  confirmRun(runId: string): void;
  cancelRun(runId: string): void;
  kernel(action: KernelAction): void;
  /** Clear these cells' outputs for everyone (every cell's with `null`).
   *  Without it, a clear hides the outputs in this view only. */
  clearOutputs?(cellIds: string[] | null): void;
  /** Switch the environment to the one the notebook's `env` setting names as
   *  `recorded` (`default`, `script` or a relative path); the kernel restarts. */
  switchEnv(recorded: string): void;
  install(packages: string[]): void;
  /** Build an environment again (a failed or changed one), remove packages from its
   *  spec, or cancel its build under way. */
  changeEnv?(action: "build" | "remove" | "cancel", packages: string[]): void;
  /** Pages a table output through the engine: the table page as the wire
   *  carries it, which the table reads as it reads its output's first page. */
  inspectFrame?(query: FrameQuery): Promise<TablePageWire>;
  copyCellLink?(cellId: string): void;
  openOutput?(cellId: string, outputId: string): void;
  dismissNotice?(id: string): void;
}

/** What a host's SQL connection control is given for one SQL cell. */
export interface SqlConnectionSlot {
  cellId: string;
  /** The connection the cell names; `null` runs it in the notebook's DuckDB. */
  connection: string | null;
  /** Whether this person may change it. */
  canEdit: boolean;
  /** Writes the cell's connection to the document (`null`: DuckDB). */
  onChange(connection: string | null): void;
}

export interface NotebookPermissions {
  /** Can edit: change cells and settings. */
  canEdit: boolean;
  /** Can edit, and so run code (the same right on the platform). */
  canRun: boolean;
}

export interface NotebookOutputSettings {
  theme: OutputTheme;
  frame?: FrameServices;
  /** Where an output stored out of line is read from, by its hash. */
  blobUrl?: (sha256: string) => string;
}
